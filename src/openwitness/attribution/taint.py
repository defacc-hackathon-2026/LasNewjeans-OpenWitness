"""Procedencia de argumentos (taint simple, SPEC §7.9 paso 3).

Se extraen los argumentos sensibles de la acción y se busca, de forma normalizada, en qué fragmentos del
contexto aparece cada uno.

- **Decisivos** (los usan las reglas): destinatario, URL, ruta, comando y monto, según el nombre del argumento,
  más cualquier argumento cuyo valor completo sea un correo o una URL.
- **Informativos** (solo van a `arg_provenance`): correos y URLs dentro de texto libre (`body`, `content`…) y los
  strings largos (≥ 40 caracteres). El texto libre suele redactarlo el modelo, así que no decide.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..llm.naive import EMAIL_RE, URL_RE, find_emails, find_paths, find_urls
from ..types import EvidenceItem

EMAIL_KEYS = {"to", "cc", "bcc", "recipient", "recipients", "email", "address"}
URL_KEYS = {"url", "endpoint", "webhook", "href", "link", "uri"}
PATH_KEYS = {"path", "file", "filename", "filepath", "dest", "destination", "target"}
COMMAND_KEYS = {"command", "cmd", "script"}
AMOUNT_KEYS = {"amount", "price", "total"}
LONG_TEXT = 40


@dataclass
class SensitiveArg:
    name: str          # ruta del argumento: "to", "body", "items[0].url"
    kind: str          # email | url | path | command | amount | text
    value: str         # normalizado
    decisive: bool


def norm_email(s: str) -> str:
    return s.strip().strip("<>").lower()


def norm_url(s: str) -> str:
    try:
        u = urlsplit(s.strip())
    except ValueError:
        return s.strip().lower()
    path = u.path.rstrip("/")
    q = f"?{u.query}" if u.query else ""
    return f"{u.scheme.lower()}://{(u.netloc or '').lower()}{path}{q}"


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _leaves(obj, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _leaves(v, f"{prefix}[{i}]")
    elif obj is not None and not isinstance(obj, bool):
        yield prefix, obj


def extract_sensitive(args: dict) -> list[SensitiveArg]:
    out: list[SensitiveArg] = []
    for name, v in _leaves(args or {}):
        key = re.split(r"[.\[]", name)[-1].lower() if name else ""
        s = str(v).strip()
        if not s:
            continue
        if isinstance(v, (int, float)) or key in AMOUNT_KEYS:
            if key in AMOUNT_KEYS:
                out.append(SensitiveArg(name, "amount", norm_text(s), True))
            continue
        if EMAIL_RE.fullmatch(s) or (key in EMAIL_KEYS and find_emails(s)):
            out += [SensitiveArg(name, "email", norm_email(e), True) for e in (find_emails(s) or [s])]
        elif URL_RE.fullmatch(s) or (key in URL_KEYS and find_urls(s)):
            out += [SensitiveArg(name, "url", norm_url(u), True) for u in (find_urls(s) or [s])]
        elif key in URL_KEYS:
            out.append(SensitiveArg(name, "url", norm_url(s), True))
        elif key in PATH_KEYS:
            out.append(SensitiveArg(name, "path", s, True))
        elif key in COMMAND_KEYS:
            out.append(SensitiveArg(name, "command", norm_text(s), True))
        else:   # texto libre: informativo
            out += [SensitiveArg(name, "email", norm_email(e), False) for e in find_emails(s)]
            out += [SensitiveArg(name, "url", norm_url(u), False) for u in find_urls(s)]
            if len(s) >= LONG_TEXT:
                out.append(SensitiveArg(name, "text", norm_text(s), False))
    return out


def appears_in(arg: SensitiveArg, text: str) -> bool:
    if not text:
        return False
    if arg.kind == "email":
        return arg.value in {norm_email(e) for e in find_emails(text)}
    if arg.kind == "url":
        found = {norm_url(u) for u in find_urls(text)}
        return arg.value in found or any(f and arg.value.startswith(f) for f in found)
    if arg.kind == "path":
        p = arg.value
        if p in text:
            return True
        return p in set(find_paths(text))
    return arg.value in norm_text(text)


def occurrences(arg: SensitiveArg, items: list[EvidenceItem]) -> list[EvidenceItem]:
    return [i for i in items if appears_in(arg, i.fragment)]
