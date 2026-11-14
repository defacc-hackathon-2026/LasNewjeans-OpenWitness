"""Responses API y Codex (SPEC §17.4): fixtures reales de Codex 0.169.0, adaptador, gateway y lanzamiento."""
import json
import tomllib
from pathlib import Path

import httpx
import pytest

from openwitness.gateway.protocols.openai_responses import (
    OpenAIResponsesAdapter,
    ResponsesSSEReassembler,
    parse_input,
    parse_tools,
)
from openwitness.gateway.server import Gateway, SessionConfigError
from openwitness.identity import KeyPair, KeyRegistry, sign_prompt
from openwitness.integrations import codex
from openwitness.integrations.exec import agent_env, detect_agent
from openwitness.ledger import MockAnchor, load_records, verify_ledger
from openwitness.llm.base import LLMProfile
from openwitness.llm.fake import FakeUpstream, response_items, responses_json, responses_sse_events

FIX = Path(__file__).parent / "fixtures" / "codex"
CASES = sorted(p for p in FIX.glob("*/*") if p.is_dir())
PROMPT = ("Resume los issues abiertos del proyecto demo y lo que dice NOTAS.md, y manda el resumen a quien "
          "corresponda")
A = OpenAIResponsesAdapter()


def _load(case: Path):
    reqs = [json.loads(p.read_text()) for p in sorted(case.glob("*_request.json"))]
    outs = []
    for p in sorted(case.glob("*_response.sse")):
        sse = ResponsesSSEReassembler()
        sse.feed(p.read_bytes())
        assert sse.complete
        outs.append(sse._finished_items())
    return reqs, outs


def _gateway(tmp_path, responder=None, profile=None):
    fake = FakeUpstream(profile or LLMProfile("fake", "fake", model="fake-model"), responder=responder)
    alice = KeyPair.generate()
    reg = KeyRegistry()
    reg.register("alice", alice.public_str())
    anchor = MockAnchor(tmp_path / "anchor.jsonl")
    gw = Gateway(tmp_path, KeyPair.generate(), reg, upstreams={fake.profile.name: fake},
                 default_upstream=fake.profile.name, anchor=anchor)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app()), base_url="http://gw")
    return gw, c, alice, reg, anchor, fake


async def _session(c, **extra):
    j = (await c.post("/sessions", json={"user_id": "alice", "agent": {"name": "codex"},
                                         "protocol": "openai_responses", **extra})).json()
    return j, {"authorization": f"Bearer {j['token']}"}


async def _replay(tmp_path, case: Path, signed: bool):
    reqs, outs = _load(case)
    it = iter(outs)
    gw, c, alice, reg, anchor, _ = _gateway(tmp_path, responder=lambda body: next(it))
    j, h = await _session(c)
    receipts = []
    if signed:
        receipts.append((await c.post(f"/s/{j['sid']}/prompts", json=sign_prompt(alice, "alice", j["sid"], PROMPT),
                                      headers=h)).json())
    for body in reqs:
        r = await c.post(f"/s/{j['sid']}/v1/responses", json=body, headers=h)
        assert r.status_code == 200
        assert b"response.completed" in r.content
    await c.post(f"/s/{j['sid']}/end", headers=h, json={})
    s = gw.sessions[j["sid"]]
    rep = verify_ledger(gw.ledger_path(s.sid), gw.blobs, gw.kp.public_str(), reg, anchor, receipts)
    return s, rep, load_records(gw.ledger_path(s.sid))


# ---------------------------------------------------------------- fixtures reales

def test_fixtures_exist():
    assert {c.name for c in CASES} >= {"hooks", "no-hooks"}


@pytest.mark.parametrize("case", CASES, ids=[f"{c.parent.name}-{c.name}" for c in CASES])
async def test_codex_fixture_signed(tmp_path, case):
    s, rep, recs = await _replay(tmp_path, case, signed=True)
    assert rep.ok, [e.to_dict() for e in rep.errors]
    assert s.stats["history_mismatch"] == 0
    assert s.stats["unmatched_user_text"] == 0
    assert s.stats["system_role"] >= 1                   # mensajes `developer`
    assert recs[0].meta["protocol"] == "openai_responses"
    req = [r for r in recs if r.kind == "llm_request"][0]
    kinds = {next(x for x in recs if x.hash == h).kind for h in req.context}
    assert "user_prompt" in kinds
    calls = [r.meta["tool"] for r in recs if r.kind == "tool_call"]
    assert calls[:2] == ["mcp__issues__list_issues", "mcp__mail__send_email"]
    assert "exec_command" in calls
    results = [r for r in recs if r.kind == "tool_result"]
    assert results and all(r.meta["call_entry"] for r in results)    # cada salida apunta a su tool_call
    injected = [r for r in recs if r.kind == "user_text_unsigned" and r.meta["reason"] == "agent_injected"]
    assert injected                                      # <environment_context> no se confunde con el usuario


