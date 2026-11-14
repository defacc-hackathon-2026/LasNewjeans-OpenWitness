"""`verify_ledger`: las 10 comprobaciones de SPEC §7.4.

Todo lo que se comprueba aquí es determinista. No atribuye nada: solo dice si la evidencia es íntegra y
hasta qué entrada (la atribución, §7.9, no atribuye sobre evidencia corrupta).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..canonical import hash_obj, normalize_text, sha256_tagged
from ..identity import DOMAIN_LEDGER, DOMAIN_RECEIPT, KeyRegistry, verify, verify_prompt
from ..normalize import block_text
from ..types import AnchorReceipt, IntegrityError, IntegrityReport, LedgerRecord, Receipt
from .anchor import Anchor
from .blobs import BlobStore, decode_payload
from .ledger import GENESIS, load_records

# Tipos de entrada compatibles con cada rol de mensaje en la comprobación 10.
ROLE_KINDS = {
    "user": {"user_prompt", "user_text_unsigned", "tool_result"},
    "system": {"user_text_unsigned"},
    "assistant": {"llm_response", "tool_call", "context_reset"},
}


def _as_receipt(r: Receipt | dict) -> Receipt:
    return r if isinstance(r, Receipt) else Receipt.from_dict(r)


def verify_ledger(path: Path | str, blobs: BlobStore, gateway_pubkey: str, registry: KeyRegistry,
                  anchor: Anchor | None, receipts: Iterable[Receipt | dict] = ()) -> IntegrityReport:
    rep = IntegrityReport(ok=True)

    def err(seq: int | None, error: str, detail: str = "", check: int = 0) -> None:
        rep.errors.append(IntegrityError(seq, error, detail, check))

    try:
        recs = load_records(path)
    except Exception as e:  # noqa: BLE001 - archivo corrupto o truncado a mitad de línea
        return IntegrityReport(ok=False, errors=[IntegrityError(None, "unparseable", str(e), 0)])
    rep.entries = len(recs)
    if not recs:
        return IntegrityReport(ok=False, errors=[IntegrityError(None, "empty_ledger", "", 3)])

    session_id = recs[0].entry.get("session_id")
    rep.session_id = session_id
    index_of: dict[str, int] = {}            # hash -> posición (solo entradas ya vistas)
    payloads: dict[int, Any] = {}            # posición -> payload decodificado (si el blob está y coincide)
    accepted: dict[str, set[str | None]] = {}  # herramienta -> manifest_hash aceptados (comprobación 6)
    builtin_allowlist: set[str] | None = None
    seen_nonces: set[str] = set()
    prev = GENESIS

    for i, r in enumerate(recs):
        e = r.entry
        # 1. hash
        try:
            if hash_obj(e) != r.hash:
                err(i, "hash_mismatch", check=1)
        except Exception as ex:  # noqa: BLE001 - p. ej. un float insertado en la entrada
            err(i, "entry_not_canonical", str(ex), check=1)
        # 2. firma del gateway
        if not verify(DOMAIN_LEDGER, e, r.sig, gateway_pubkey):
            err(i, "bad_signature", check=2)
        # 3. cadena y secuencia
        if e.get("seq") != i:
            err(i, "seq_gap", f"esperado {i}, encontrado {e.get('seq')}", check=3)
        if e.get("prev") != prev:
            err(i, "broken_chain", check=3)
        if e.get("session_id") != session_id:
            err(i, "session_mismatch", str(e.get("session_id")), check=3)
        if i == 0:
            if e.get("kind") != "session_start":
                err(0, "no_genesis", "la primera entrada no es session_start", check=3)
            else:
                gp = (e.get("meta") or {}).get("gateway_pubkey")
                if gp and gp != gateway_pubkey:
                    err(0, "gateway_pubkey_mismatch", gp, check=2)
                pol = (e.get("meta") or {}).get("policy") or {}
                if pol.get("builtin_allowlist") is not None:
                    builtin_allowlist = set(pol["builtin_allowlist"])
        # 4. contexto: solo entradas anteriores de esta sesión
        for c in e.get("context") or []:
            if c not in index_of:
                err(i, "context_ref_invalid", c, check=4)
        # 9. blob
        ph = e.get("payload_hash")
        if ph:
            try:
                b = blobs.get(ph)
            except ValueError:
                b = None
                err(i, "bad_payload_hash", ph, check=9)
            if b is None:
                rep.blob_status[i] = "missing"
            elif sha256_tagged(b) != ph:
                rep.blob_status[i] = "mismatch"
                err(i, "blob_mismatch", check=9)
            else:
                rep.blob_status[i] = "present"
                try:
                    payloads[i] = decode_payload(b)
                except ValueError:
                    err(i, "blob_unparseable", check=9)
        kind, meta = e.get("kind"), e.get("meta") or {}
        payload = payloads.get(i)
        # 5. prompts firmados
        if kind == "user_prompt" and payload is not None:
            vr = verify_prompt(payload, registry, seen_nonces)
            if not vr.ok:
                err(i, "prompt_" + vr.reason, check=5)
            else:
                body = payload["body"]
                if body.get("session_id") != session_id:
                    err(i, "prompt_wrong_session", str(body.get("session_id")), check=5)
                if meta.get("user_id") not in (None, body.get("user_id")):
                    err(i, "prompt_user_mismatch", f"{meta.get('user_id')} != {body.get('user_id')}", check=5)
        # 6. herramientas aceptadas
        if kind == "manifest_decision" and meta.get("decision") == "accepted":
            accepted.setdefault(meta.get("tool", ""), set()).add(meta.get("manifest_hash"))
            if (meta.get("reason") == "builtin" and builtin_allowlist is not None
                    and meta.get("tool") not in builtin_allowlist):
                err(i, "builtin_not_allowlisted", meta.get("tool", ""), check=6)
        if kind == "tool_call" and not meta.get("anomaly"):
            tool, origin = meta.get("tool", ""), meta.get("origin", "")
            if tool not in accepted:
                err(i, "tool_call_not_accepted", tool, check=6)
            elif origin.startswith("mcp:") and meta.get("manifest_hash") not in accepted[tool]:
                err(i, "tool_call_unaccepted_manifest", tool, check=6)
        # 10. coherencia del llm_request con su contexto
        if kind == "llm_request" and payload is not None:
            for msg in _check_request(payload, e.get("context") or [], meta, index_of, recs, payloads):
                err(i, "llm_request_incoherent", msg, check=10)
        # 7a. entradas anchor dentro de la bitácora
        if kind == "anchor":
            hs, hh = meta.get("head_seq"), meta.get("head_hash")
            if anchor is None:
                err(i, "anchor_unverifiable", "no se aportó el backend del ancla", check=7)
            elif payload is None:
                err(i, "anchor_receipt_missing", check=7)
            else:
                try:
                    ok = anchor.check(AnchorReceipt.from_dict(payload), hh)
                except (KeyError, TypeError):
                    ok = False
                if not ok:
                    err(i, "anchor_invalid", check=7)
                elif not isinstance(hs, int) or hs >= i or recs[hs].hash != hh:
                    err(i, "anchor_head_mismatch", f"head_seq={hs}", check=7)
                else:
                    rep.last_anchored_seq = max(rep.last_anchored_seq, hs)
        index_of[r.hash] = i
        prev = r.hash

    # 7b. toda cabeza anclada para esta sesión y esta llave existe en la bitácora (hallazgo H1)
    if anchor is not None:
        pos = {r.hash: i for i, r in enumerate(recs)}
        recorded = {(r.entry.get("meta") or {}).get("head_hash") for r in recs if r.entry.get("kind") == "anchor"}
        for h in anchor.heads_for(session_id, gateway_pubkey):
            if h not in pos:
                err(None, "anchored_head_missing", h, check=7)
            else:
                rep.last_anchored_seq = max(rep.last_anchored_seq, pos[h])
                if h not in recorded:   # se borró la entrada anchor (p. ej. la última de la sesión)
                    err(pos[h], "anchor_entry_missing", h, check=7)

    # 8. recibos del usuario
    for rc in receipts:
        rc = _as_receipt(rc)
        b = rc.body
        if b.get("session_id") != session_id:
            continue
        seq = b.get("seq")
        if not verify(DOMAIN_RECEIPT, b, rc.sig, gateway_pubkey):
            err(seq, "receipt_bad_sig", check=8)
        elif not isinstance(seq, int) or seq >= len(recs):
            err(None, "receipt_entry_missing", f"seq={seq}", check=8)
        elif recs[seq].hash != b.get("entry_hash") or (
                b.get("prompt_hash") and recs[seq].payload_hash != b.get("prompt_hash")):
            err(seq, "receipt_contradicts_ledger", check=8)

    rep.unanchored_tail = sum(1 for r in recs[rep.last_anchored_seq + 1:] if r.entry.get("kind") != "anchor")
    rep.ok = not rep.errors
    return rep


# ---------------------------------------------------------------------- comprobación 10

def _text_blocks(m: dict) -> str:
    return normalize_text("\n".join(b.get("text", "") for b in m.get("content", []) if b.get("type") == "text"))


def _check_request(payload: dict, context: list[str], meta: dict, index_of: dict[str, int],
                   recs: list[LedgerRecord], payloads: dict[int, Any]) -> list[str]:
    """Cada mensaje del blob corresponde a entradas de `context` y no sobra ni falta ninguna.

    `meta.segments[k]` es el índice del mensaje que cubre `context[k]` (-1 = toda la petición, solo para
    `context_reset`). Si el blob de una entrada del contexto falta, esa entrada no se puede contrastar y se
    omite (el blob ya quedó como `missing`).
    """
    problems: list[str] = []
    messages = payload.get("messages") or []
    segments = meta.get("segments")
    if segments is None:
        return ["falta meta.segments"]
    if len(segments) != len(context):
        return [f"segments ({len(segments)}) y context ({len(context)}) no tienen el mismo largo"]
    by_msg: dict[int, list[tuple[str, Any]]] = {}
    for h, seg in zip(context, segments):
        j = index_of.get(h)
        if j is None:
            continue                      # ya reportado por la comprobación 4
        kind = recs[j].entry.get("kind")
        if seg == -1:
            if kind != "context_reset":
                problems.append(f"entrada {j} ({kind}) con segmento -1")
            continue
        if not isinstance(seg, int) or not 0 <= seg < len(messages):
            problems.append(f"entrada {j} apunta a un mensaje inexistente ({seg})")
            continue
        role = messages[seg].get("role")
        if kind not in ROLE_KINDS.get(role, set()):
            problems.append(f"entrada {j} ({kind}) no corresponde a un mensaje de rol {role}")
            continue
        by_msg.setdefault(seg, []).append((kind, payloads.get(j)))
    for k, m in enumerate(messages):
        entries = by_msg.get(k)
        if not entries:
            problems.append(f"mensaje {k} ({m.get('role')}) sin entrada en el contexto")
            continue
        problems += [f"mensaje {k}: {p}" for p in _check_message(m, entries)]
    return problems


def _check_message(m: dict, entries: list[tuple[str, Any]]) -> list[str]:
    out: list[str] = []
    blocks = m.get("content", [])
    if m.get("role") == "assistant":
        resp = [p for kind, p in entries if kind == "llm_response" and p is not None]
        reset = [p for kind, p in entries if kind == "context_reset" and p is not None]
        calls = [p for kind, p in entries if kind == "tool_call" and p is not None]
        if resp and not reset and all(p.get("content") != blocks for p in resp):
            # ítem por ítem (Responses, §17.4): cada bloque tiene que estar en alguna llm_response del contexto
            known = [b for p in resp for b in p.get("content") or []]
            if not blocks or any(b not in known for b in blocks):
                out.append("no coincide con la llm_response registrada")
        if reset and all(p.get("message") != m for p in reset):
            out.append("no coincide con el context_reset registrado")
        ids = {b.get("id") for b in blocks if b.get("type") == "tool_use"}
        for p in calls:
            if p.get("id") not in ids:
                out.append(f"tool_call {p.get('id')} no aparece en el mensaje")
        return out
    # user / system: cada tool_result tiene su entrada, y el texto queda cubierto por las entradas de texto
    results = [p for kind, p in entries if kind == "tool_result" and p is not None]
    missing_payloads = any(p is None for _, p in entries)
    for b in blocks:
        if b.get("type") == "tool_result" and not missing_payloads and b not in results:
            out.append(f"tool_result {b.get('tool_use_id')} sin entrada")
    for p in results:
        if p not in blocks:
            out.append("una entrada tool_result no aparece en el mensaje")
    text = _text_blocks(m)
    for kind, p in entries:
        if p is None or kind == "tool_result":
            continue
        frag = normalize_text(p["body"]["content"]) if kind == "user_prompt" else (
            normalize_text(p.get("text", "")) if "text" in p else normalize_text(block_text(p.get("block", {}))))
        if "block" in p and p["block"] not in blocks:
            out.append("una entrada de bloque no aparece en el mensaje")
            continue
        if frag and frag not in text:
            out.append(f"texto de {kind} no aparece en el mensaje")
            continue
        text = text.replace(frag, "", 1) if frag else text
    if not missing_payloads and normalize_text(text):
        out.append("texto del mensaje sin entrada que lo cubra")
    return out
