"""KeyPair Ed25519 con separación de dominio (SPEC §6.1, §7.2).

Provisional (Adrian) según el contrato de §7.2. Dueña: Juliana.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from ..canonical import canonical_json

DOMAIN_PROMPT = "openwitness/prompt/v1"
DOMAIN_MANIFEST = "openwitness/manifest/v1"
DOMAIN_LEDGER = "openwitness/ledger/v1"
DOMAIN_RECEIPT = "openwitness/receipt/v1"
DOMAIN_ANCHOR = "openwitness/anchor/v1"


def _message(domain: str, obj: Any) -> bytes:
    return domain.encode() + b"\x00" + hashlib.sha256(canonical_json(obj)).digest()


class KeyPair:
    def __init__(self, sk: SigningKey):
        self._sk = sk

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(SigningKey.generate())

    @classmethod
    def load(cls, path: Path | str) -> "KeyPair":
        return cls(SigningKey(base64.b64decode(Path(path).read_text().strip())))

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(base64.b64encode(bytes(self._sk)).decode() + "\n")
        os.chmod(p, 0o600)

    def public_str(self) -> str:
        return "ed25519:" + base64.b64encode(bytes(self._sk.verify_key)).decode()

    def sign(self, domain: str, obj: Any) -> str:
        return base64.b64encode(self._sk.sign(_message(domain, obj)).signature).decode()


def verify(domain: str, obj: Any, sig: str, pubkey: str) -> bool:
    try:
        if not pubkey.startswith("ed25519:"):
            return False
        vk = VerifyKey(base64.b64decode(pubkey.removeprefix("ed25519:")))
        vk.verify(_message(domain, obj), base64.b64decode(sig))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False
    except Exception:  # noqa: BLE001 - firma o llave mal formadas
        return False
