"""Hooks de Claude Code, `owit exec` y `owit hooks install` (SPEC §5.3, §7.6).

Los payloads son los de Claude Code 2.1.323 (§5.3). El gateway corre de verdad por HTTP en un hilo.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from bench.synthetic import make_env
from openwitness.integrations import claude_code
from openwitness.integrations.exec import ExecError, agent_env, detect_agent, run_exec
from openwitness.integrations.hooks import GatewayError, hook_pre_tool, hook_prompt, hook_session_start
from openwitness.ledger import load_records, verify_ledger
from openwitness.manifests import ManifestGate, TrustStore, sign_manifest
from openwitness.identity import KeyPair
from openwitness.serve import ServerThread
from openwitness.types import ToolDef

PROMPT = "Resume los issues abiertos del proyecto demo"
ISSUES = ToolDef("list_issues", "Lista los issues abiertos de un proyecto.",
                 {"type": "object", "properties": {"project": {"type": "string"}}})
TOOLS = [{"name": "mcp__issues__list_issues", "description": ISSUES.description, "input_schema": ISSUES.input_schema},
         {"name": "mcp__ghost__run", "description": "Servidor sin manifiesto.", "input_schema": {"type": "object"}},
         {"name": "Read", "description": "Lee un archivo.", "input_schema": {"type": "object"}}]


def prompt_payload(prompt=PROMPT):
    return json.dumps({"session_id": "cc-session-1", "transcript_path": "/tmp/t.jsonl", "cwd": "/tmp/work",
                       "permission_mode": "default", "hook_event_name": "UserPromptSubmit", "prompt": prompt,
                       "prompt_id": "p1"})


def tool_payload(tool, tool_use_id="toolu_1", mcp_server=None, tool_input=None):
    j = {"session_id": "cc-session-1", "cwd": "/tmp/work", "hook_event_name": "PreToolUse", "tool_name": tool,
         "tool_input": tool_input or {"project": "demo"}, "tool_use_id": tool_use_id, "effort": "medium"}
    if mcp_server:
        j["mcp_server"] = mcp_server
    return json.dumps(j)


@pytest.fixture
def live(tmp_path):
    """Gateway real por HTTP, con un gate que conoce el servidor `issues`, y una sesión de alice."""
    env = make_env(tmp_path / "gw")
    pub = KeyPair.generate()
    ts = TrustStore()
    ts.trust("issues", pub.public_str())
    ts.add_manifest(sign_manifest(pub, "issues", "1.0.0", "issues", [ISSUES]))
    env.gateway.gate = ManifestGate(ts)
    srv = ServerThread(env.gateway.app()).start()
    key = tmp_path / "keys" / "alice.key"
    env.users["alice"].save(key)
    j = httpx.post(srv.url + "/sessions", json={"user_id": "alice", "agent": {"name": "claude-code", "version": "t"}}).json()
    hook_env = {"OWIT_GATEWAY": srv.url, "OWIT_SESSION": j["sid"], "OWIT_TOKEN": j["token"], "OWIT_USER": "alice",
                "OWIT_USER_KEY": str(key), "OWIT_HOME": str(tmp_path / "owit-home")}
    try:
        yield {"env": env, "url": srv.url, "sid": j["sid"], "token": j["token"], "hook_env": hook_env, "key": key,
               "home": tmp_path / "owit-home", "tmp": tmp_path}
    finally:
        srv.stop()


def _messages(live, content, tools=TOOLS):
    r = httpx.post(f"{live['url']}/s/{live['sid']}/v1/messages?beta=true",
                   headers={"authorization": f"Bearer {live['token']}", "anthropic-version": "2023-06-01"},
                   json={"model": "fake-model", "max_tokens": 16, "stream": False, "tools": tools,
                         "messages": [{"role": "user", "content": content}]}, timeout=30)
    r.raise_for_status()
    return r


def _end_and_load(live):
    httpx.post(f"{live['url']}/s/{live['sid']}/end", headers={"x-api-key": live["token"]}, json={})
    return load_records(live["env"].gateway.ledger_path(live["sid"]))


# ---------------------------------------------------------------- owit hook prompt

def test_prompt_hook_signs_registers_and_saves_receipt(live):
    res = hook_prompt(prompt_payload(), live["hook_env"])
    assert (res.exit_code, res.stdout, res.stderr) == (0, "", "")      # nada en stdout: iría al contexto del modelo

    receipts = sorted((live["home"] / "receipts").glob("*.json"))
    assert len(receipts) == 1 and receipts[0].name.startswith(live["sid"] + "_")
    receipt = json.loads(receipts[0].read_text())
    assert receipt["body"]["session_id"] == live["sid"]

    _messages(live, PROMPT)                                             # el agente envía el mismo texto
    recs = _end_and_load(live)
    prompts = [r for r in recs if r.kind == "user_prompt"]
    assert len(prompts) == 1 and prompts[0].meta["user_id"] == "alice"
    assert prompts[0].meta["agent_session_id"] == "cc-session-1"
    assert prompts[0].seq == receipt["body"]["seq"] and prompts[0].hash == receipt["body"]["entry_hash"]
    assert not [r for r in recs if r.kind == "user_text_unsigned" and r.meta["reason"] in ("no_hook", "no_match")]
    gw = live["env"].gateway
    rep = verify_ledger(gw.ledger_path(live["sid"]), gw.blobs, gw.kp.public_str(), live["env"].registry,
                        live["env"].anchor, [receipt])
    assert rep.ok, [e.to_dict() for e in rep.errors]


def test_prompt_hook_does_nothing_outside_owit_exec(tmp_path):
    res = hook_prompt(prompt_payload(), {"OWIT_HOME": str(tmp_path)})
    assert (res.exit_code, res.stdout, res.stderr) == (0, "", "")
    assert not (tmp_path / "receipts").exists()


@pytest.mark.parametrize("break_env", [
    {"OWIT_USER_KEY": "/no/existe.key"},
    {"OWIT_GATEWAY": "http://127.0.0.1:9"},          # nadie escucha ahí
    {"OWIT_TOKEN": "token-equivocado"},
    {"OWIT_USER": "mallory"},                         # llave de alice, usuario de otra persona
])
def test_prompt_hook_failure_warns_or_blocks_but_never_writes_stdout(live, break_env):
    env = {**live["hook_env"], **break_env}
    soft = hook_prompt(prompt_payload(), env)
    assert soft.exit_code == 0 and soft.stdout == "" and "sin firma" in soft.stderr
    strict = hook_prompt(prompt_payload(), {**env, "OWIT_STRICT": "1"})
    assert strict.exit_code == 2 and strict.stdout == "" and "modo estricto" in strict.stderr
    assert not (live["home"] / "receipts").exists()


def test_prompt_hook_with_garbage_stdin(live):
    assert hook_prompt("no es json", live["hook_env"]).exit_code == 0
    assert hook_prompt("no es json", {**live["hook_env"], "OWIT_STRICT": "1"}).exit_code == 2
    assert hook_prompt(json.dumps({"prompt": 42}), live["hook_env"]).stdout == ""


def test_prompt_hook_does_not_leak_the_key_in_messages(live):
    secret = live["key"].read_text().strip()
    res = hook_prompt(prompt_payload(), {**live["hook_env"], "OWIT_GATEWAY": "http://127.0.0.1:9", "OWIT_STRICT": "1"})
    assert secret not in res.stderr and secret not in res.stdout


# ---------------------------------------------------------------- owit hook pre-tool

def test_pre_tool_allows_accepted_and_denies_rejected(live):
    _messages(live, "hola")                           # el gateway filtra `tools` y decide

    ok = hook_pre_tool(tool_payload("mcp__issues__list_issues", "toolu_a", "issues"), live["hook_env"])
    assert (ok.exit_code, ok.stdout) == (0, "")       # permitir = no decir nada

    read = hook_pre_tool(tool_payload("Read", "toolu_b", tool_input={"file_path": "x"}), live["hook_env"])
    assert (read.exit_code, read.stdout) == (0, "")

    bad = hook_pre_tool(tool_payload("mcp__ghost__run", "toolu_c", "ghost"), live["hook_env"])
    out = json.loads(bad.stdout)["hookSpecificOutput"]
    assert bad.exit_code == 0
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "unsigned" in out["permissionDecisionReason"] and "mcp__ghost__run" in out["permissionDecisionReason"]

    never_seen = hook_pre_tool(tool_payload("mcp__otro__x", "toolu_d", "otro"), live["hook_env"])
    assert json.loads(never_seen.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

    checks = {r.meta["tool"]: r.meta["decision"] for r in _end_and_load(live) if r.kind == "tool_check"}
    assert checks == {"mcp__issues__list_issues": "allow", "Read": "allow", "mcp__ghost__run": "deny",
                      "mcp__otro__x": "deny"}


def test_pre_tool_does_nothing_outside_owit_exec():
    res = hook_pre_tool(tool_payload("Bash"), {})
    assert (res.exit_code, res.stdout, res.stderr) == (0, "", "")


def test_pre_tool_when_gateway_is_down(live):
    env = {**live["hook_env"], "OWIT_GATEWAY": "http://127.0.0.1:9"}
    soft = hook_pre_tool(tool_payload("Read"), env)
    assert soft.exit_code == 0 and soft.stdout == "" and "no se pudo verificar" in soft.stderr
    strict = hook_pre_tool(tool_payload("Read"), {**env, "OWIT_STRICT": "1"})
    assert json.loads(strict.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_tool_sends_what_the_gateway_expects(live):
    sent = {}

    def post(url, token, obj):
        sent.update(url=url, token=token, obj=obj)
        return 200, {"decision": "allow", "reason": "ok"}

    hook_pre_tool(tool_payload("mcp__issues__list_issues", "toolu_z", "issues"), live["hook_env"], post)
    assert sent["url"] == f"{live['url']}/s/{live['sid']}/tool-check" and sent["token"] == live["token"]
    assert sent["obj"] == {"tool": "mcp__issues__list_issues", "args": {"project": "demo"}, "tool_use_id": "toolu_z",
                           "mcp_server": "issues"}

    def boom(url, token, obj):
        raise GatewayError("sin red")
    assert hook_pre_tool(tool_payload("Read"), live["hook_env"], boom).stdout == ""


# ---------------------------------------------------------------- owit hook session-start

def test_session_start_records_the_agent_session_and_is_silent(live):
    payload = json.dumps({"session_id": "cc-session-1", "cwd": "/tmp/work", "hook_event_name": "SessionStart",
                          "source": "startup"})
    for _ in range(2):
        res = hook_session_start(payload, live["hook_env"])
        assert (res.exit_code, res.stdout, res.stderr) == (0, "", "")
    data = json.loads((live["home"] / "sessions" / f"{live['sid']}.json").read_text())
    assert data["sid"] == live["sid"] and data["agent_session_ids"] == ["cc-session-1"] and data["user"] == "alice"
    assert live["token"] not in json.dumps(data)                       # el token no se guarda en disco


# ---------------------------------------------------------------- los hooks como procesos (`owit hook …`)

def _owit_hook(kind, payload, env):
    full = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join(sys.path), **env}
    return subprocess.run([sys.executable, "-m", "openwitness.cli", "hook", kind], input=payload, text=True,
                          env=full, capture_output=True, timeout=60)


def test_hooks_as_subprocesses(live):
    p = _owit_hook("prompt", prompt_payload(), live["hook_env"])
    assert (p.returncode, p.stdout) == (0, ""), p.stderr
    assert len(list((live["home"] / "receipts").glob("*.json"))) == 1
    _messages(live, PROMPT)
    d = _owit_hook("pre-tool", tool_payload("mcp__ghost__run", "toolu_c", "ghost"), live["hook_env"])
    assert d.returncode == 0 and json.loads(d.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    a = _owit_hook("pre-tool", tool_payload("Read"), live["hook_env"])
    assert (a.returncode, a.stdout) == (0, "")
    s = _owit_hook("prompt", prompt_payload(), {**live["hook_env"], "OWIT_USER_KEY": "/no/existe", "OWIT_STRICT": "1"})
    assert s.returncode == 2 and s.stdout == "" and "modo estricto" in s.stderr


# ---------------------------------------------------------------- owit exec

AGENT = r"""
import json, os, sys, subprocess
out = {k: v for k, v in os.environ.items() if k.startswith(("OWIT_", "ANTHROPIC_", "ENABLE_", "CLAUDE_CODE_OAUTH"))}
out["argv"] = sys.argv[1:]
# el "agente" dispara su hook de prompt, como haría Claude Code
p = subprocess.run([sys.executable, "-m", "openwitness.cli", "hook", "prompt"], text=True, capture_output=True,
                   input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "cc-1", "prompt": "hola"}))
