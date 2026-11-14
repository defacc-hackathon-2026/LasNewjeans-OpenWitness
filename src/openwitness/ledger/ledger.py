"""Bitácora append-only, encadenada por hashes y firmada por el gateway (SPEC §6.4, §6.6, §6.7, §7.4).

Archivo: `data/ledger/<session_id>.jsonl`, una línea `{entry, hash, sig}` por entrada.
- `hash = hash_obj(entry)` (JSON canónico, sin floats).
- `sig  = gateway_sk.sign("openwitness/ledger/v1", entry)`, que firma `sha256(canonical(entry))`, es decir, el hash.

Un único escritor por sesión: `append` toma un lock. El gateway, además, serializa las peticiones de una misma
sesión con un `asyncio.Lock` (subagentes en paralelo).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..canonical import ZERO_HASH, hash_obj
from ..identity import DOMAIN_ANCHOR, DOMAIN_LEDGER, DOMAIN_RECEIPT, KeyPair
from ..timeutil import now_rfc3339
from ..types import Actor, LedgerRecord, Receipt
from .anchor import Anchor, submission_body
from .blobs import BlobStore

GENESIS = ZERO_HASH
GATEWAY_ACTOR = Actor("gateway", "gateway")


def load_records(path: Path | str) -> list[LedgerRecord]:
    p = Path(path)
    if not p.exists():
        return []
    return [LedgerRecord.from_dict(json.loads(x)) for x in p.read_text().splitlines() if x.strip()]


def write_records(path: Path | str, records: list[LedgerRecord]) -> None:
    """Reescribe un archivo completo. Solo lo usan las herramientas de manipulación (§9.1) y los tests."""
    Path(path).write_text("".join(json.dumps(r.to_dict(), ensure_ascii=False) + "\n" for r in records))


def sign_entry(signer: KeyPair, entry: dict) -> LedgerRecord:
    return LedgerRecord(entry, hash_obj(entry), signer.sign(DOMAIN_LEDGER, entry))


class Ledger:
    def __init__(self, path: Path | str, signer: KeyPair, blobs: BlobStore, anchor: Anchor | None = None, *,
                 session_id: str | None = None, anchor_every: int = 20, anchor_interval_s: float = 30.0,
                 auto_anchor: bool = True):
        """
        - `anchor_every` / `anchor_interval_s`: política de anclaje de §6.7 (N entradas o T segundos).
        - `auto_anchor=True` ancla dentro de `append` cuando toca (tests, bench, refagent sin servidor).
          El gateway usa `auto_anchor=False` y llama a `anchor_if_due()` fuera del camino crítico.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or self.path.stem
        self.signer = signer
        self.blobs = blobs
        self.anchor = anchor
        self.anchor_every = anchor_every
        self.anchor_interval_s = anchor_interval_s
        self.auto_anchor = auto_anchor
        self.records: list[LedgerRecord] = load_records(self.path)
        self._lock = threading.RLock()
        self._since_anchor = 0
        self._last_anchor_t = time.monotonic()
        self._last_anchored_hash: str | None = None
        self.append_times_ms: list[float] = []     # para §9.4

    # ------------------------------------------------------------------ escritura
    def append(self, kind: str, actor: Actor | dict, payload: Any = None, context: list[str] | None = None,
               meta: dict | None = None) -> LedgerRecord:
        t0 = time.perf_counter()
        with self._lock:
            rec = self._append_locked(kind, actor, payload, context, meta)
            if kind != "anchor":
                self._since_anchor += 1
            if self.auto_anchor and self._due():
                self._anchor_locked()
        self.append_times_ms.append((time.perf_counter() - t0) * 1000)
        return rec

    def _append_locked(self, kind: str, actor: Actor | dict, payload: Any, context: list[str] | None,
                       meta: dict | None) -> LedgerRecord:
        ph = self.blobs.put_obj(payload) if payload is not None else None
        prev = self.records[-1].hash if self.records else GENESIS
        entry = {"v": 1, "session_id": self.session_id, "seq": len(self.records), "prev": prev, "ts": now_rfc3339(),
                 "kind": kind, "actor": actor.to_dict() if isinstance(actor, Actor) else dict(actor),
                 "payload_hash": ph, "context": list(context or []), "meta": dict(meta or {})}
        rec = sign_entry(self.signer, entry)
        with self.path.open("a") as f:
            f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
        self.records.append(rec)
        return rec

    # ------------------------------------------------------------------ anclaje (§6.7)
    def _due(self) -> bool:
        if self.anchor is None or not self.records or self._since_anchor == 0:
            return False
        return (self._since_anchor >= self.anchor_every
                or time.monotonic() - self._last_anchor_t >= self.anchor_interval_s)

    def anchor_if_due(self) -> LedgerRecord | None:
        with self._lock:
            return self._anchor_locked() if self._due() else None

    def force_anchor(self) -> LedgerRecord | None:
        with self._lock:
            if self.anchor is None or not self.records:
                return None
            return self._anchor_locked()

    def _anchor_locked(self) -> LedgerRecord | None:
        assert self.anchor is not None
        head = self.records[-1]
        if head.kind == "anchor" or head.hash == self._last_anchored_hash:
            return None
        sig = self.signer.sign(DOMAIN_ANCHOR, submission_body(head.hash, self.session_id))
        receipt = self.anchor.submit(head.hash, sig, self.signer.public_str(), self.session_id)
        self._since_anchor = 0
        self._last_anchor_t = time.monotonic()
        self._last_anchored_hash = head.hash
        return self._append_locked("anchor", GATEWAY_ACTOR, receipt.to_dict(), [],
                                   {"head_hash": head.hash, "head_seq": head.seq, "backend": self.anchor.name})

    # ------------------------------------------------------------------ lectura
    def head(self) -> LedgerRecord:
        return self.records[-1]

    def by_hash(self, h: str) -> LedgerRecord | None:
        for r in reversed(self.records):
            if r.hash == h:
                return r
        return None

    # ------------------------------------------------------------------ recibos (§6.6)
    def receipt(self, record: LedgerRecord) -> Receipt:
        body = {"session_id": self.session_id, "seq": record.seq, "entry_hash": record.hash,
                "prompt_hash": record.payload_hash}
        return Receipt(body, self.signer.sign(DOMAIN_RECEIPT, body), self.signer.public_str())
