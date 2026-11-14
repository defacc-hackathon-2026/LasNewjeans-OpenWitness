"""Modelo falso "ingenuo" para el benchmark de atribución (SPEC §7.7, §9.2). Dueño: Andres.

Es un `Responder` (`body -> list[bloques]`) para `FakeUpstream`. Sigue un plan por pasos como
`ScriptedResponder`, pero **obedece la última instrucción que ve**: los argumentos pueden llevar marcadores que
se resuelven con lo que hay en el contexto de *esa* petición, no con valores fijos del guion:

| Marcador | Valor |
|---|---|
| `{last_email}` / `{last_url}` / `{last_path}` | el último correo / URL / ruta en el contexto (usuario o herramienta) |
| `{user_email}` / `{user_url}` / `{user_path}` | el último que aparece en texto del usuario |
| `{tool_email}` / `{tool_url}` / `{tool_path}` | el último que aparece en un resultado de herramienta |
| `{last_tool_text}` | los primeros 300 caracteres del último resultado de herramienta |

Así, si una herramienta inyecta un destinatario, el modelo lo usa porque lo leyó, y la atribución tiene que
descubrirlo desde la bitácora (el guion no le dice a nadie de dónde salió el dato).
"""
from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
PATH_RE = re.compile(r"(?<![\w:/.@])(~?/(?:[\w.\-]+/)*[\w.\-]+)")


def _strip_trailing(s: str) -> str:
    return s.rstrip(".,;:!?")


def find_emails(text: str) -> list[str]:
    return [_strip_trailing(m) for m in EMAIL_RE.findall(text or "")]


def find_urls(text: str) -> list[str]:
    return [_strip_trailing(m) for m in URL_RE.findall(text or "")]


def find_paths(text: str) -> list[str]:
    t = URL_RE.sub(" ", text or "")
    t = EMAIL_RE.sub(" ", t)
    return [_strip_trailing(m) for m in PATH_RE.findall(t)]


def _block_text(b: dict) -> str:
    if b.get("type") == "text":
        return b.get("text", "")
    if b.get("type") == "tool_result":
        c = b.get("content")
        if isinstance(c, str):
            return c
        return "\n".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
    return ""


def context_texts(body: dict) -> list[tuple[str, str]]:
    """[(fuente, texto)] en orden: fuente es "user" o "tool". Ignora system y asistente."""
    out = []
    for m in body.get("messages") or []:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        blocks = [{"type": "text", "text": c}] if isinstance(c, str) else (c or [])
        for b in blocks:
            if b.get("type") == "tool_result":
                out.append(("tool", _block_text(b)))
            elif b.get("type") == "text" and not b.get("text", "").lstrip().startswith("<system-reminder>"):
                out.append(("user", b.get("text", "")))
    return out


def resolve_markers(value: Any, body: dict) -> Any:
    if isinstance(value, dict):
        return {k: resolve_markers(v, body) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_markers(v, body) for v in value]
    if not isinstance(value, str) or "{" not in value:
        return value
    texts = context_texts(body)
    finders = {"email": find_emails, "url": find_urls, "path": find_paths}

    def last(kind: str, src: str | None) -> str:
        for s, t in reversed(texts):
            if src and s != src:
                continue
            found = finders[kind](t)
            if found:
                return found[-1]
        return ""

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key == "last_tool_text":
            tool = [t for s, t in texts if s == "tool"]
            return tool[-1][:300] if tool else ""
        scope, _, kind = key.partition("_")
        if kind not in finders or scope not in ("last", "user", "tool"):
            return m.group(0)
        return last(kind, None if scope == "last" else scope)

    return re.sub(r"\{(\w+)\}", sub, value)


class NaiveResponder:
    """Plan por pasos (paso k = petición con k mensajes de asistente previos), con marcadores resueltos."""

    def __init__(self, steps: list[dict]):
        self.steps = steps

    def __call__(self, body: dict) -> list[dict]:
        k = sum(1 for m in body.get("messages") or [] if m.get("role") == "assistant")
        if k >= len(self.steps):
            return [{"type": "text", "text": "Listo."}]
        st = self.steps[k]
        blocks: list[dict] = []
        if "text" in st:
            blocks.append({"type": "text", "text": resolve_markers(st["text"], body)})
        for tu in st.get("tool_uses", []) + ([st["tool_use"]] if "tool_use" in st else []):
            blocks.append({"type": "tool_use", "name": tu["name"], "input": resolve_markers(tu.get("input", {}), body)})
        return blocks or [{"type": "text", "text": "Listo."}]
