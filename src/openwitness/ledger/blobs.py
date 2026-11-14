"""BlobStore direccionado por contenido (SPEC §6.4, §10.1).

El payload de cada entrada vive aquí y la entrada solo guarda su hash. Borrar un blob (privacidad, retención)
no rompe la cadena: el verificador lo marca como `missing`.

Los blobs NO pasan por `canonical_json`: las peticiones reales al LLM traen floats (`temperature`, `top_p`), y
las reglas de §6.1 solo prohíben floats en estructuras firmadas. El hash se calcula sobre los bytes exactos
del blob, con claves ordenadas y sin espacios para que el mismo payload dé siempre los mismos bytes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..canonical import sha256_tagged


def encode_payload(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode_payload(data: bytes) -> Any:
    return json.loads(data)


class BlobStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, h: str) -> Path:
        hexd = h.removeprefix("sha256:")
        if len(hexd) != 64 or any(c not in "0123456789abcdef" for c in hexd):
            raise ValueError(f"hash de blob inválido: {h!r}")
        return self.root / hexd

    def put(self, data: bytes) -> str:
        h = sha256_tagged(data)
        p = self._path(h)
        if not p.exists():
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)
        return h

    def put_obj(self, obj: Any) -> str:
        return self.put(encode_payload(obj))

    def get(self, h: str) -> bytes | None:
        """None = borrado por privacidad (o nunca existió)."""
        p = self._path(h)
        return p.read_bytes() if p.exists() else None

    def get_obj(self, h: str) -> Any | None:
        b = self.get(h)
        return None if b is None else decode_payload(b)

    def delete(self, h: str) -> bool:
        p = self._path(h)
        if p.exists():
            p.unlink()
            return True
        return False
