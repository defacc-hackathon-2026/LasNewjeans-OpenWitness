"""Emparejar el texto del rol usuario con los `SignedPrompt` ya registrados (SPEC §6.5 paso 4, §12.4)."""
from __future__ import annotations

from ..canonical import normalize_text


def find_prompt(text: str, prompts: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Devuelve (contenido, hash de entrada) del prompt firmado más largo contenido en `text`."""
    t = normalize_text(text)
    best = None
    for content, h in prompts:
        if content and content in t and (best is None or len(content) > len(best[0])):
            best = (content, h)
    return best


def remainder(text: str, content: str) -> str:
    """Texto del bloque que no cubre el prompt firmado (lo que el agente agregó alrededor)."""
    return normalize_text(normalize_text(text).replace(content, "", 1))


AGENT_MARKERS = ("<system-reminder>", "<command-name>", "<local-command-stdout>", "<command-message>")


def looks_agent_injected(text: str) -> bool:
    t = normalize_text(text)
    return t.startswith(AGENT_MARKERS)