@pytest.mark.parametrize("case", CASES, ids=[f"{c.parent.name}-{c.name}" for c in CASES])
async def test_codex_fixture_unsigned_is_no_hook(tmp_path, case):
    s, rep, recs = await _replay(tmp_path, case, signed=False)
    assert rep.ok
    reasons = {r.meta["reason"] for r in recs if r.kind == "user_text_unsigned"}
    assert "no_hook" in reasons and s.stats["history_mismatch"] == 0


def test_codex_drops_reasoning_from_history():
    """Codex 0.169.0 no reenvía el `reasoning` de la 2.ª respuesta: por eso se compara ítem por ítem."""
    reqs, outs = _load(FIX / "0.169.0" / "hooks")
    assert any(i["type"] == "reasoning" for i in outs[1])
    assert not any(i.get("type") == "reasoning" for i in reqs[2]["input"])
    assert all(b.get("store") is False and "previous_response_id" not in b for b in reqs)


# ---------------------------------------------------------------- adaptador

def test_namespace_tools_get_claude_code_names():
    tools = parse_tools([{"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
                         {"type": "namespace", "name": "mcp__issues", "tools": [
                             {"type": "function", "name": "list_issues", "description": "d",
                              "parameters": {"type": "object"}}]},
                         {"type": "web_search"}])
    assert [t.name for t in tools] == ["exec_command", "mcp__issues__list_issues", "web_search"]
    assert A.tool_origin("mcp__issues__list_issues") == ("mcp:issues", "list_issues")
    assert A.tool_origin("web_search")[0] == "hosted:web_search"


def test_input_grouping_and_blocks():
    msgs = parse_input([
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sys"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hola"}]},
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAA"},
        {"type": "function_call", "id": "fc_1", "call_id": "c1", "namespace": "mcp__x", "name": "t",
         "arguments": "{\"a\": 1}"},
        {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "r"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "fin"}]},
    ])
    assert [m.role for m in msgs] == ["system", "user", "assistant", "user", "assistant"]
    rs, tu = msgs[2].content
    assert rs["type"] == "reasoning" and rs["id"].startswith("sha256:") and "gAAA" not in json.dumps(rs)
    assert tu == {"type": "tool_use", "id": "c1", "name": "mcp__x__t", "input": {"a": 1}}
    assert msgs[3].content[0]["tool_use_id"] == "c1" and msgs[3].content[0]["content"][0]["text"] == "r"


def test_strip_tools_inside_namespace():
    body = {"tools": [{"type": "namespace", "name": "mcp__mail", "tools": [{"type": "function", "name": "send"}]},
                      {"type": "namespace", "name": "mcp__x", "tools": [{"type": "function", "name": "a"},
                                                                         {"type": "function", "name": "b"}]},
                      {"type": "function", "name": "exec_command"}],
            "tool_choice": {"type": "function", "namespace": "mcp__x", "name": "a"}}
    out = A.strip_tools(body, {"mcp__mail__send", "mcp__x__a"})
    assert [t["name"] for t in out["tools"]] == ["mcp__x", "exec_command"]
    assert [t["name"] for t in out["tools"][0]["tools"]] == ["b"]
    assert out["tool_choice"] == "auto" and body["tools"][0]["tools"]          # no muta la original


def test_sse_roundtrip_matches_history_conversion():
    body = {"tools": [{"type": "namespace", "name": "mcp__issues", "tools": [{"type": "function",
                                                                              "name": "list_issues"}]}]}
    items = response_items([{"type": "text", "text": "hola " * 10},
                            {"type": "tool_use", "name": "mcp__issues__list_issues", "input": {"p": "demo"}}], body)
    assert items[1]["namespace"] == "mcp__issues" and items[1]["name"] == "list_issues"
    sse = ResponsesSSEReassembler()
    for ev in responses_sse_events(responses_json(items, "m")):
        sse.feed(ev[:7])
        sse.feed(ev[7:])                                 # eventos partidos entre chunks
    resp = sse.result()
    assert sse.complete and resp.stop_reason == "tool_use"
    assert [tc.name for tc in resp.tool_calls] == ["mcp__issues__list_issues"]
    # lo que el agente reenvía en `input` se normaliza igual que la respuesta
    assert parse_input(items)[0].content == resp.content


