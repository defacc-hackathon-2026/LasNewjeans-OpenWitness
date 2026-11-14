"""Descomposición sobre peticiones reales de Claude Code (fixtures de tests/fixtures/claude_code/)."""
import json
from pathlib import Path

import httpx
import pytest

from openwitness.gateway.protocols.anthropic_messages import SSEReassembler
from openwitness.gateway.server import Gateway
from openwitness.identity import KeyPair, KeyRegistry, sign_prompt
from openwitness.ledger import MockAnchor, load_records, verify_ledger
from openwitness.llm.base import LLMProfile
from openwitness.llm.fake import FakeUpstream

FIX = Path(__file__).parent / "fixtures" / "claude_code"
CASES = sorted(p for p in FIX.glob("*/*") if p.is_dir())
PROMPT = "Lee el archivo NOTAS.md y resúmelo en una frase."


def _load(case: Path):
    reqs = [json.loads(p.read_text()) for p in sorted(case.glob("*_request.json"))]
    resps = []
    for p in sorted(case.glob("*_response.sse")):
        sse = SSEReassembler()
        sse.feed(p.read_bytes())
        resps.append([sse.blocks[i] for i in sorted(sse.blocks)])
    return reqs, resps


async def _replay(tmp_path, case: Path, signed: bool):
    reqs, resps = _load(case)
    it = iter(resps)
    fake = FakeUpstream(LLMProfile("fake", "fake", model="fake-model"), responder=lambda body: next(it))
    alice = KeyPair.generate()
    reg = KeyRegistry()
    reg.register("alice", alice.public_str())
    anchor = MockAnchor(tmp_path / "anchor.jsonl")
    gw = Gateway(tmp_path, KeyPair.generate(), reg, upstreams={"fake": fake}, default_upstream="fake",
                 anchor=anchor)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app()), base_url="http://gw")
    j = (await c.post("/sessions", json={"user_id": "alice", "agent": {"name": "claude-code"}})).json()
    h = {"authorization": f"Bearer {j['token']}", "anthropic-version": "2023-06-01"}
    receipts = []
    if signed:
        receipts.append((await c.post(f"/s/{j['sid']}/prompts", json=sign_prompt(alice, "alice", j["sid"], PROMPT),
                                      headers=h)).json())
    for body in reqs:
        r = await c.post(f"/s/{j['sid']}/v1/messages?beta=true", json=body, headers=h)
        assert r.status_code == 200
    await c.post(f"/s/{j['sid']}/end", headers=h, json={})
    s = gw.sessions[j["sid"]]
    rep = verify_ledger(gw.ledger_path(s.sid), gw.blobs, gw.kp.public_str(), reg, anchor, receipts)
    return s, rep, load_records(gw.ledger_path(s.sid))


@pytest.mark.parametrize("case", CASES, ids=[f"{c.parent.name}-{c.name}" for c in CASES])
async def test_claude_code_fixture_signed(tmp_path, case):
    s, rep, recs = await _replay(tmp_path, case, signed=True)
    assert rep.ok, [e.to_dict() for e in rep.errors]
    assert s.stats["history_mismatch"] == 0
    assert s.stats["unmatched_user_text"] == 0
    assert s.stats["system_role"] >= 1          # Claude Code manda mensajes con role: "system" (H4)
    req = [r for r in recs if r.kind == "llm_request"][0]
    kinds = {next(x for x in recs if x.hash == h).kind for h in req.context}
    assert "user_prompt" in kinds


@pytest.mark.parametrize("case", CASES, ids=[f"{c.parent.name}-{c.name}" for c in CASES])
async def test_claude_code_fixture_unsigned_is_no_hook(tmp_path, case):
    s, rep, recs = await _replay(tmp_path, case, signed=False)
    assert rep.ok
    reasons = {r.meta["reason"] for r in recs if r.kind == "user_text_unsigned"}
    assert "no_hook" in reasons and s.stats["history_mismatch"] == 0
