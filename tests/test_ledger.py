"""Bitácora, recibos, MockAnchor y verify_ledger (SPEC §6.4, §6.6, §6.7, §7.4)."""
import pytest

from openwitness.canonical import CanonicalError
from openwitness.identity import KeyPair, KeyRegistry, sign_prompt
from openwitness.ledger import BlobStore, Ledger, MockAnchor, load_records, verify_ledger
from openwitness.ledger.ledger import write_records
from openwitness.types import Actor

G = Actor("gateway", "gateway")


def _ledger(tmp_path, anchor_every=3):
    kp = KeyPair.generate()
    blobs = BlobStore(tmp_path / "blobs")
    anchor = MockAnchor(tmp_path / "anchor.jsonl")
    led = Ledger(tmp_path / "ledger" / "s_test.jsonl", kp, blobs, anchor, anchor_every=anchor_every)
    led.append("session_start", G, {"cfg": 1}, [], {"gateway_pubkey": kp.public_str(), "user_id": "alice"})
    return kp, blobs, anchor, led


def test_genesis_and_chain(tmp_path):
    kp, blobs, anchor, led = _ledger(tmp_path)
    r1 = led.append("user_text_unsigned", G, {"text": "hola"}, [led.head().hash], {"reason": "no_hook"})
    recs = load_records(led.path)
    assert recs[0].entry["prev"] == "sha256:" + "0" * 64 and recs[0].seq == 0
    assert r1.entry["prev"] == recs[0].hash
    rep = verify_ledger(led.path, blobs, kp.public_str(), KeyRegistry(), anchor)
    assert rep.ok, rep.errors


def test_floats_allowed_in_payload_not_in_meta(tmp_path):
    kp, blobs, anchor, led = _ledger(tmp_path)
    led.append("llm_request", G, {"temperature": 0.2, "messages": []}, [], {"segments": []})
    with pytest.raises(CanonicalError):
        led.append("llm_response", G, {}, [], {"score": 0.5})


def test_periodic_anchor_and_tail(tmp_path):
    kp, blobs, anchor, led = _ledger(tmp_path, anchor_every=3)
    for i in range(7):
        led.append("user_text_unsigned", G, {"text": str(i)}, [], {"reason": "no_hook"})
    kinds = [r.kind for r in load_records(led.path)]
    assert kinds.count("anchor") == 2
    rep = verify_ledger(led.path, blobs, kp.public_str(), KeyRegistry(), anchor)
    assert rep.ok and rep.unanchored_tail == 2
    led.force_anchor()
    rep = verify_ledger(led.path, blobs, kp.public_str(), KeyRegistry(), anchor)
    assert rep.ok and rep.unanchored_tail == 0
    assert len(anchor.heads_for("s_test", kp.public_str())) == 3


def test_missing_blob_is_not_tampering(tmp_path):
    kp, blobs, anchor, led = _ledger(tmp_path)
    r = led.append("tool_result", G, {"type": "tool_result", "content": []}, [], {})
    blobs.delete(r.payload_hash)
    rep = verify_ledger(led.path, blobs, kp.public_str(), KeyRegistry(), anchor)
    assert rep.ok and rep.blob_status[r.seq] == "missing"


def test_signed_prompt_and_receipt(tmp_path):
    kp, blobs, anchor, led = _ledger(tmp_path, anchor_every=100)
    alice = KeyPair.generate()
    reg = KeyRegistry()
    reg.register("alice", alice.public_str())
    sp = sign_prompt(alice, "alice", "s_test", "Resume los issues")
    r = led.append("user_prompt", Actor("user", "alice"), sp, [], {"user_id": "alice", "signed": True})
    rc = led.receipt(r)
    assert verify_ledger(led.path, blobs, kp.public_str(), reg, anchor, [rc]).ok
    # truncar la entrada del prompt (cola no anclada): solo el recibo lo delata (L3)
    write_records(led.path, load_records(led.path)[:-1])
    assert verify_ledger(led.path, blobs, kp.public_str(), reg, anchor).ok
    rep = verify_ledger(led.path, blobs, kp.public_str(), reg, anchor, [rc])
    assert not rep.ok and rep.errors[0].error == "receipt_entry_missing"


def test_prompt_from_unregistered_key_fails(tmp_path):
    kp, blobs, anchor, led = _ledger(tmp_path)
    mallory = KeyPair.generate()
    sp = sign_prompt(mallory, "alice", "s_test", "x")
    led.append("user_prompt", Actor("user", "alice"), sp, [], {"user_id": "alice", "signed": True})
    reg = KeyRegistry()
    rep = verify_ledger(led.path, blobs, kp.public_str(), reg, anchor)
    assert any(e.error == "prompt_unknown_key" for e in rep.errors)


def test_anchor_rejects_bad_submitter_sig(tmp_path):
    from openwitness.ledger.anchor import AnchorRejected
    anchor = MockAnchor(tmp_path / "a.jsonl")
    with pytest.raises(AnchorRejected):
        anchor.submit("sha256:" + "1" * 64, "AAAA", KeyPair.generate().public_str(), "s")


def test_tamper_bench_small(tmp_path):
    from bench.tamper import run
    res = run(tmp_path, n_sessions=2, positions=3)
    s = res["summary"]["in_threat_model"]
    assert s["trials"] > 0 and s["detected"] == s["trials"]
    assert res["by_class"]["truncate_unanchored_with_receipt"]["detected"] == \
        res["by_class"]["truncate_unanchored_with_receipt"]["trials"]
