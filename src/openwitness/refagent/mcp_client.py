"""Cliente MCP del agente de referencia, con `ManifestGate` (SPEC §6.3, §7.8).

Lanza los servidores de demo por stdio, lista sus herramientas y las expone al modelo como
`mcp__<servidor>__<herramienta>`. Si `openwitness.manifests` (Juliana) está disponible y se le pasa un
`TrustStore`, aplica el gate: las herramientas rechazadas no se ofrecen al modelo.

El benchmark usa por defecto `InProcessToolbox` (mismas herramientas, sin subprocesos); este cliente es el
camino "real" por MCP y se usa con `owit agent run --mcp`.
"""
from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ..types import ToolDef
from .tools import TOOLS

DEMO_SERVERS = ("issues", "mail", "files", "http_fetch")


class McpToolbox:
    def __init__(self, servers: tuple[str, ...] = DEMO_SERVERS, env: dict[str, str] | None = None,
                 gate: Any = None, builtins: bool = True):
        self.servers = servers
        self.env = {**os.environ, **(env or {})}
        self.gate = gate
        self.builtins = builtins
        self.sessions: dict[str, ClientSession] = {}
        self.defs: list[dict] = []
        self.decisions: list[dict] = []
        self._stack = AsyncExitStack()
        self._local = None

    async def __aenter__(self) -> "McpToolbox":
        seen_owner: dict[str, str] = {}
        for srv in self.servers:
            params = StdioServerParameters(command=sys.executable,
                                           args=["-m", "openwitness.refagent.tools.serve", srv], env=self.env)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            sess = await self._stack.enter_async_context(ClientSession(read, write))
            await sess.initialize()
            self.sessions[srv] = sess
            listed = await sess.list_tools()
            for t in listed.tools:
                live = ToolDef(t.name, t.description or "", t.input_schema or {})
                if self.gate is not None:
                    d = self.gate.evaluate_tool(srv, live, seen_owner)
                    self.decisions.append({"server": srv, "tool": t.name, "accepted": d.accepted, "reason": d.reason})
                    if not d.accepted:
                        continue
                self.defs.append({"name": f"mcp__{srv}__{t.name}", "description": live.description,
                                  "input_schema": live.input_schema})
        if self.builtins:
            from .tools import InProcessToolbox
            self._local = InProcessToolbox(tools=[t for t in TOOLS if t.server is None])
            self.defs += self._local.tool_defs()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._stack.aclose()

    def tool_defs(self) -> list[dict]:
        return list(self.defs)

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        if name.startswith("mcp__"):
            srv, _, tool = name[5:].partition("__")
            sess = self.sessions.get(srv)
            if sess is None:
                return f"Error: servidor MCP desconocido {srv}", True
            r = await sess.call_tool(tool, args or {})
            text = "\n".join(getattr(c, "text", "") for c in (getattr(r, "content", None) or []))
            return text, bool(getattr(r, "is_error", False) or getattr(r, "isError", False))
        if self._local is not None:
            return await self._local.call(name, args)
        return f"Error: herramienta desconocida {name}", True


def default_gate(manifest_dir: str | None = None):
    """El ManifestGate de Juliana si está en el repo; si no, None (las herramientas pasan como `unsigned`)."""
    try:
        from ..manifests import ManifestGate, TrustStore  # type: ignore[attr-defined]
    except ImportError:
        return None
    ts = TrustStore()
    if manifest_dir and os.path.isdir(manifest_dir):
        ts.load_dir(manifest_dir)
    return ManifestGate(ts)
