"""Anclas externas (SPEC §6.7). `MockAnchor` para la demo offline y los tests; Rekor queda para G11.

Cada envío incluye el `session_id`, para que el verificador pueda listar desde el ancla todas las cabezas
publicadas de una sesión sin fiarse de lo que diga la bitácora (§7.4, comprobación 7; hallazgo H1).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol

from ..identity import DOMAIN_ANCHOR, KeyPair, verify
from ..timeutil import now_rfc3339
from ..types import AnchorReceipt


def submission_body(head_hash: str, session_id: str) -> dict:
    """Lo que firma el gateway al anclar una cabeza."""
    return {"session_id": session_id, "head_hash": head_hash}


class Anchor(Protocol):
    name: str

    def submit(self, head_hash: str, sig: str, pubkey: str, session_id: str) -> AnchorReceipt: ...

    def check(self, receipt: AnchorReceipt, head_hash: str) -> bool: ...

    def heads_for(self, session_id: str, pubkey: str) -> list[str]: ...


class AnchorRejected(Exception):
    pass


class MockAnchor:
    """Log append-only en un archivo aparte, firmado con una llave que no es la del gateway.

    Modela un servicio externo (Rekor, TSA): el operador del gateway no puede reescribirlo. En la demo vive
    en el mismo disco; el modelo de amenazas supone que en producción no.
    """

    name = "mock"

    def __init__(self, path: Path | str, key: KeyPair | None = None, key_path: Path | str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        kp = Path(key_path) if key_path else self.path.with_suffix(".key")
        if key is not None:
            self.kp = key
        elif kp.exists():
            self.kp = KeyPair.load(kp)
        else:
            self.kp = KeyPair.generate()
            self.kp.save(kp)
        self._lock = threading.Lock()

    def _lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def submit(self, head_hash: str, sig: str, pubkey: str, session_id: str) -> AnchorReceipt:
        if not verify(DOMAIN_ANCHOR, submission_body(head_hash, session_id), sig, pubkey):
            raise AnchorRejected("firma del gateway inválida sobre la cabeza")
        with self._lock:
            index = len(self._lines())
            body = {"index": index, "session_id": session_id, "head_hash": head_hash, "submitter": pubkey,
                    "submitter_sig": sig, "integrated_time": now_rfc3339()}
            rec = AnchorReceipt(self.name, body, self.kp.sign(DOMAIN_ANCHOR, body), self.kp.public_str())
            with self.path.open("a") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
            return rec

    def check(self, receipt: AnchorReceipt, head_hash: str) -> bool:
        if receipt.body.get("head_hash") != head_hash or receipt.anchor_pubkey != self.kp.public_str():
            return False
        if not verify(DOMAIN_ANCHOR, receipt.body, receipt.sig, receipt.anchor_pubkey):
            return False
        lines = self._lines()
        i = receipt.body.get("index", -1)
        return 0 <= i < len(lines) and lines[i] == receipt.to_dict()

    def heads_for(self, session_id: str, pubkey: str) -> list[str]:
        out = []
        for line in self._lines():
            b = line["body"]
            if b["session_id"] == session_id and b["submitter"] == pubkey and verify(
                    DOMAIN_ANCHOR, line["body"], line["sig"], line["anchor_pubkey"]):
                out.append(b["head_hash"])
        return out
