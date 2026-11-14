"""Evidencia de una acción: contexto exacto que vio el modelo, con la procedencia de cada fragmento (SPEC §7.9).

`tool_call.context[0]` es el `llm_request` cuya respuesta lo produjo; el `context` de ese `llm_request` son las
entradas que componen el contexto (y `meta.segments[k]` dice a qué mensaje corresponde cada una, §6.4).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ledger import BlobStore, load_records
from ..types import EvidenceItem, IntegrityReport, LedgerRecord

UNSIGNED_REASON = {"no_hook": "user_unsigned", "no_match": "user_unsigned", "agent_injected": "agent_injected",
                   "non_text_block": "agent_injected", "empty": "agent_injected", "system_role": "system",
                   "compaction": "compaction"}


def is_tool_provenance(p: str) -> bool:
    return p.startswith(("mcp:", "builtin:", "hosted:"))


def _content_text(c: Any) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    parts.append(json.dumps(b.get("input") or {}, ensure_ascii=False))
                elif "content" in b:
                    parts.append(_content_text(b.get("content")))
            else:
                parts.append(str(b))
        return "\n".join(p for p in parts if p)
    return json.dumps(c, ensure_ascii=False)


def entry_text(rec: LedgerRecord, payload: Any) -> str:
    """Texto buscable del payload de una entrada (para el taint)."""
    if payload is None:
        return ""
    k = rec.kind
    if k == "user_prompt":
        return (payload.get("body") or {}).get("content", "")
    if k == "user_text_unsigned":
        return payload.get("text") if "text" in payload else json.dumps(payload.get("block"), ensure_ascii=False)
    if k == "tool_result":
        return _content_text(payload.get("content"))
    if k == "llm_response":
        return _content_text(payload.get("content"))
    if k == "tool_call":
        return json.dumps(payload.get("args") or {}, ensure_ascii=False)
    if k == "context_reset":
        if "message" in payload:
            return _content_text((payload.get("message") or {}).get("content"))
        return "\n".join(_content_text(m.get("content")) for m in payload.get("messages") or [])
    return _content_text(payload)


def provenance(rec: LedgerRecord) -> tuple[str, str]:
    """(procedencia, motivo) de una entrada del contexto, según la tabla de §7.9."""
    k, m = rec.kind, rec.meta
    if k == "user_prompt":
        return "user_signed", f"SignedPrompt de {m.get('user_id')}"
    if k == "user_text_unsigned":
        r = m.get("reason", "")
        return UNSIGNED_REASON.get(r, "user_unsigned"), f"user_text_unsigned/{r}"
    if k == "tool_result":
        return m.get("origin") or "unknown", f"tool_result de {m.get('tool')}"
    if k in ("llm_response", "tool_call"):
        return "model", k
    if k == "context_reset":
        if m.get("reason") == "history_mismatch":
            return "model", "context_reset/history_mismatch"
        return "compaction", "context_reset/compaction"
    return "unknown", k


@dataclass
class EvidenceBundle:
    action: LedgerRecord
    action_payload: dict | None
    request: LedgerRecord | None
    items: list[EvidenceItem] = field(default_factory=list)      # fragmento = texto completo
    resources: dict[int, str] = field(default_factory=dict)      # seq de tool_result → recurso (URL, ruta)
    user_id: str | None = None
    integrity_ok: bool = True
    integrity_errors: list[dict] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)             # seq cuyo blob falta (evidencia reducida)

    @property
    def tool(self) -> str:
        return (self.action_payload or {}).get("name") or self.action.meta.get("tool", "")

    @property
    def args(self) -> dict:
        return (self.action_payload or {}).get("args") or {}

    def texts(self, prov: str) -> list[str]:
        return [i.fragment for i in self.items if i.provenance == prov]

    def seqs(self) -> set[int]:
        return {i.seq for i in self.items} | {self.action.seq} | ({self.request.seq} if self.request else set())


RESOURCE_KEYS = ("url", "path", "file", "uri", "issue_id", "project")


def build_evidence(ledger_path: Path | str, blobs: BlobStore, entry_seq: int,
                   integrity: IntegrityReport | None = None, records: list[LedgerRecord] | None = None
                   ) -> EvidenceBundle:
    recs = records if records is not None else load_records(ledger_path)
    by_seq = {r.seq: r for r in recs}
    by_hash = {r.hash: r for r in recs}
    action = by_seq.get(entry_seq)
    if action is None:
        raise ValueError(f"no existe la entrada seq={entry_seq}")
    if action.kind != "tool_call":
        raise ValueError(f"seq={entry_seq} es {action.kind}; la atribución es sobre un tool_call")
    start = by_seq.get(0)
    ev = EvidenceBundle(action, blobs.get_obj(action.payload_hash) if action.payload_hash else None, None,
                        user_id=(start.meta.get("user_id") if start else None))
    if integrity is not None and not integrity.ok_up_to(entry_seq):
        ev.integrity_ok = False
        ev.integrity_errors = [e.to_dict() for e in integrity.errors
                               if e.seq is None or e.seq <= entry_seq][:10]
        return ev
    req = by_hash.get(action.context[0]) if action.context else None
    ev.request = req
    if req is None:
        return ev
    req_payload = blobs.get_obj(req.payload_hash) if req.payload_hash else None
    if req_payload is None:
        ev.missing.append(req.seq)
    elif req_payload.get("system"):
        ev.items.append(EvidenceItem(req.seq, "system_prompt", req.hash, str(req_payload["system"]), "system",
                                     "system prompt del llm_request"))
    seen: set[str] = set()
    for h in req.context:
        if h in seen:
            continue
        seen.add(h)
        r = by_hash.get(h)
        if r is None:
            continue
        payload = blobs.get_obj(r.payload_hash) if r.payload_hash else None
        if payload is None and r.payload_hash:
            ev.missing.append(r.seq)
        prov, why = provenance(r)
        ev.items.append(EvidenceItem(r.seq, r.kind, r.hash, entry_text(r, payload), prov, why))
        if r.kind == "tool_result" and r.meta.get("call_entry") in by_hash:
            call = by_hash[r.meta["call_entry"]]
            cp = blobs.get_obj(call.payload_hash) or {}
            args = cp.get("args") or {}
            res = next((str(args[k]) for k in RESOURCE_KEYS if args.get(k)), None)
            if res:
                ev.resources[r.seq] = res
    return ev
