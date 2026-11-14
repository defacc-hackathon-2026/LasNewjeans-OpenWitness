"""Herramientas de demo del agente de referencia (SPEC §7.8, §10.3). Dueño: Andres.

Todo es sintético y local:
- `issues` (tipo Sentry): devuelve issues que pueden traer texto inyectado (lo fija el escenario);
- `mail`: **no envía nada**, solo registra en un outbox; solo acepta dominios reservados (`*.test`, `example.*`);
- `files`: lectura y escritura en un sandbox (por defecto, en memoria);
- `http_fetch`: solo hosts `*.test` / `localhost`, servidos desde fixtures;
- internas (`builtin:`) `Read` y `WebFetch`: el mismo contenido externo pero como herramientas del agente.

Las mismas definiciones sirven al backend en proceso (benchmark, rápido) y a los servidores MCP por stdio
(`python -m openwitness.refagent.tools.serve <servidor>`), que usa la prueba con Claude Code.

**Fixtures** (`tool_fixtures` del escenario): clave `"servidor.herramienta"` (p. ej. `issues.list_issues`) o el
nombre de la interna (`WebFetch`). El valor es un string, o un dict indexado por el primer argumento con `"*"`
como comodín.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

ALLOWED_MAIL = (".test", "example.com", "example.org", "example.net")


@dataclass
class ToolContext:
    fixtures: dict[str, Any] = field(default_factory=dict)
    outbox: list[dict] = field(default_factory=list)          # correos "enviados" (simulados)
    written: dict[str, str] = field(default_factory=dict)     # archivos escritos en el sandbox
    fetched: list[dict] = field(default_factory=list)         # peticiones HTTP simuladas

    def fixture(self, key: str, arg: Any, default: str) -> str:
        v = self.fixtures.get(key)
        if v is None:
            return default
        if isinstance(v, dict):
            return str(v.get(str(arg), v.get("*", default)))
        return str(v)


@dataclass
class ToolSpec:
    server: str | None                       # None = herramienta interna del agente
    name: str                                # nombre original (en el servidor)
    description: str
    input_schema: dict
    handler: Callable[[dict, ToolContext], str]

    @property
    def model_name(self) -> str:
        return f"mcp__{self.server}__{self.name}" if self.server else self.name

    @property
    def origin(self) -> str:
        return f"mcp:{self.server}" if self.server else f"builtin:{self.name}"

    @property
    def fixture_key(self) -> str:
        return f"{self.server}.{self.name}" if self.server else self.name

    def anthropic_def(self) -> dict:
        return {"name": self.model_name, "description": self.description, "input_schema": self.input_schema}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


S = {"type": "string"}


# ---------------------------------------------------------------- handlers

def list_issues(a: dict, ctx: ToolContext) -> str:
    p = a.get("project", "")
    return ctx.fixture("issues.list_issues", p, f"No hay issues abiertos en el proyecto {p}.")


def get_issue(a: dict, ctx: ToolContext) -> str:
    i = a.get("issue_id", "")
    return ctx.fixture("issues.get_issue", i, f"Issue {i}: no encontrado.")


def mail_allowed(to: str) -> bool:
    if "@" not in to:
        return False
    dom = to.rsplit("@", 1)[-1].strip().lower()
    return dom.endswith(".test") or any(dom == d or dom.endswith("." + d) for d in ALLOWED_MAIL[1:])


def send_email(a: dict, ctx: ToolContext) -> str:
    to = str(a.get("to", ""))
    if not all(mail_allowed(t.strip()) for t in to.split(",") if t.strip()) or not to.strip():
        return f"Error: destinatario no permitido en la demo ({to!r}); solo dominios reservados (.test, example.*)."
    ctx.outbox.append({"to": to, "subject": a.get("subject", ""), "body": a.get("body", "")})
    return ctx.fixture("mail.send_email", to, f"Correo registrado en el outbox para {to} (simulado: no se envió).")


def read_file(a: dict, ctx: ToolContext, key: str = "files.read_file") -> str:
    p = str(a.get("path", ""))
    if p in ctx.written:
        return ctx.written[p]
    return ctx.fixture(key, p, f"Error: {p} no existe en el sandbox.")


def write_file(a: dict, ctx: ToolContext) -> str:
    p = str(a.get("path", ""))
    ctx.written[p] = str(a.get("content", ""))
    return f"Escrito {p} ({len(ctx.written[p])} caracteres) en el sandbox."


def host_allowed(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return h == "localhost" or h.endswith(".test") or h in ("127.0.0.1",)


def fetch(a: dict, ctx: ToolContext, key: str = "http_fetch.fetch") -> str:
    url = str(a.get("url", ""))
    if not host_allowed(url):
        return f"Error: {url} no está permitido (la demo solo accede a hosts .test y localhost)."
    method = str(a.get("method", "GET")).upper()
    ctx.fetched.append({"url": url, "method": method, "body": a.get("body")})
    if method != "GET":
        return ctx.fixture(key, url, f"{method} {url}: 200 OK (simulado).")
    return ctx.fixture(key, url, f"GET {url}: 404 Not Found.")


TOOLS: list[ToolSpec] = [
    ToolSpec("issues", "list_issues", "Lista los issues abiertos de un proyecto (tracker de errores).",
             _obj({"project": S}, ["project"]), list_issues),
    ToolSpec("issues", "get_issue", "Devuelve el detalle de un issue por su id.",
             _obj({"issue_id": S}, ["issue_id"]), get_issue),
    ToolSpec("mail", "send_email", "Envía un correo electrónico.",
             _obj({"to": S, "subject": S, "body": S}, ["to", "body"]), send_email),
    ToolSpec("files", "read_file", "Lee un archivo del espacio de trabajo.",
             _obj({"path": S}, ["path"]), read_file),
    ToolSpec("files", "write_file", "Escribe un archivo en el espacio de trabajo.",
             _obj({"path": S, "content": S}, ["path", "content"]), write_file),
    ToolSpec("http_fetch", "fetch", "Hace una petición HTTP (GET por defecto; POST para enviar datos).",
             _obj({"url": S, "method": S, "body": S}, ["url"]), fetch),
    ToolSpec(None, "Read", "Lee un archivo local.", _obj({"path": S}, ["path"]),
             lambda a, c: read_file(a, c, "Read")),
    ToolSpec(None, "WebFetch", "Descarga una página web y devuelve su texto.", _obj({"url": S}, ["url"]),
             lambda a, c: fetch(a, c, "WebFetch")),
]

BY_MODEL_NAME = {t.model_name: t for t in TOOLS}


def list_emails(a: dict, ctx: ToolContext) -> str:
    q = a.get("query", "")
    return ctx.fixture("mail.list_emails", q or "*", "La bandeja de entrada está vacía.")


# Herramientas opcionales: no están en TOOLS (no cambian la lista de herramientas de los 35 escenarios ni los
# manifiestos de las pruebas con agentes reales). Un escenario las pide con `extra_tools: [clave]`.
EXTRA_TOOLS: dict[str, ToolSpec] = {
    "mail.list_emails": ToolSpec("mail", "list_emails",
                                 "Lista los correos de la bandeja de entrada (opcionalmente filtrados por `query`).",
                                 _obj({"query": S}, []), list_emails),
}


def tools_for(extra: list[str] | None = None) -> list[ToolSpec]:
    return TOOLS + [EXTRA_TOOLS[k] for k in extra or []]


class InProcessToolbox:
    """Backend en proceso: mismas herramientas, sin subprocesos (benchmark)."""

    def __init__(self, fixtures: dict | None = None, tools: list[ToolSpec] | None = None):
        self.ctx = ToolContext(dict(fixtures or {}))
        self.tools = tools or TOOLS
        self.by_name = {t.model_name: t for t in self.tools}

    def tool_defs(self) -> list[dict]:
        return [t.anthropic_def() for t in self.tools]

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        t = self.by_name.get(name)
        if t is None:
            return f"Error: herramienta desconocida {name}", True
        try:
            out = t.handler(args or {}, self.ctx)
        except Exception as e:   # una herramienta de demo nunca tumba al agente
            return f"Error: {type(e).__name__}: {e}", True
        return out, out.startswith("Error:")

    async def aclose(self) -> None:
        return None

    def side_effects(self) -> dict:
        return {"outbox": self.ctx.outbox, "written": self.ctx.written, "fetched": self.ctx.fetched}


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
