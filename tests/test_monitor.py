"""Monitor de solo lectura (SPEC §17.5, G14): API con el TestClient de Starlette, incluido `--no-content`."""
from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from bench.synthetic import make_sessions
from openwitness.ledger import load_records
from openwitness.ledger.ledger import write_records
from openwitness.monitor import Monitor, create_app


@pytest.fixture
def env_sessions(tmp_path):
    import asyncio
    env, sess = asyncio.run(make_sessions(tmp_path, n=2))     # alice (inyección sutil) y mallory (firmada)
    return tmp_path, env, sess


def _client(root, env, **kw):
    mon = Monitor(root, env.registry, env.gateway_pubkey, **kw)
    return mon, TestClient(create_app(mon, poll=False))


def _snapshot(root):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_sessions_alerts_and_actions(env_sessions):
    root, env, sess = env_sessions
    before = _snapshot(root)
    mon, c = _client(root, env)
    with c:
        ss = c.get("/api/sessions").json()
        assert len(ss) == 2 and all(s["integrity"]["status"] in ("ok", "pending_anchor") for s in ss)
        by_user = {s["user"]: s for s in ss}
        assert by_user["alice"]["verdicts"].get("TOOL_INJECTED") == 1
        assert by_user["mallory"]["verdicts"].get("USER_INSTRUCTED")
        alice = by_user["alice"]["sid"]
        alerts = c.get("/api/alerts", params={"sid": alice}).json()
        inj = [a for a in alerts if a["type"] == "tool_injected"]
        assert inj and "parcial" in inj[0]["title"] and "mcp:issues" in inj[0]["title"]

        ents = c.get(f"/api/sessions/{alice}/entries").json()["entries"]
        assert ents[0]["kind"] == "session_start"
        assert {e["provenance"] for e in ents} >= {"user_signed", "mcp:issues", "model", "gateway"}
        call = next(e for e in ents if e["kind"] == "tool_call" and e["meta"]["tool"].endswith("send_email"))
        assert call["verdict"]["label"] == "TOOL_INJECTED" and call["verdict"]["partial"]
        tail = c.get(f"/api/sessions/{alice}/entries", params={"after": call["seq"]}).json()["entries"]
        assert all(e["seq"] > call["seq"] for e in tail)

        act = c.get(f"/api/sessions/{alice}/actions/{call['seq']}").json()
        assert act["verdict"]["rule"] == "R2b" and act["verdict"]["arg_provenance"]["to"] == ["mcp:issues"]
        hit = [i for i in act["context"] if "to" in i["matches"]]
        assert hit and hit[0]["provenance"] == "mcp:issues" and "reportes@evil.test" in hit[0]["fragment"]
        assert c.get(f"/api/sessions/{alice}/actions/0").status_code == 404

        rep = c.get(f"/api/sessions/{alice}/verify").json()
        assert rep["ok"] and rep["badge"]["status"] in ("ok", "pending_anchor")
    assert _snapshot(root) == before          # solo lectura: nada cambió en data/


def test_no_content_hides_blobs(env_sessions):
    root, env, sess = env_sessions
    mon, c = _client(root, env, no_content=True)
    with c:
        alice = next(s["sid"] for s in c.get("/api/sessions").json() if s["user"] == "alice")
        ents = c.get(f"/api/sessions/{alice}/entries").json()["entries"]
        assert all(e["fragment"] is None for e in ents) and all(e["summary"] for e in ents)
        call = next(e for e in ents if e["kind"] == "tool_call" and e["meta"]["tool"].endswith("send_email"))
        act = c.get(f"/api/sessions/{alice}/actions/{call['seq']}").json()
        assert all(i["fragment"] is None for i in act["context"])
        assert set(act["args"].values()) == {"•••"} and all(s["value"] is None for s in act["sensitive"])
        assert act["verdict"]["label"] == "TOOL_INJECTED"


def test_tamper_turns_session_to_fail_and_unverifiable(env_sessions):
    root, env, sess = env_sessions
    mon, c = _client(root, env)
    with c:
        mallory = sess[1]
        recs = load_records(mallory["ledger"])
        prompt_seq = next(r.seq for r in recs if r.kind == "user_prompt")
        write_records(mallory["ledger"], [r for r in recs if r.seq != prompt_seq])   # el operador borra el prompt
        st = os.stat(mallory["ledger"])
        os.utime(mallory["ledger"], ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        events = mon.refresh()
        assert any(e["type"] == "reset" for e in events)
        s = next(x for x in c.get("/api/sessions").json() if x["sid"] == mallory["sid"])
        assert s["integrity"]["status"] == "fail" and s["alerts"]["critical"] == 1
        ents = c.get(f"/api/sessions/{mallory['sid']}/entries").json()["entries"]
        calls = [e for e in ents if e["kind"] == "tool_call"]
        assert calls and all(e["verdict"]["label"] == "UNVERIFIABLE" for e in calls)


def test_live_append_and_partial_line(env_sessions):
    root, env, sess = env_sessions
    mon, c = _client(root, env)
    with c:
        path = sess[0]["ledger"]
        n = len(c.get(f"/api/sessions/{sess[0]['sid']}/entries").json()["entries"])
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"entry": {"seq": 999')                 # línea a medio escribir: se ignora
        assert not [e for e in mon.refresh() if e["type"] in ("entries", "reset")]
        assert len(c.get(f"/api/sessions/{sess[0]['sid']}/entries").json()["entries"]) == n


def test_token_required(env_sessions):
    root, env, _ = env_sessions
    mon = Monitor(root, env.registry, env.gateway_pubkey)
    with TestClient(create_app(mon, token="s3cret", poll=False)) as c:
        assert c.get("/api/sessions").status_code == 401
        assert c.get("/api/sessions", headers={"authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/sessions?token=s3cret").status_code == 200
