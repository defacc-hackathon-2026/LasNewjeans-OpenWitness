"""Upstream `fake`: respuestas guionizadas con el protocolo Anthropic Messages, en SSE o JSON (SPEC §7.7, §7.8).

Reparto: Adrian escribe el transporte (este archivo: SSE, `FakeUpstream`, app ASGI). Andres escribe el
"modelo" (el `Responder` que decide qué contestar) para los escenarios del benchmark; se enchufa con
`FakeUpstream(profile, responder=...)` o `fake_app(responder)`.

Un `Responder` es cualquier callable `body -> list[dict]` que devuelve los bloques de contenido del asistente
(`{"type": "text", ...}` o `{"type": "tool_use", "name": ..., "input": ...}`; el `id` se completa solo).
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
        blocks = []
        if "text" in st:
            blocks.append({"type": "text", "text": st["text"]})
        for tu in st.get("tool_uses", []) + ([st["tool_use"]] if "tool_use" in st else []):
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
        stream, msg = respond(self.responder, body, self.profile.model or "fake-model")
        if stream:
            return UpstreamResponse(200, {"content-type": "text/event-stream"}, _iter(sse_events(msg)))
        return UpstreamResponse(200, {"content-type": "application/json"}, _iter([json.dumps(msg).encode()]))

    async def passthrough(self, method: str, path: str, body: bytes, headers: dict[str, str],
                          query: str = "") -> UpstreamResponse:
        if path.endswith("/count_tokens"):
            data: Any = {"input_tokens": 10}
        elif path.endswith("/models"):
            data = {"data": [{"id": self.profile.model or "fake-model", "type": "model"}], "has_more": False}
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

    return Starlette(routes=[Route("/v1/messages", messages, methods=["POST"]),
                             Route("/v1/messages/count_tokens", count_tokens, methods=["POST"])])
