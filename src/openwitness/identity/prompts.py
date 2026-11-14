"""SignedPrompt: firma y verificación con anti-replay (SPEC §6.2, §7.2). Provisional (Adrian); dueña: Juliana."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime

from ..canonical import hash_obj, normalize_text, sha256_tagged
from ..timeutil import now_rfc3339
from .keys import DOMAIN_PROMPT, KeyPair, verify
from .registry import KeyRegistry


def content_hash(content: str) -> str:
    return sha256_tagged(normalize_text(content).encode("utf-8"))


def sign_prompt(kp: KeyPair, user_id: str, session_id: str, content: str,
                agent_session_id: str | None = None) -> dict:
    body = {"v": 1, "user_id": user_id, "session_id": session_id, "agent_session_id": agent_session_id,
            "nonce": base64.b64encode(os.urandom(16)).decode(), "ts": now_rfc3339(), "content": content,
            "content_hash": content_hash(content)}
    return {"body": body, "pubkey": kp.public_str(), "sig": kp.sign(DOMAIN_PROMPT, body)}


@dataclass
class VerifyResult:
    ok: bool
    reason: str = "ok"   # ok | bad_sig | unknown_key | content_hash | replay | malformed

    def __bool__(self) -> bool:
        return self.ok


def _is_utc_timestamp(ts: str) -> bool:
    """RFC 3339 con zona horaria (§6.1). Sin zona no se puede comparar con la fecha de revocación."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def verify_prompt(sp: dict, registry: KeyRegistry, seen_nonces: set[str]) -> VerifyResult:
    try:
        body, pubkey, sig = sp["body"], sp["pubkey"], sp["sig"]
        user_id, ts, nonce = body["user_id"], body["ts"], body["nonce"]
    except (KeyError, TypeError):
        return VerifyResult(False, "malformed")
    # Tipos: un cuerpo bien firmado pero con tipos raros no debe lanzar excepciones más abajo.
    if not all(isinstance(x, str) for x in (pubkey, sig, user_id, ts, nonce, body.get("content", ""),
                                            body.get("content_hash", ""))):
        return VerifyResult(False, "malformed")
    if not _is_utc_timestamp(ts):
        return VerifyResult(False, "malformed")
    if not verify(DOMAIN_PROMPT, body, sig, pubkey):
        return VerifyResult(False, "bad_sig")
    if not registry.is_valid(user_id, pubkey, ts):
        return VerifyResult(False, "unknown_key")
    if body.get("content_hash") != content_hash(body.get("content", "")):
        return VerifyResult(False, "content_hash")
    if nonce in seen_nonces:
        return VerifyResult(False, "replay")
    seen_nonces.add(nonce)
    return VerifyResult(True)


def prompt_hash(sp: dict) -> str:
    return hash_obj(sp)
