"""Proxy de punta a punta con upstream fake (SPEC §6.5, §7.5)."""
from openwitness.ledger import load_records, verify_ledger

from bench.synthetic import make_sessions, run_conversation, scripted_env


async def test_conversation_verifies_clean(tmp_path):
    env = scripted_env(tmp_path)
    s = await run_conversation(env)
    rep = verify_ledger(s["ledger"], env.gateway.blobs, env.gateway_pubkey, env.registry, env.anchor,
                        s["receipts"])
    assert rep.ok, [e.to_dict() for e in rep.errors]
    assert s["stats"]["history_mismatch"] == 0
    assert s["stats"]["unmatched_user_text"] == 0
    kinds = [r.kind for r in load_records(s["ledger"])]
    assert kinds[0] == "session_start" and "user_prompt" in kinds and "tool_call" in kinds
    assert "anchor" in kinds and kinds[-1] == "anchor"
    assert rep.unanchored_tail == 0


async def test_all_variants_verify(tmp_path):
    env, sessions = await make_sessions(tmp_path, n=5)
    for s in sessions:
        rep = verify_ledger(s["ledger"], env.gateway.blobs, env.gateway_pubkey, env.registry, env.anchor,
                            s["receipts"])
        assert rep.ok, (s["sid"], [e.to_dict() for e in rep.errors])
        assert s["stats"]["history_mismatch"] == 0


async def test_unsigned_text_is_no_hook(tmp_path):
    env = scripted_env(tmp_path)
    s = await run_conversation(env, signed=False)
    reasons = [r.meta.get("reason") for r in load_records(s["ledger"]) if r.kind == "user_text_unsigned"]
    assert "no_hook" in reasons


async def test_require_signed_prompts_rejects(tmp_path):
    env = scripted_env(tmp_path)
    c = env.client
    j = (await c.post("/sessions", json={"user_id": "alice", "policy": {"require_signed_prompts": True}})).json()
    r = await c.post(f"/s/{j['sid']}/v1/messages", headers={"x-api-key": j["token"]},
                     json={"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hola"}]})
    assert r.status_code == 403


async def test_bad_token(tmp_path):
    env = scripted_env(tmp_path)
    j = (await env.client.post("/sessions", json={"user_id": "alice"})).json()
    r = await env.client.post(f"/s/{j['sid']}/v1/messages", headers={"x-api-key": "nope"}, json={})
    assert r.status_code == 401
