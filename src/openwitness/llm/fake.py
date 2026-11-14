"""Upstream `fake`: respuestas guionizadas con Anthropic Messages o con la Responses API, en SSE o JSON (SPEC §7.7, §7.8, §17.4).

Reparto: Adrian escribe el transporte (este archivo: SSE, `FakeUpstream`, app ASGI). Andres escribe el
"modelo" (el `Responder` que decide qué contestar) para los escenarios del benchmark; se enchufa con
`FakeUpstream(profile, responder=...)` o `fake_app(responder)`.

Un `Responder` es cualquier callable `body -> list[dict]` que devuelve los bloques de contenido del asistente
(`{"type": "text", ...}` o `{"type": "tool_use", "name": ..., "input": ...}`; el `id` se completa solo).

Con la Responses API, el `Responder` recibe la petición ya traducida a la forma Anthropic (`messages`, `tools`
con los nombres neutrales, p. ej. `mcp__issues__list_issues`) y sus bloques se emiten como ítems de Responses:
así los mismos guiones sirven para Claude Code y para Codex.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import yaml

from .base import LLMProfile, UpstreamResponse

Responder = Callable[[dict], list[dict]]


def echo_responder(body: dict) -> list[dict]:
    """Por defecto: contesta un texto fijo. Sirve para la latencia y para las peticiones auxiliares de un agente."""
    return [{"type": "text", "text": "ok"}]


class ScriptedResponder:
    """Guion YAML: una lista de pasos; el paso k responde a la petición con k mensajes de asistente previos.

    ```yaml
    steps:
      - tool_use: {name: mcp__issues__list_issues, input: {project: demo}}
      - text: "Listo."
    ```
    Si el historial supera el guion, contesta el último paso que sea texto (o "ok").
    """

    def __init__(self, steps: list[dict]):
        self.steps = steps

    @classmethod
    def load(cls, path: Path | str) -> "ScriptedResponder":
        d = yaml.safe_load(Path(path).read_text()) or {}
        return cls(d.get("steps", d if isinstance(d, list) else []))

    def __call__(self, body: dict) -> list[dict]:
        k = sum(1 for m in body.get("messages") or [] if m.get("role") == "assistant")
        if k >= len(self.steps):
            return [{"type": "text", "text": "ok"}]
        st = self.steps[k]
        offered = {t.get("name") for t in body.get("tools") or []}
        uses = st.get("tool_uses", []) + ([st["tool_use"]] if "tool_use" in st else [])
        if uses and not all(u["name"] in offered for u in uses):
            return [{"type": "text", "text": "ok"}]   # petición auxiliar del agente (título, resumen…)
        blocks = []
        if "text" in st:
            blocks.append({"type": "text", "text": st["text"]})
        for tu in uses:
            blocks.append({"type": "tool_use", "name": tu["name"], "input": tu.get("input", {})})
        return blocks or [{"type": "text", "text": "ok"}]


def complete_blocks(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        b = dict(b)
        if b.get("type") == "tool_use":
            b.setdefault("id", "toolu_" + uuid.uuid4().hex[:20])
            b.setdefault("input", {})
        out.append(b)
    return out


def message_json(blocks: list[dict], model: str) -> dict:
    stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model,
            "content": blocks, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def sse_events(msg: dict, chunk_text: int = 16) -> list[bytes]:
    """El mensaje completo como la secuencia de eventos SSE que emite la API real."""
    head = dict(msg, content=[], stop_reason=None)
    out = [_sse("message_start", {"type": "message_start", "message": head})]
    for i, b in enumerate(msg["content"]):
        if b["type"] == "text":
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "text", "text": ""}}))
            t = b["text"]
            for j in range(0, max(len(t), 1), chunk_text):
                out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                        "delta": {"type": "text_delta", "text": t[j:j + chunk_text]}}))
        elif b["type"] == "tool_use":
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "tool_use", "id": b["id"],
                                                                      "name": b["name"], "input": {}}}))
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "input_json_delta",
                                                              "partial_json": json.dumps(b["input"])}}))
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"],
                                                                         "stop_sequence": None},
                                      "usage": {"output_tokens": msg["usage"]["output_tokens"]}}))
    out.append(_sse("message_stop", {"type": "message_stop"}))
    return out


def respond(responder: Responder, body: dict, model: str) -> tuple[bool, dict]:
    msg = message_json(complete_blocks(responder(body)), body.get("model") or model)
    return bool(body.get("stream")), msg


# ---------------------------------------------------------------- Responses API (§17.4)

def neutral_body(body: dict) -> dict:
    """Petición de Responses en la forma que esperan los `Responder` (Anthropic, nombres neutrales)."""
    from ..gateway.protocols.openai_responses import OpenAIResponsesAdapter
    req = OpenAIResponsesAdapter().parse_request(body)
    return {"model": req.model, "stream": req.stream, "system": req.system,
            "messages": [m.to_dict() for m in req.messages if m.role != "system"],
            "tools": [t.to_dict() for t in req.tools]}


def _namespaces(body: dict) -> dict[str, tuple[str, str]]:
    """Nombre neutral -> (namespace, nombre) de las herramientas agrupadas en un `namespace`."""
    out = {}
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == "namespace":
            for x in t.get("tools") or []:
                if isinstance(x, dict):
                    out[f"{t.get('name')}__{x.get('name')}"] = (t.get("name", ""), x.get("name", ""))
    return out


RAW_ITEMS = ("message", "function_call", "custom_tool_call", "web_search_call")


def response_items(blocks: list[dict], body: dict) -> list[dict]:
    """Bloques del `Responder` como ítems de salida. Un ítem de Responses ya armado (p. ej. de una fixture) pasa
    tal cual; `tool_use` a `mcp__<s>__<t>` sale con `namespace`, como lo emite el modelo, aunque el gateway la
    haya quitado de `tools` (así se prueba un modelo que insiste en una herramienta bloqueada)."""
    ns = _namespaces(body)
    items: list[dict] = []
    for b in complete_blocks(blocks):
        if b.get("type") in RAW_ITEMS or (b.get("type") == "reasoning" and "encrypted_content" in b):
            items.append(b)
        elif b.get("type") == "text":
            items.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:16], "status": "completed",
                          "role": "assistant",
                          "content": [{"type": "output_text", "text": b["text"], "annotations": []}]})
        elif b.get("type") == "tool_use":
            name = b["name"]
            it = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:16], "status": "completed",
                  "call_id": "call_" + b["id"].removeprefix("toolu_"), "arguments": json.dumps(b["input"])}
            if name in ns:
                it["namespace"], it["name"] = ns[name]
            elif name.startswith("mcp__") and "__" in name[5:]:
                server, _, tool = name[5:].partition("__")
                it["namespace"], it["name"] = f"mcp__{server}", tool
            else:
                it["name"] = name
            items.append(it)
        elif b.get("type") == "reasoning":
            items.append({"type": "reasoning", "id": "rs_" + uuid.uuid4().hex[:16], "summary":
                          [{"type": "summary_text", "text": b.get("thinking", "")}] if b.get("thinking") else [],
                          "encrypted_content": b.get("encrypted_content", "gAAAA" + uuid.uuid4().hex)})
    return items


def responses_json(items: list[dict], model: str) -> dict:
    return {"id": "resp_" + uuid.uuid4().hex[:24], "object": "response", "created_at": 0, "status": "completed",
            "model": model, "output": items, "error": None, "incomplete_details": None,
            "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 5,
                      "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 15}}


def _ev(data: dict) -> bytes:
    return f"event: {data['type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def responses_sse_events(resp: dict, chunk_text: int = 16) -> list[bytes]:
    """La respuesta completa como la secuencia de eventos SSE de la Responses API."""
    head = dict(resp, status="in_progress", output=[], usage=None)
    out = [_ev({"type": "response.created", "sequence_number": 0, "response": head}),
           _ev({"type": "response.in_progress", "sequence_number": 1, "response": head})]
    n = 2
    for i, it in enumerate(resp["output"]):
        added = dict(it, status="in_progress")
        if it["type"] == "message":
            added["content"] = []
        elif it["type"] == "function_call":
            added["arguments"] = ""
        out.append(_ev({"type": "response.output_item.added", "sequence_number": n, "output_index": i,
                        "item": added}))
        n += 1
        if it["type"] == "message":
            t = it["content"][0]["text"]
            out.append(_ev({"type": "response.content_part.added", "sequence_number": n, "item_id": it["id"],
                            "output_index": i, "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []}}))
            n += 1
            for j in range(0, max(len(t), 1), chunk_text):
                out.append(_ev({"type": "response.output_text.delta", "sequence_number": n, "item_id": it["id"],
                                "output_index": i, "content_index": 0, "delta": t[j:j + chunk_text]}))
                n += 1
            out.append(_ev({"type": "response.output_text.done", "sequence_number": n, "item_id": it["id"],
                            "output_index": i, "content_index": 0, "text": t}))
            n += 1
        elif it["type"] == "function_call":
            out.append(_ev({"type": "response.function_call_arguments.delta", "sequence_number": n,
                            "item_id": it["id"], "output_index": i, "delta": it["arguments"]}))
            out.append(_ev({"type": "response.function_call_arguments.done", "sequence_number": n + 1,
                            "item_id": it["id"], "output_index": i, "arguments": it["arguments"]}))
            n += 2
        out.append(_ev({"type": "response.output_item.done", "sequence_number": n, "output_index": i, "item": it}))
        n += 1
    out.append(_ev({"type": "response.completed", "sequence_number": n, "response": resp}))
    return out


def respond_responses(responder: Responder, body: dict, model: str) -> tuple[bool, dict]:
    items = response_items(responder(neutral_body(body)), body)
    return bool(body.get("stream")), responses_json(items, body.get("model") or model)


async def _iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


class FakeUpstream:
    """Upstream en proceso: no abre sockets (rápido y determinista para tests y benchmark)."""

    def __init__(self, profile: LLMProfile | None = None, responder: Responder | None = None):
        self.profile = profile or LLMProfile("fake", "fake", model="fake-model")
        if responder is None and self.profile.script:
            responder = ScriptedResponder.load(self.profile.script)
        self.responder = responder or echo_responder
        self.requests: list[dict] = []          # lo que realmente llegó al "modelo" (tras el filtrado)

    async def forward(self, protocol: str, body: dict, headers: dict[str, str], query: str = "") -> UpstreamResponse:
        self.requests.append(body)
        if protocol == "openai_responses":
            stream, resp = respond_responses(self.responder, body, self.profile.model or "fake-model")
            if stream:
                return UpstreamResponse(200, {"content-type": "text/event-stream"}, _iter(responses_sse_events(resp)))
            return UpstreamResponse(200, {"content-type": "application/json"}, _iter([json.dumps(resp).encode()]))
        stream, msg = respond(self.responder, body, self.profile.model or "fake-model")
        if stream:
            return UpstreamResponse(200, {"content-type": "text/event-stream"}, _iter(sse_events(msg)))
        return UpstreamResponse(200, {"content-type": "application/json"}, _iter([json.dumps(msg).encode()]))

    async def passthrough(self, method: str, path: str, body: bytes, headers: dict[str, str],
                          query: str = "") -> UpstreamResponse:
        if path.endswith("/count_tokens"):
            data: Any = {"input_tokens": 10}
        elif path.endswith("/models"):
            data = {"object": "list", "data": [{"id": self.profile.model or "fake-model", "type": "model",
                                                "object": "model"}], "has_more": False}
        else:
            return UpstreamResponse(404, {"content-type": "application/json"}, _iter([b'{"error":"not found"}']))
        return UpstreamResponse(200, {"content-type": "application/json"}, _iter([json.dumps(data).encode()]))

    def describe(self) -> dict:
        return {"profile": self.profile.name, "provider": "fake", "model_requested": self.profile.model,
                "script": self.profile.script}


def fake_app(responder: Responder | None = None, model: str = "fake-model"):
    """El mismo modelo falso como servidor HTTP (para medir la latencia con y sin gateway, §9.4)."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    resp = responder or echo_responder

    async def messages(request: Request):
        body = await request.json()
        stream, msg = respond(resp, body, model)
        if stream:
            return StreamingResponse(_iter(sse_events(msg)), media_type="text/event-stream")
        return JSONResponse(msg)

    async def count_tokens(request: Request):
        return JSONResponse({"input_tokens": 10})

    async def responses(request: Request):
        body = await request.json()
        stream, r = respond_responses(resp, body, model)
        if stream:
            return StreamingResponse(_iter(responses_sse_events(r)), media_type="text/event-stream")
        return JSONResponse(r)

    return Starlette(routes=[Route("/v1/messages", messages, methods=["POST"]),
                             Route("/v1/messages/count_tokens", count_tokens, methods=["POST"]),
                             Route("/v1/responses", responses, methods=["POST"])])
