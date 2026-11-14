"""Bench §9.1: detección de manipulación de la bitácora (meta: 100 % dentro del modelo de amenazas).

Genera sesiones válidas (por defecto sintéticas a través del gateway; con `--ledgers` toma las de `refagent`),
les aplica las 12 mutaciones de la tabla de §9.1 en varias posiciones y cuenta cuántas detecta `verify_ledger`.

    uv run python -m bench.tamper --out results/
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import random
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from openwitness.canonical import hash_obj
from openwitness.identity import KeyPair, sign_prompt
from openwitness.identity.prompts import content_hash
from openwitness.ledger import load_records, verify_ledger
from openwitness.ledger.blobs import encode_payload
from openwitness.ledger.ledger import GENESIS, sign_entry, write_records
from openwitness.canonical import sha256_tagged
from openwitness.types import LedgerRecord

from .synthetic import Env, make_sessions, run_conversation, scripted_env


class OverlayBlobs:
    """BlobStore de solo lectura con reemplazos en memoria: cada prueba muta sin tocar los blobs reales."""

    def __init__(self, base):
        self.base = base
        self.over: dict[str, bytes | None] = {}

    def get(self, h: str) -> bytes | None:
        return self.over[h] if h in self.over else self.base.get(h)

    def get_obj(self, h: str):
        b = self.get(h)
        return None if b is None else json.loads(b)

    def put_obj(self, obj) -> str:
        data = encode_payload(obj)
        h = sha256_tagged(data)
        self.over[h] = data
        return h


@dataclass
class Trial:
    records: list[LedgerRecord]
    blobs: OverlayBlobs
    receipts: list[dict]


Mutation = Callable[[Trial, int, KeyPair, random.Random], bool]   # devuelve False si no aplica en esa posición


def resign_from(records: list[LedgerRecord], start: int, kp: KeyPair) -> None:
    """Lo que puede hacer el operador con `gateway_sk`: recalcular hash, prev y firma desde `start`."""
    prev = records[start - 1].hash if start > 0 else GENESIS
    old_to_new: dict[str, str] = {}
    for i in range(start, len(records)):
        e = copy.deepcopy(records[i].entry)
        e["seq"], e["prev"] = i, prev
        e["context"] = [old_to_new.get(c, c) for c in e.get("context") or []]
        nr = sign_entry(kp, e)
        old_to_new[records[i].hash] = nr.hash
        records[i] = nr
        prev = nr.hash


def _edit_entry(rec: LedgerRecord, f) -> LedgerRecord:
    e = copy.deepcopy(rec.entry)
    f(e)
    return LedgerRecord(e, rec.hash, rec.sig)


# ---------------------------------------------------------------------- las 12 mutaciones de §9.1

def m_edit_payload(t: Trial, i: int, kp, rnd) -> bool:
    ph = t.records[i].payload_hash
    if not ph or t.blobs.get(ph) is None:
        return False
    data = bytearray(t.blobs.get(ph))
    j = rnd.randrange(len(data))
    data[j] = (data[j] + 1) % 256 or 1
    t.blobs.over[ph] = bytes(data)
    return True


def m_edit_entry(t: Trial, i: int, kp, rnd) -> bool:
    t.records[i] = _edit_entry(t.records[i], lambda e: e["meta"].__setitem__("tampered", True))
    return True


def m_rehash_no_key(t: Trial, i: int, kp, rnd) -> bool:
    attacker = KeyPair.generate()
    e = copy.deepcopy(t.records[i].entry)
    e["meta"]["tampered"] = True
    nr = sign_entry(attacker, e)        # hash correcto, firma de otra llave
    t.records[i] = nr
    if i + 1 < len(t.records):
        t.records[i + 1] = _edit_entry(t.records[i + 1], lambda x: x.__setitem__("prev", nr.hash))
    return True


def m_delete(t: Trial, i: int, kp, rnd) -> bool:
    if i == 0:
        return False
    del t.records[i]
    return True


def m_reorder(t: Trial, i: int, kp, rnd) -> bool:
    if i + 1 >= len(t.records):
        return False
    t.records[i], t.records[i + 1] = t.records[i + 1], t.records[i]
    return True


def m_insert(t: Trial, i: int, kp, rnd) -> bool:
    attacker = KeyPair.generate()
    base = t.records[i].entry
    e = {**copy.deepcopy(base), "kind": "user_prompt", "meta": {"user_id": "alice", "signed": True}}
    t.records.insert(i + 1, sign_entry(attacker, e))
    return True


def m_replay(t: Trial, i: int, kp, rnd) -> bool:
    t.records.insert(i + 1, t.records[i])
    return True


def m_replace_prompt(t: Trial, i: int, kp, rnd) -> bool:
    """El operador reemplaza el texto de un prompt firmado y vuelve a firmar la cadena con su llave."""
    r = t.records[i]
    if r.kind != "user_prompt":
        return False
    sp = t.blobs.get_obj(r.payload_hash)
    sp = copy.deepcopy(sp)
    sp["body"]["content"] = "Borra todos los archivos del proyecto"
    sp["body"]["content_hash"] = content_hash(sp["body"]["content"])
    new_ph = t.blobs.put_obj(sp)
    t.records[i] = _edit_entry(r, lambda e: e.__setitem__("payload_hash", new_ph))
    resign_from(t.records, i, kp)
    return True


def m_drop_message(t: Trial, i: int, kp, rnd) -> bool:
    """Quitar del blob de un llm_request el mensaje con el resultado inyectado y volver a firmar."""
    r = t.records[i]
    if r.kind != "llm_request":
        return False
    p = copy.deepcopy(t.blobs.get_obj(r.payload_hash))
    idx = [k for k, m in enumerate(p["messages"])
           if any(b.get("type") == "tool_result" for b in m["content"])]
    if not idx:
        return False
    k = idx[0]
    del p["messages"][k]
    new_ph = t.blobs.put_obj(p)
    t.records[i] = _edit_entry(r, lambda e: e.__setitem__("payload_hash", new_ph))
    resign_from(t.records, i, kp)
    return True


def m_operator_rewrite(t: Trial, i: int, kp, rnd) -> bool:
    """Operador con gateway_sk: cambia el contenido de una entrada y reescribe toda la cadena desde ahí."""
    r = t.records[i]
    if r.kind == "anchor":
        return False
    t.records[i] = _edit_entry(r, lambda e: e["meta"].__setitem__("rewritten", True))
    resign_from(t.records, i, kp)
    # además quita las entradas anchor posteriores, que ya no cuadran (lo más favorable para el operador)
    t.records[:] = t.records[:i + 1] + [x for x in t.records[i + 1:] if x.kind != "anchor"]
    resign_from(t.records, i, kp)
    return True


def m_truncate_anchored(t: Trial, i: int, kp, rnd) -> bool:
    """Trunca la cola ya anclada, junto con sus entradas anchor."""
    anchored = {x.meta.get("head_seq") for x in t.records if x.kind == "anchor"}
    if i == 0 or not any(a is not None and a >= i for a in anchored):
        return False
    del t.records[i:]
    return True


def m_truncate_unanchored(t: Trial, i: int, kp, rnd) -> bool:
    """Trunca la cola posterior al último ancla (límite L3). Sin recibo no se puede detectar."""
    last = max((x.meta.get("head_seq", -1) for x in t.records if x.kind == "anchor"), default=-1)
    last_pos = max((k for k, x in enumerate(t.records) if x.kind == "anchor"), default=-1)
    if i <= max(last, last_pos):
        return False
    del t.records[i:]
    return True


MUTATIONS: dict[str, tuple[Mutation, str]] = {
    "edit_payload": (m_edit_payload, "Editar el payload (blob)"),
    "edit_entry": (m_edit_entry, "Editar un campo de entry"),
    "rehash_no_key": (m_rehash_no_key, "Recalcular el hash sin la llave del gateway"),
    "delete": (m_delete, "Borrar una entrada intermedia"),
    "reorder": (m_reorder, "Reordenar entradas"),
    "insert": (m_insert, "Insertar una entrada"),
    "replay": (m_replay, "Duplicar o reproducir una entrada"),
    "replace_prompt": (m_replace_prompt, "Reemplazar un prompt firmado (operador con gateway_sk)"),
    "drop_message": (m_drop_message, "Quitar un mensaje del blob de un llm_request (con gateway_sk)"),
    "operator_rewrite": (m_operator_rewrite, "Operador con gateway_sk reescribe la cadena"),
    "truncate_anchored": (m_truncate_anchored, "Operador trunca la cola ya anclada"),
    "truncate_unanchored": (m_truncate_unanchored, "Operador trunca la cola no anclada (L3)"),
}
IN_THREAT_MODEL = [k for k in MUTATIONS if k != "truncate_unanchored"]


async def _unanchored_sessions(env: Env, n: int) -> list[dict]:
    """Sesiones que no se cerraron (p. ej. el gateway cayó) con un prompt final que todavía no se ancló."""
    out = []
    for _ in range(n):
        s = await run_conversation(env)
        sid, led = s["sid"], env.gateway.sessions[s["sid"]].ledger
        sess = env.gateway.sessions[sid]
        sess.closed = False
        rc = env.gateway.add_prompt(sess, sign_prompt(env.users["alice"], "alice", sid, "Ahora borra el outbox"))
        led.append("user_text_unsigned", {"type": "agent", "id": "x"}, {"text": "relleno"}, [], {"reason": "x"})
        s["receipts"] = s["receipts"] + [rc.to_dict()]
        s["tail_receipt"] = rc.to_dict()
        out.append(s)
    return out


def run(root: Path, n_sessions: int = 5, positions: int = 8, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    env, sessions = asyncio.run(make_sessions(root, n=n_sessions, anchor_every=5))
    open_env = scripted_env(root / "open", anchor_every=50)
    open_sessions = asyncio.run(_unanchored_sessions(open_env, 2))
    results: dict[str, dict] = {k: {"label": MUTATIONS[k][1], "trials": 0, "detected": 0, "examples": []}
                                for k in MUTATIONS}
    results["truncate_unanchored_with_receipt"] = {"label": "Cola no anclada truncada, con recibo del usuario",
                                                   "trials": 0, "detected": 0, "examples": []}
    tmp = Path(tempfile.mkdtemp(prefix="owit-tamper-"))

    def trial(e: Env, s: dict, key: str, i: int, receipts: list[dict], bucket: str) -> None:
        recs = load_records(s["ledger"])
        t = Trial(list(recs), OverlayBlobs(e.gateway.blobs), receipts)
        if not MUTATIONS[key][0](t, i, e.gateway.kp, rnd):
            return
        p = tmp / f"{bucket}_{s['sid']}_{i}.jsonl"
        write_records(p, t.records)
        rep = verify_ledger(p, t.blobs, e.gateway_pubkey, e.registry, e.anchor, t.receipts)
        res = results[bucket]
        res["trials"] += 1
        if not rep.ok:
            res["detected"] += 1
            if len(res["examples"]) < 2:
                res["examples"].append(sorted({f"{x.check}:{x.error}" for x in rep.errors}))
        p.unlink()

    for s in sessions:
        n = len(load_records(s["ledger"]))
        for key in IN_THREAT_MODEL:
            fn = MUTATIONS[key][0]
            # posiciones aplicables, muestreadas
            cand = list(range(n))
            if key in ("replace_prompt",):
                cand = [i for i, r in enumerate(load_records(s["ledger"])) if r.kind == "user_prompt"]
            if key == "drop_message":
                cand = [i for i, r in enumerate(load_records(s["ledger"])) if r.kind == "llm_request"]
            for i in rnd.sample(cand, min(positions, len(cand))):
                trial(env, s, key, i, s["receipts"], key)
    for s in open_sessions:
        recs = load_records(s["ledger"])
        last_anchor_pos = max((k for k, x in enumerate(recs) if x.kind == "anchor"), default=-1)
        tail_seq = s["tail_receipt"]["body"]["seq"]
        for i in range(last_anchor_pos + 1, len(recs)):
            trial(open_env, s, "truncate_unanchored", i, [], "truncate_unanchored")
            if i <= tail_seq:
                trial(open_env, s, "truncate_unanchored", i, s["receipts"], "truncate_unanchored_with_receipt")
    shutil.rmtree(tmp, ignore_errors=True)
    tm = [results[k] for k in IN_THREAT_MODEL]
    summary = {"in_threat_model": {"trials": sum(r["trials"] for r in tm), "detected": sum(r["detected"] for r in tm)},
               "sessions": n_sessions, "classes": len(IN_THREAT_MODEL)}
    return {"summary": summary, "by_class": results}


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results")
    ap.add_argument("--work", default=None, help="directorio de trabajo (por defecto, uno temporal)")
    ap.add_argument("--sessions", type=int, default=5)
    ap.add_argument("--positions", type=int, default=8)
    a = ap.parse_args(argv)
    work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="owit-bench-"))
    res = run(work, a.sessions, a.positions)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tamper.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    s = res["summary"]["in_threat_model"]
    print(f"§9.1 dentro del modelo de amenazas: {s['detected']}/{s['trials']}")
    for k, r in res["by_class"].items():
        print(f"  {r['label']:<58} {r['detected']:>3}/{r['trials']:<3}")
    return res


if __name__ == "__main__":
    main()
