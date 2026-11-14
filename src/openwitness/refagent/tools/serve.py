"""Servidores MCP de demo por stdio (SPEC §7.8, §10.3): `issues`, `mail`, `files`, `http_fetch`.

    uv run python -m openwitness.refagent.tools.serve issues
    uv run python -m openwitness.refagent.tools.serve mail     # nunca envía: escribe en OWIT_OUTBOX

Variables de entorno (opcionales):
- `OWIT_FIXTURES`: JSON o YAML con los `tool_fixtures` del escenario (mismo formato que el benchmark);
- `OWIT_OUTBOX`: archivo JSONL donde `mail` registra los correos (por defecto `data/outbox.jsonl`);
- `OWIT_SANDBOX`: carpeta donde `files` lee y escribe (por defecto, solo fixtures y memoria).

Para Claude Code (`.mcp.json`):
    {"mcpServers": {"issues": {"command": "uv", "args": ["run", "python", "-m",
                    "openwitness.refagent.tools.serve", "issues"]}}}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml
from mcp.server.mcpserver import MCPServer

from . import TOOLS, ToolContext


def load_fixtures() -> dict:
    p = os.environ.get("OWIT_FIXTURES")
    if not p or not Path(p).exists():
        return {}
    text = Path(p).read_text(encoding="utf-8")
    d = json.loads(text) if p.endswith(".json") else yaml.safe_load(text)
    return (d or {}).get("tool_fixtures", d) or {}


class SandboxContext(ToolContext):
    """`files` sobre una carpeta real (confinado a ella) y `mail` sobre un JSONL."""

    def __init__(self, fixtures: dict, sandbox: Path | None, outbox: Path):
        super().__init__(fixtures)
        self.sandbox = sandbox
        self.outbox_path = outbox

    def _safe(self, p: str) -> Path | None:
        if self.sandbox is None:
            return None
        q = (self.sandbox / p.lstrip("/~")).resolve()
        return q if q.is_relative_to(self.sandbox.resolve()) else None


def build(server: str) -> MCPServer:
    sandbox = Path(os.environ["OWIT_SANDBOX"]) if os.environ.get("OWIT_SANDBOX") else None
    outbox = Path(os.environ.get("OWIT_OUTBOX", "data/outbox.jsonl"))
    ctx = SandboxContext(load_fixtures(), sandbox, outbox)
    specs = [t for t in TOOLS if t.server == server]
    if not specs:
        raise SystemExit(f"servidor desconocido: {server} (issues | mail | files | http_fetch)")
    mcp = MCPServer(server, version="1.0.0", instructions=f"Servidor MCP de demo `{server}` (Open Witness).")

    def runner(spec):
        def call(**kwargs) -> str:
            if spec.server == "files" and ctx.sandbox is not None:
                p = ctx._safe(str(kwargs.get("path", "")))
                if p is None:
                    return "Error: ruta fuera del sandbox."
                if spec.name == "write_file":
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(str(kwargs.get("content", "")), encoding="utf-8")
                    return f"Escrito {kwargs.get('path')} en el sandbox."
                if p.exists():
                    return p.read_text(encoding="utf-8")
            n = len(ctx.outbox)
            out = spec.handler(kwargs, ctx)
            if len(ctx.outbox) > n:
                outbox.parent.mkdir(parents=True, exist_ok=True)
                with outbox.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ctx.outbox[-1], ensure_ascii=False) + "\n")
            return out
        return call

    for spec in specs:
        props = spec.input_schema["properties"]
        req = set(spec.input_schema.get("required", []))
        # MCPServer deriva el esquema de la firma: se genera una función con los parámetros del esquema
        params = ", ".join([f"{k}: str" for k in props if k in req] + [f"{k}: str = ''" for k in props if k not in req])
        ns: dict = {"_run": runner(spec)}
        exec(f"def {spec.name}({params}) -> str:\n    return _run(**{{k: v for k, v in locals().items() if v != ''}})",
             ns)
        mcp.add_tool(ns[spec.name], name=spec.name, description=spec.description)
    return mcp


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        raise SystemExit("uso: python -m openwitness.refagent.tools.serve <issues|mail|files|http_fetch>")
    build(argv[0]).run("stdio")


if __name__ == "__main__":
    main()
