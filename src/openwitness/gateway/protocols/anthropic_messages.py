"""Protocolo Anthropic Messages: Claude Code, OpenCode y refagent (SPEC §5.4, §7.5)."""
from __future__ import annotations

import copy
import json
from typing import Any

from ...normalize import norm_block, norm_content, norm_message, system_text
from ...types import Message, NormalizedRequest, NormalizedResponse, ToolDef

PROTOCOL = "anthropic_messages"
MCP_PREFIX = "mcp__"


def blocked_reference_text(tool_name: str) -> str:
    return f"Open Witness bloqueó la herramienta {tool_name}: su manifiesto no es válido."


class AnthropicMessagesAdapter:
    protocol = PROTOCOL

    def parse_request(self, body: dict) -> NormalizedRequest:
        msgs = [Message.from_dict(norm_message(m)) for m in body.get("messages") or []]
        tools = [ToolDef.from_dict(t) for t in body.get("tools") or [] if isinstance(t, dict)]
        return NormalizedRequest(PROTOCOL, system_text(body.get("system")), msgs, tools, body.get("model"),
                                 bool(body.get("stream")))

    @staticmethod
    def tool_origin(tool_name: str) -> tuple[str, str]:
        """"mcp__sentry__list_issues" -> ("mcp:sentry", "list_issues"); "Bash" -> ("builtin:Bash", "Bash")."""
        if tool_name.startswith(MCP_PREFIX):
            rest = tool_name[len(MCP_PREFIX):]
            server, sep, tool = rest.partition("__")
            if sep and server and tool:
                return f"mcp:{server}", tool
        return f"builtin:{tool_name}", tool_name

    def strip_tools(self, body: dict, rejected: set[str]) -> dict:
        """Copia de la petición sin las herramientas rechazadas y sin `tool_reference` a ellas (§6.3)."""
        if not rejected:
            return body
        out = copy.copy(body)
        if isinstance(body.get("tools"), list):
            out["tools"] = [t for t in body["tools"] if not (isinstance(t, dict) and t.get("name") in rejected)]
        msgs = body.get("messages")
        if isinstance(msgs, list):
            out["messages"] = [self._strip_refs(m, rejected) for m in msgs]
        tc = body.get("tool_choice")
        if isinstance(tc, dict) and tc.get("type") == "tool" and tc.get("name") in rejected:
            out["tool_choice"] = {"type": "auto"}
        return out

    @staticmethod
    def _strip_refs(m: dict, rejected: set[str]) -> dict:
        c = m.get("content")
        if not isinstance(c, list):
            return m
        changed = False
        new_blocks = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                inner = []
                for x in b["content"]:
                    if isinstance(x, dict) and x.get("type") == "tool_reference" and x.get("tool_name") in rejected:
                        inner.append({"type": "text", "text": blocked_reference_text(x["tool_name"])})
                        changed = True
                    else:
                        inner.append(x)
                b = dict(b, content=inner)
            new_blocks.append(b)
        return dict(m, content=new_blocks) if changed else m

    @staticmethod
    def referenced_tools(body: dict) -> set[str]:
        """Nombres de herramientas que aparecen en bloques `tool_reference` (carga diferida, §5.3)."""
        out: set[str] = set()
        for m in body.get("messages") or []:
            content = m.get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                    for x in b["content"]:
                        if isinstance(x, dict) and x.get("type") == "tool_reference":
                            out.add(x.get("tool_name", ""))
        return out

    @staticmethod
    def parse_json_response(data: dict, status: int = 200) -> NormalizedResponse:
        return NormalizedResponse(norm_content(data.get("content")), data.get("stop_reason"), data.get("model"),
                                  data.get("usage") or {}, status)


class SSEReassembler:
    """Reensambla el stream SSE de Anthropic en paralelo, sin retener los bytes que se reenvían."""

    def __init__(self) -> None:
        self._buf = b""
        self.blocks: dict[int, dict] = {}
        self._partial_json: dict[int, str] = {}
        self.stop_reason: str | None = None
        self.model: str | None = None
        self.usage: dict[str, Any] = {}
        self.error: dict | None = None
        self.complete = False

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while True:
            sep = self._next_sep()
            if sep is None:
                return
            idx, n = sep
            ev, self._buf = self._buf[:idx], self._buf[idx + n:]
            self._event(ev)

    def _next_sep(self) -> tuple[int, int] | None:
        cands = [(self._buf.find(s), len(s)) for s in (b"\n\n", b"\r\n\r\n")]
        cands = [c for c in cands if c[0] >= 0]
        return min(cands) if cands else None

    def _event(self, ev: bytes) -> None:
        data = b"\n".join(line[5:].lstrip() for line in ev.replace(b"\r\n", b"\n").split(b"\n")
                          if line.startswith(b"data:"))
        if not data:
            return
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            return
        t = d.get("type")
        if t == "message_start":
            msg = d.get("message") or {}
            self.model = msg.get("model")
            self.usage.update(msg.get("usage") or {})
        elif t == "content_block_start":
            self.blocks[d["index"]] = dict(d.get("content_block") or {})
            self._partial_json[d["index"]] = ""
        elif t == "content_block_delta":
            b = self.blocks.setdefault(d["index"], {})
            dl = d.get("delta") or {}
            dt = dl.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + dl.get("text", "")
            elif dt == "input_json_delta":
                self._partial_json[d["index"]] = self._partial_json.get(d["index"], "") + dl.get("partial_json", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + dl.get("thinking", "")
            elif dt == "signature_delta":
                b["signature"] = b.get("signature", "") + dl.get("signature", "")
        elif t == "content_block_stop":
            b = self.blocks.get(d["index"])
            if b is not None and b.get("type") in ("tool_use", "server_tool_use"):
                raw = self._partial_json.get(d["index"], "")
                if raw:
                    try:
                        b["input"] = json.loads(raw)
                    except json.JSONDecodeError:
                        b["input"] = {"_unparsed": raw}
                else:
                    b.setdefault("input", {})
        elif t == "message_delta":
            self.stop_reason = (d.get("delta") or {}).get("stop_reason", self.stop_reason)
            self.usage.update(d.get("usage") or {})
        elif t == "message_stop":
            self.complete = True
        elif t == "error":
            self.error = d.get("error") or d

    def response(self, status: int = 200) -> NormalizedResponse:
        content = [norm_block(self.blocks[i]) for i in sorted(self.blocks)]
        return NormalizedResponse(content, self.stop_reason, self.model, dict(self.usage), status)