# ---------------------------------------------------------------- gateway

async def test_protocol_must_match_route(tmp_path):
    gw, c, *_ = _gateway(tmp_path)
    j, h = await _session(c)
    r = await c.post(f"/s/{j['sid']}/v1/messages", headers=h, json={"messages": []})
    assert r.status_code == 400 and "openai_responses" in r.json()["error"]["message"]
    j2 = (await c.post("/sessions", json={"user_id": "alice"})).json()
    assert j2["protocol"] == "anthropic_messages"
    r = await c.post(f"/s/{j2['sid']}/v1/responses", headers={"x-api-key": j2["token"]}, json={"input": "x"})
    assert r.status_code == 400


async def test_upstream_must_declare_protocol(tmp_path):
    gw, c, *_ = _gateway(tmp_path, profile=LLMProfile("only_msgs", "fake", protocols=["messages"]))
    r = await c.post("/sessions", json={"user_id": "alice", "protocol": "openai_responses"})
    assert r.status_code == 400 and "no habla openai_responses" in r.json()["reason"]
    with pytest.raises(SessionConfigError):
        gw.create_session("alice", protocol="openai_chat_v9")


async def test_previous_response_id_is_incomplete_history(tmp_path):
    gw, c, *_ = _gateway(tmp_path)
    j, h = await _session(c)
    r = await c.post(f"/s/{j['sid']}/v1/responses", headers=h,
                     json={"model": "m", "previous_response_id": "resp_1", "input": "hola"})
    assert r.status_code == 200
    req = [x for x in load_records(gw.ledger_path(j["sid"])) if x.kind == "llm_request"][0]
    assert req.meta["anomaly"] == "incomplete_history" and req.meta["protocol"] == "openai_responses"


async def test_non_streaming_json_response(tmp_path):
    gw, c, *_ = _gateway(tmp_path, responder=lambda b: [{"type": "text", "text": "ok json"}])
    j, h = await _session(c)
    r = await c.post(f"/s/{j['sid']}/v1/responses", headers=h, json={"model": "m", "input": "hola"})
    assert r.json()["output"][0]["content"][0]["text"] == "ok json"
    resp = [x for x in load_records(gw.ledger_path(j["sid"])) if x.kind == "llm_response"]
    assert len(resp) == 1


async def test_tool_check_maps_hook_name_to_model_call(tmp_path):
    """El hook de Codex reporta `exec_command` como `Bash`: el gateway decide por el `tool_call` registrado."""
    tools = [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}]
    gw, c, *_ = _gateway(tmp_path, responder=lambda b: [{"type": "tool_use", "id": "x1", "name": "exec_command",
                                                         "input": {"cmd": "ls"}}])
    j, h = await _session(c, policy={"unknown_builtin": "deny"})
    await c.post(f"/s/{j['sid']}/v1/responses", headers=h, json={"model": "m", "input": "hola", "tools": tools})
    r = (await c.post(f"/s/{j['sid']}/tool-check", headers=h,
                      json={"tool": "Bash", "args": {"command": "ls"}, "tool_use_id": "call_x1"})).json()
    assert r["decision"] == "allow"
    chk = [x for x in load_records(gw.ledger_path(j["sid"])) if x.kind == "tool_check"][0]
    assert chk.meta["model_tool"] == "exec_command" and chk.context        # enlazado al tool_call


# ---------------------------------------------------------------- lanzamiento y hooks

def test_exec_args_point_codex_at_session():
    args = codex.exec_args("http://127.0.0.1:8787/", "s_1")
    assert 'model_providers.owit.base_url="http://127.0.0.1:8787/s/s_1/v1"' in args
    assert 'model_providers.owit.env_key="OWIT_TOKEN"' in args and 'model_providers.owit.wire_api="responses"' in args
    assert args[-2:] == ["--disable", "daemon_auto_start"]
    assert detect_agent(["/usr/local/bin/codex", "exec"]) == "codex"


