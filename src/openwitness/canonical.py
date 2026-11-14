"""JSON canónico, hashes y normalización de texto (SPEC §6.1, §7.1).

Versión provisional escrita por Adrian para no bloquear la bitácora; respeta el contrato de §7.1.
Dueña: Juliana. Si su versión cambia algo, manda la suya mientras respete el contrato.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

ZERO_HASH = "sha256:" + "0" * 64


class CanonicalError(ValueError):
    """La estructura no se puede firmar (p. ej. contiene floats)."""


def _check(obj: Any, path: str = "$") -> None:
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return
    if isinstance(obj, float):
        raise CanonicalError(f"float no permitido en estructura firmada: {path}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalError(f"clave no string en {path}")
            _check(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check(v, f"{path}[{i}]")
        return
    raise CanonicalError(f"tipo no serializable en {path}: {type(obj).__name__}")


def canonical_json(obj: Any) -> bytes:
    """Claves ordenadas, sin espacios, UTF-8, sin floats."""
    _check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_tagged(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_obj(obj: Any) -> str:
    return sha256_tagged(canonical_json(obj))


def normalize_text(s: str) -> str:
    """NFC, finales de línea \\n, sin espacios al inicio ni al final."""
    return unicodedata.normalize("NFC", s.replace("\r\n", "\n").replace("\r", "\n")).strip()
