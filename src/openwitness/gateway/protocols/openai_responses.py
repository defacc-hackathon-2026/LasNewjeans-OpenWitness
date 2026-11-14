"""Protocolo OpenAI Responses: Codex CLI (SPEC §17.4).

El adaptador traduce los ítems de la Responses API a los tipos neutrales de `types.py`, con los mismos bloques
que usa el adaptador Anthropic (`text`, `tool_use`, `tool_result`), para que el `Decomposer`, `verify_ledger` y
la atribución no tengan que saber de qué protocolo viene cada petición.

| Responses API                                   | Neutral                                                   |
|-------------------------------------------------|-----------------------------------------------------------|
| `instructions`                                  | `system`                                                  |
| `message` rol `developer` / `system`            | `Message("system")`                                       |
| `message` rol `user` (`input_text`)             | `Message("user")` con bloques `text`                      |
| `message` rol `assistant` (`output_text`)       | bloque `text` de un `Message("assistant")`                |
| `function_call` (`call_id`, `name`, `namespace`)| bloque `tool_use` (`id = call_id`, `input` = args parseados) |
| `function_call_output`                          | bloque `tool_result` (`tool_use_id = call_id`)            |
| `custom_tool_call` / `custom_tool_call_output`  | `tool_use` / `tool_result` (`input = {"input": <texto>}`) |
| `reasoning` (`encrypted_content`)               | bloque `reasoning` opaco: solo el hash, nunca se descifra |
| `web_search_call` y otras alojadas              | `tool_use` con origen `hosted:<tipo>`                     |

Los ítems seguidos del asistente (`reasoning`, `message`, `function_call`…) forman un solo `Message("assistant")`,
igual que en Anthropic un turno del asistente es un mensaje con varios bloques. Los ítems seguidos de usuario o
de resultados de herramienta forman un `Message("user")`.

Nombres de herramientas: Codex agrupa las de un servidor MCP en un `namespace` (`mcp__<server>`) y el modelo
llama a `{"namespace": "mcp__issues", "name": "list_issues"}`. El nombre neutral es `mcp__issues__list_issues`,
el mismo que usa Claude Code y el que reportan los hooks de Codex: así `tool_origin`, `ToolFilter`, la
atribución y `tool-check` funcionan sin cambios.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from ...canonical import sha256_tagged
from ...types import Message, NormalizedRequest, NormalizedResponse, ToolDef
from .anthropic_messages import AnthropicMessagesAdapter

PROTOCOL = "openai_responses"
NS_SEP = "__"
ASSISTANT_ITEMS = {"function_call", "custom_tool_call", "reasoning", "local_shell_call", "web_search_call",
                   "file_search_call", "code_interpreter_call", "image_generation_call", "computer_call",
                   "mcp_call", "tool_search_call"}
OUTPUT_ITEMS = {"function_call_output", "custom_tool_call_output", "local_shell_call_output",
                "computer_call_output", "tool_search_output"}
# Herramientas que ejecuta el proveedor, no el agente: no pasan por el hook PreToolUse (L9).
HOSTED_CALLS = {"web_search_call": "web_search", "file_search_call": "file_search",
                "code_interpreter_call": "code_interpreter", "image_generation_call": "image_generation",
                "mcp_call": "remote_mcp"}
HOSTED_TOOL_TYPES = {"web_search", "web_search_preview", "file_search", "code_interpreter", "image_generation",
                     "computer_use_preview", "mcp"}


def neutral_name(name: str, namespace: str | None = None) -> str:
    return f"{namespace}{NS_SEP}{name}" if namespace else name


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        return {"_unparsed": raw}
    return v if isinstance(v, dict) else {"_value": v}


def _content_text(part: Any) -> dict | None:
    """Un `content` de mensaje o de salida de herramienta como bloque neutral."""
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None
    t = part.get("type")
    if t in ("input_text", "output_text", "text"):
        return {"type": "text", "text": part.get("text", "")}
    if t == "refusal":
        return {"type": "text", "text": part.get("refusal", "")}
    if t in ("input_image", "input_file", "input_audio"):
        ref = part.get("image_url") or part.get("file_id") or part.get("file_url") or part.get("filename") or ""
        return {"type": "image" if t == "input_image" else "file",
                "id": sha256_tagged(json.dumps(ref, sort_keys=True).encode()) if ref else ""}
    return {"type": t or "unknown"}


def _contents(c: Any) -> list[dict]:
    if c is None:
        return []
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    out = []
    for p in c if isinstance(c, list) else [c]:
        b = _content_text(p)
        if b is not None:
            out.append(b)
    return out


def item_block(item: dict) -> list[dict]:
    """Bloques neutrales de un ítem del asistente (los mismos para la petición y para la respuesta)."""
    t = item.get("type")
    if t == "message":
        return [b for b in _contents(item.get("content")) if b.get("type") != "text" or b.get("text")]
    if t == "function_call":
        return [{"type": "tool_use", "id": item.get("call_id") or item.get("id", ""),
                 "name": neutral_name(item.get("name", ""), item.get("namespace")),
                 "input": _parse_args(item.get("arguments"))}]
    if t == "custom_tool_call":
        return [{"type": "tool_use", "id": item.get("call_id") or item.get("id", ""), "name": item.get("name", ""),
                 "input": {"input": item.get("input", "")}}]
    if t == "local_shell_call":
        return [{"type": "tool_use", "id": item.get("call_id") or item.get("id", ""), "name": "local_shell",
                 "input": item.get("action") or {}}]
    if t == "reasoning":
        summary = "\n".join(p.get("text", "") for p in item.get("summary") or [] if isinstance(p, dict))
        opaque = item.get("encrypted_content") or ""
        return [{"type": "reasoning", "thinking": summary,
                 "id": sha256_tagged(opaque.encode()) if opaque else ""}]
    if t in HOSTED_CALLS:
        args = {k: v for k, v in item.items() if k in ("action", "queries", "query", "code", "server_label",
                                                         "name", "arguments")}
        return [{"type": "tool_use", "id": item.get("id", ""), "name": HOSTED_CALLS[t], "input": args,
                 "hosted": True}]
    if t == "tool_search_call":
        return [{"type": "tool_use", "id": item.get("call_id") or item.get("id", ""), "name": "tool_search",
                 "input": _parse_args(item.get("arguments"))}]
    return [{"type": t or "unknown", "id": item.get("id", "")}]


def output_block(item: dict) -> dict:
    out = item.get("output")
    if item.get("type") == "local_shell_call_output" and isinstance(out, str):
        out = [out]
    return {"type": "tool_result", "tool_use_id": item.get("call_id") or item.get("id", ""),
            "content": _contents(out), "is_error": False}


def _role(item: dict) -> str:
    t = item.get("type", "message" if "role" in item else None)
    if t == "message":
        r = item.get("role", "user")
        return "system" if r in ("developer", "system") else ("assistant" if r == "assistant" else "user")
    if t in ASSISTANT_ITEMS:
        return "assistant"
    return "user"


def parse_input(inp: Any) -> list[Message]:
    """`input` de la petición como mensajes neutrales, agrupando ítems seguidos del mismo rol."""
    if isinstance(inp, str):
        return [Message("user", [{"type": "text", "text": inp}])]
    msgs: list[Message] = []
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        role = _role(item)
        t = item.get("type", "message")
        if t == "message" and role != "assistant":
            blocks = _contents(item.get("content"))
        elif t in OUTPUT_ITEMS:
            blocks = [output_block(item)]
        elif role == "assistant":
            blocks = item_block(item)
        else:
            blocks = [{"type": t, "id": item.get("id", "")}]
        if role == "system" or not msgs or msgs[-1].role != role:
            msgs.append(Message(role, blocks))       # cada mensaje developer es un segmento propio
        else:
            msgs[-1].content.extend(blocks)
    return msgs


def parse_tools(tools: Any) -> list[ToolDef]:
    out: list[ToolDef] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        ty = t.get("type", "function")
        if ty == "function":
            out.append(ToolDef(t.get("name", ""), t.get("description", "") or "", t.get("parameters") or {}))
        elif ty == "namespace":
            ns = t.get("name", "")
            for inner in t.get("tools") or []:
                if isinstance(inner, dict):
                    out.append(ToolDef(neutral_name(inner.get("name", ""), ns), inner.get("description", "") or "",
                                       inner.get("parameters") or {}))
        elif ty == "custom":
            out.append(ToolDef(t.get("name", ""), t.get("description", "") or "", {"format": t.get("format") or {}}))
        else:   # herramientas alojadas (web_search…) y tipos nuevos: el descriptor es el objeto entero
            out.append(ToolDef(t.get("name") or ty, t.get("description", "") or "",
                               {k: v for k, v in t.items() if k not in ("name", "description")}))
    return out


class OpenAIResponsesAdapter:
    protocol = PROTOCOL

    def parse_request(self, body: dict) -> NormalizedRequest:
        return NormalizedRequest(PROTOCOL, body.get("instructions") or "", parse_input(body.get("input")),
                                 parse_tools(body.get("tools")), body.get("model"), bool(body.get("stream")))

    @staticmethod
    def tool_origin(tool_name: str) -> tuple[str, str]:
        if tool_name in HOSTED_CALLS.values():
            return f"hosted:{tool_name}", tool_name
        return AnthropicMessagesAdapter.tool_origin(tool_name)

    def strip_tools(self, body: dict, rejected: set[str]) -> dict:
        """Copia de la petición sin las herramientas rechazadas (también dentro de un `namespace`)."""
        if not rejected or not isinstance(body.get("tools"), list):
            return body
        out = copy.copy(body)
        tools = []
        for t in body["tools"]:
            if not isinstance(t, dict):
                tools.append(t)
            elif t.get("type") == "namespace":
                ns = t.get("name", "")
                inner = [x for x in t.get("tools") or []
                         if not (isinstance(x, dict) and neutral_name(x.get("name", ""), ns) in rejected)]
                if inner:
                    tools.append(dict(t, tools=inner))
            elif t.get("name") not in rejected:
                tools.append(t)
        out["tools"] = tools
        tc = body.get("tool_choice")
        if isinstance(tc, dict) and neutral_name(tc.get("name", ""), tc.get("namespace")) in rejected:
            out["tool_choice"] = "auto"
        return out

    @staticmethod
    def referenced_tools(body: dict) -> set[str]:
        return set()      # sin `tool_reference`: Codex manda todas las herramientas en `tools`

    @staticmethod
    def incomplete_history(body: dict) -> bool:
        return bool(body.get("previous_response_id") or body.get("conversation"))

    @staticmethod
    def parse_json_response(data: dict, status: int = 200) -> NormalizedResponse:
        return response_from(data, status)

    @staticmethod
    def reassembler() -> ResponsesSSEReassembler:
        return ResponsesSSEReassembler()

    @staticmethod
    def error_body(kind: str, message: str) -> dict:
        return {"error": {"type": kind, "code": kind, "message": message, "param": None}}


def response_from(resp: dict, status: int = 200, items: list[dict] | None = None) -> NormalizedResponse:
    items = items if items is not None else [x for x in resp.get("output") or [] if isinstance(x, dict)]
    content: list[dict] = []
    for it in items:
        content.extend(item_block(it))
    stop = resp.get("status")
    if any(b.get("type") == "tool_use" and not b.get("hosted") for b in content):
        stop = "tool_use"
    return NormalizedResponse(content, stop, resp.get("model"), dict(resp.get("usage") or {}), status)


class ResponsesSSEReassembler:
    """Reensambla el stream SSE de la Responses API en paralelo, sin retener los bytes que se reenvían.

    La fuente de verdad son los `response.output_item.done` (ítems completos) y, si trae `output`, el
    `response.completed`. Los deltas solo sirven si el stream se corta antes del final.
    """

    FINAL = ("response.completed", "response.incomplete", "response.failed")

    def __init__(self) -> None:
        self._buf = b""
        self.items: dict[int, dict] = {}
        self._args: dict[int, str] = {}
        self._text: dict[int, str] = {}
        self.resp: dict = {}
        self.error: dict | None = None
        self.complete = False

    @property
    def has_content(self) -> bool:
        return bool(self.items)

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while True:
            cands = [(self._buf.find(s), len(s)) for s in (b"\n\n", b"\r\n\r\n")]
            cands = [c for c in cands if c[0] >= 0]
            if not cands:
                return
            idx, n = min(cands)
            ev, self._buf = self._buf[:idx], self._buf[idx + n:]
            self._event(ev)

    def _event(self, ev: bytes) -> None:
        data = b"\n".join(line[5:].lstrip() for line in ev.replace(b"\r\n", b"\n").split(b"\n")
                          if line.startswith(b"data:"))
        if not data or data == b"[DONE]":
            return
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            return
        t = d.get("type", "")
        i = d.get("output_index", 0)
        if t == "response.created" or t == "response.in_progress":
            self.resp.update(d.get("response") or {})
        elif t == "response.output_item.added" or t == "response.output_item.done":
            self.items[i] = dict(d.get("item") or {})
        elif t == "response.output_text.delta":
            self._text[i] = self._text.get(i, "") + d.get("delta", "")
        elif t == "response.function_call_arguments.delta":
            self._args[i] = self._args.get(i, "") + d.get("delta", "")
        elif t in self.FINAL:
            r = d.get("response") or {}
            self.resp.update(r)
            for k, it in enumerate(r.get("output") or []):
                if isinstance(it, dict):
                    self.items[k] = it
            if t == "response.failed":
                self.error = r.get("error") or d
            self.complete = True
        elif t == "error":
            self.error = d.get("error") or d

    def _finished_items(self) -> list[dict]:
        out = []
        for i in sorted(self.items):
            it = dict(self.items[i])
            if it.get("type") == "function_call" and not it.get("arguments") and i in self._args:
                it["arguments"] = self._args[i]
            if it.get("type") == "message" and not it.get("content") and i in self._text:
                it["content"] = [{"type": "output_text", "text": self._text[i]}]
            out.append(it)
        return out

    def result(self, status: int = 200) -> NormalizedResponse:
        return response_from(self.resp, status, self._finished_items())