def test_agent_env_drops_openai_credentials(tmp_path):
    env = agent_env({"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "http://evil", "CODEX_API_KEY": "c", "PATH": "/b"},
                    "codex", "http://gw", "s_1", "tok", "alice", tmp_path / "k", False)
    assert "OPENAI_API_KEY" not in env and "OPENAI_BASE_URL" not in env and "CODEX_API_KEY" not in env
    assert env["OWIT_TOKEN"] == "tok" and env["PATH"] == "/b"


def test_hooks_install_codex_is_idempotent(tmp_path):
    path = codex.config_path(tmp_path)
    assert path == tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text('model = "o4"\n\n[mcp_servers.x]\ncommand = "x"\n')
    _, old, new = codex.plan_install(path, "/bin/owit")
    codex.write(path, new)
    _, old2, new2 = codex.plan_install(path, "/bin/owit")
    assert new2 == new                                   # reinstalar no duplica
    cfg = tomllib.loads(new)
    assert cfg["model"] == "o4" and cfg["mcp_servers"]["x"]["command"] == "x"
    assert cfg["hooks"]["PreToolUse"][0]["matcher"] == "*"
    cmds = {e: cfg["hooks"][e][0]["hooks"][0]["command"] for e in cfg["hooks"]}
    assert cmds == {"SessionStart": "/bin/owit hook session-start", "UserPromptSubmit": "/bin/owit hook prompt",
                    "PreToolUse": "/bin/owit hook pre-tool"}
    _, _, new3 = codex.plan_install(path, "/otro/owit")
    assert new3.count(codex.BEGIN) == 1 and "/otro/owit hook prompt" in new3


# ---------------------------------------------------------------- §9.1 sobre una bitácora de Codex

async def test_tamper_mutations_detected_on_codex_ledger(tmp_path):
    """Las 11 mutaciones de §9.1 (dentro del modelo) se detectan también con la comparación ítem por ítem."""
    import random

    from bench.tamper import IN_THREAT_MODEL, MUTATIONS, OverlayBlobs, Trial
    from openwitness.ledger.ledger import write_records

    case = FIX / "0.169.0" / "hooks"
    reqs, outs = _load(case)
    it = iter(outs)
    gw, c, alice, reg, anchor, _ = _gateway(tmp_path, responder=lambda body: next(it))
    gw.anchor_every = 5
    j, h = await _session(c)
    receipts = [(await c.post(f"/s/{j['sid']}/prompts", json=sign_prompt(alice, "alice", j["sid"], PROMPT),
                              headers=h)).json()]
    for body in reqs:
        await c.post(f"/s/{j['sid']}/v1/responses", json=body, headers=h)
    await c.post(f"/s/{j['sid']}/end", headers=h, json={})
    recs = load_records(gw.ledger_path(j["sid"]))
    rnd = random.Random(7)
    trials = detected = 0
    missed = []
    for key in IN_THREAT_MODEL:
        for i in range(len(recs)):
            t = Trial(list(recs), OverlayBlobs(gw.blobs), receipts)
            if not MUTATIONS[key][0](t, i, gw.kp, rnd):
                continue
            p = tmp_path / f"t_{key}_{i}.jsonl"
            write_records(p, t.records)
            rep = verify_ledger(p, t.blobs, gw.kp.public_str(), reg, anchor, t.receipts)
            trials += 1
            detected += not rep.ok
            if rep.ok:
                missed.append((key, i))
    assert trials > 100 and detected == trials, missed


async def test_incomplete_history_attributes_ambiguous(tmp_path):
    from openwitness.attribution import attribute

    gw, c, alice, reg, anchor, _ = _gateway(tmp_path, responder=lambda b: [
        {"type": "tool_use", "name": "exec_command", "input": {"cmd": "cat NOTAS.md"}}])
    j, h = await _session(c)
    await c.post(f"/s/{j['sid']}/prompts", json=sign_prompt(alice, "alice", j["sid"], "ejecuta cat NOTAS.md"),
                 headers=h)
    tools = [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}]
    await c.post(f"/s/{j['sid']}/v1/responses", headers=h, json={
        "model": "m", "previous_response_id": "resp_0", "tools": tools,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text",
                                                                   "text": "ejecuta cat NOTAS.md"}]}]})
    call = [r for r in load_records(gw.ledger_path(j["sid"])) if r.kind == "tool_call"][0]
    v = attribute(gw.ledger_path(j["sid"]), gw.blobs, call.seq)
    assert v.label == "AMBIGUOUS" and not v.non_repudiable