out["hook"] = [p.returncode, p.stdout]
open(os.environ["OUT_FILE"], "w").write(json.dumps(out))
sys.exit(7)
"""


def test_exec_creates_session_redirects_agent_and_closes(live, tmp_path):
    out_file = tmp_path / "agent-out.json"
    base = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join(sys.path), "OUT_FILE": str(out_file),
            "OWIT_HOME": str(live["home"]), "ANTHROPIC_API_KEY": "sk-real-del-usuario", "CLAUDE_CODE_OAUTH_TOKEN": "x",
            "OWIT_STRICT": "1"}
    code = run_exec([sys.executable, "-c", AGENT, "-p", "x"], "alice", live["key"], live["url"], agent="claude-code",
                    env=base, version="2.1.323")
    assert code == 7                                                   # el código de salida del agente
    seen = json.loads(out_file.read_text())
    sid, token = seen["OWIT_SESSION"], seen["OWIT_TOKEN"]
    assert sid != live["sid"]                                          # una sesión nueva para este agente
    assert seen["ANTHROPIC_BASE_URL"] == f"{live['url']}/s/{sid}" and seen["ANTHROPIC_AUTH_TOKEN"] == token
    assert seen["ENABLE_TOOL_SEARCH"] == "false"
    assert (seen["OWIT_GATEWAY"], seen["OWIT_USER"]) == (live["url"], "alice")
    assert Path(seen["OWIT_USER_KEY"]).is_absolute() and KeyPair.load(seen["OWIT_USER_KEY"]).public_str() == \
        live["env"].users["alice"].public_str()
    assert "ANTHROPIC_API_KEY" not in seen and "CLAUDE_CODE_OAUTH_TOKEN" not in seen   # el agente no ve la key real
    assert "OWIT_STRICT" not in seen                                   # sin --strict no se hereda
    assert seen["argv"] == ["-p", "x"] and seen["hook"] == [0, ""]

    gw = live["env"].gateway
    recs = load_records(gw.ledger_path(sid))
    kinds = [r.kind for r in recs]
    assert kinds[0] == "session_start" and "user_prompt" in kinds and kinds[-1] == "anchor"   # cerrada y anclada
    assert recs[0].meta["agent"] == {"name": "claude-code", "version": "2.1.323"}
    assert verify_ledger(gw.ledger_path(sid), gw.blobs, gw.kp.public_str(), live["env"].registry, live["env"].anchor).ok


def test_exec_strict_and_signed_prompt_argument(live, tmp_path):
    out_file = tmp_path / "agent-out.json"
    script = "import json,os,sys; open(os.environ['OUT_FILE'],'w').write(json.dumps([sys.argv[1:], os.environ.get('OWIT_STRICT')]))"
    base = {"PATH": os.environ.get("PATH", ""), "OUT_FILE": str(out_file), "OWIT_HOME": str(live["home"])}
    code = run_exec([sys.executable, "-c", script, "-p"], "alice", live["key"], live["url"], strict=True,
                    prompt="Resume los issues", env=base, version="1")
    assert code == 0
    argv, strict = json.loads(out_file.read_text())
    assert argv == ["-p", "Resume los issues"] and strict == "1"
    (sid,) = set(live["env"].gateway.sessions) - {live["sid"]}          # la sesión que creó exec
    recs = load_records(live["env"].gateway.ledger_path(sid))
    assert recs[0].meta["policy"]["require_signed_prompts"] is True
    assert [r.kind for r in recs].count("user_prompt") == 1
    assert len(list((live["home"] / "receipts").glob(f"{sid}_*.json"))) == 1


def test_exec_errors_are_explained(live, tmp_path):
    with pytest.raises(ExecError, match="owit keygen"):
        run_exec(["true"], "alice", tmp_path / "no.key", live["url"])
    with pytest.raises(ExecError, match="no responde"):
        run_exec(["true"], "alice", live["key"], "http://127.0.0.1:9", version="1")
    with pytest.raises(ExecError, match="falta el comando"):
        run_exec([], "alice", live["key"], live["url"])
    with pytest.raises(ExecError, match="no se encontró"):
        run_exec(["/no/existe/agente"], "alice", live["key"], live["url"], version="1")


def test_agent_detection_and_env():
    assert detect_agent(["claude", "-p", "x"]) == "claude-code" and detect_agent(["/usr/bin/claude"]) == "claude-code"
    assert detect_agent(["python3"]) == "python3"
    env = agent_env({"HOME": "/h", "ANTHROPIC_BASE_URL": "https://otra"}, "otro-agente", "http://gw", "s1", "t1", "bob",
                    Path("keys/bob.key"), strict=True)
    assert "ANTHROPIC_BASE_URL" not in env and env["OWIT_STRICT"] == "1" and env["HOME"] == "/h"


# ---------------------------------------------------------------- owit hooks install claude-code

def test_hooks_config_points_to_the_three_commands():
    cfg = claude_code.hooks_config("/opt/bin/owit")
    assert set(cfg) == {"SessionStart", "UserPromptSubmit", "PreToolUse"}
    assert cfg["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": "/opt/bin/owit hook prompt"}]}]
    assert cfg["PreToolUse"][0]["matcher"] == "*"
    assert cfg["PreToolUse"][0]["hooks"][0]["command"] == "/opt/bin/owit hook pre-tool"
    assert cfg["SessionStart"][0]["hooks"][0]["command"] == "/opt/bin/owit hook session-start"


def test_install_keeps_existing_settings_and_is_idempotent(tmp_path):
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "mi-linter"}]}
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": [mine]}}))

    path, old, new = claude_code.plan_install(tmp_path, "owit")
    assert path == settings and settings.read_text() == old           # planear no escribe
    d = claude_code.diff(path, old, new)
    assert "+" in d and "owit hook pre-tool" in d
    claude_code.write(path, new)

    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Read"]}
    assert data["hooks"]["PreToolUse"][0] == mine and len(data["hooks"]["PreToolUse"]) == 2
    assert claude_code.plan_install(tmp_path, "owit")[1:] == (new, new)             # segunda vez: sin cambios
    _, _, moved = claude_code.plan_install(tmp_path, "/otra/ruta/owit")             # cambia la ruta: reemplaza
    assert json.dumps(json.loads(moved)).count("hook pre-tool") == 1


def test_install_cli_shows_diff_before_writing(tmp_path):
    from typer.testing import CliRunner

    from openwitness.cli import app
    run = CliRunner().invoke
    settings = tmp_path / ".claude" / "settings.local.json"

    r = run(app, ["hooks", "install", "claude-code", "--project", str(tmp_path), "--command", "owit", "--dry-run"])
    assert r.exit_code == 0 and "owit hook prompt" in r.output and not settings.exists()
    r = run(app, ["hooks", "install", "claude-code", "--project", str(tmp_path), "--command", "owit"], input="n\n")
    assert r.exit_code == 1 and not settings.exists()                  # sin confirmar no escribe
    r = run(app, ["hooks", "install", "claude-code", "--project", str(tmp_path), "--command", "owit"], input="y\n")
    assert r.exit_code == 0 and "owit hook prompt" in r.output
    assert json.loads(settings.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "owit hook prompt"
    r = run(app, ["hooks", "install", "claude-code", "--project", str(tmp_path), "--command", "owit"])
    assert r.exit_code == 0 and "ya están instalados" in r.output
    assert run(app, ["hooks", "install", "codex", "--project", str(tmp_path)]).exit_code != 0

    settings.write_text("{roto")
    assert run(app, ["hooks", "install", "claude-code", "--project", str(tmp_path), "--yes"]).exit_code != 0
    assert settings.read_text() == "{roto"                             # no pisa un archivo que no entiende
