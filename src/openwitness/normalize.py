"""Normalización de bloques y mensajes para comparar historiales (SPEC §6.1).

La usan el adaptador Anthropic (al normalizar peticiones y respuestas), el `Decomposer` y la comprobación 10
de `verify_ledger`. Tiene que ser idéntica en los tres sitios: por eso vive aquí y no en el adaptador.
"""
from __future__ import annotations

from typing import Any

from .canonical import normalize_text

KEEP = ("type", "text", "id", "name", "input", "tool_use_id", "content", "is_error", "thinking", "tool_name")


def norm_block(b: Any) -> dict:
    if isinstance(b, str):
        return {"type": "text", "text": b}
    out = {k: b[k] for k in KEEP if k in b}
    if out.get("type") == "tool_result":
        c = out.get("content")
        if c is None:
            out["content"] = []
        elif isinstance(c, str):
            out["content"] = [{"type": "text", "text": c}]
        else:
            out["content"] = [norm_block(x) for x in c]
        out["is_error"] = bool(out.get("is_error", False))
    return out


def norm_content(c: Any) -> list[dict]:
    if c is None:
        return []
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [norm_block(b) for b in c]


def norm_message(m: dict) -> dict:
    return {"role": m["role"], "content": norm_content(m.get("content"))}


def system_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "\n".join(b.get("text", "") for b in system if isinstance(b, dict))


def block_text(b: dict) -> str:
    """Texto visible de un bloque (para emparejar prompts y buscar argumentos)."""
    t = b.get("type")
    if t == "text":
        return b.get("text", "")
    if t == "tool_result":
        return "\n".join(block_text(x) for x in b.get("content", []))
    if t == "tool_reference":
        return b.get("tool_name", "")
    return ""


def message_text(m: dict) -> str:
    return normalize_text("\n".join(block_text(b) for b in m.get("content", [])))
