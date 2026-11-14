"""Claude Code real a través del gateway, con upstream `fake` (sin API key, sin gastar tokens).

Graba las peticiones como fixtures (`tests/fixtures/claude_code/<versión>/`) y comprueba que la descomposición
deja 0 textos sin emparejar y 0 `history_mismatch` (§9.2, validación con agentes reales).

    uv run python -m bench.claude_code_probe [--tool-search] [--save-fixtures]

Usa un `CLAUDE_CONFIG_DIR` vacío (no toca `~/.claude`) y quita `ANTHROPIC_API_KEY` del entorno.
Los hooks de firma (`owit hook prompt`) son de Juliana: sin ellos, el prompt queda como `no_hook` (L8).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from openwitness.identity import KeyPair, KeyRegistry
from openwitness.ledger import load_records, verify_ledger
from openwitness.llm.base import LLMProfile
from openwitness.llm.fake import FakeUpstream, ScriptedResponder
from openwitness.gateway.server import Gateway
from openwitness.serve import ServerThread

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "claude_code"

SCRIPT = [
    {"tool_use": {"name": "Read", "input": {"file_path": "{work}/NOTAS.md"}}},
    {"text": "Las notas dicen que el proyecto demo tiene dos issues abiertos."},
]


def claude_version() -> str:
    out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout
    return out.split()[0] if out else "unknown"


def scrub(s: str, tmp: Path) -> str:
    for t in sorted({str(tmp), str(tmp.resolve())}, key=len, reverse=True):
        s = s.replace(t, "/tmp/owit-cc")
    return s.replace(str(Path.home()), "/home/user")


def sanitize(obj, tmp: Path):
    """Quita identificadores del dispositivo y rutas locales antes de versionar una fixture (§10.1)."""
    s = scrub(json.dumps(obj, ensure_ascii=False), tmp)
    d = json.loads(s)
    if isinstance(d, dict) and "metadata" in d:
        d["metadata"] = {"user_id": "<redactado: device_id y session_id de Claude Code>"}
    return d


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool-search", action="store_true", help="dejar la carga diferida de herramientas activa")
    ap.add_argument("--save-fixtures", action="store_true")
    ap.add_argument("--prompt", default="Lee el archivo NOTAS.md y resúmelo en una frase.")
    a = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="owit-cc-"))
    work, home, rec = tmp / "work", tmp / "claude-home", tmp / "requests"
    for d in (work, home, rec):
        d.mkdir(parents=True)
    (work / "NOTAS.md").write_text("# Notas\nEl proyecto demo tiene dos issues abiertos.\n")
    steps = json.loads(json.dumps(SCRIPT).replace("{work}", str(work)))
    fake = FakeUpstream(LLMProfile("fake", "fake", model="fake-model"), responder=ScriptedResponder(steps))
    gw = Gateway(tmp / "data", KeyPair.generate(), KeyRegistry(), upstreams={"fake": fake},
                 default_upstream="fake", record_dir=rec, anchor_every=10)
    srv = ServerThread(gw.app()).start()
    version = claude_version()
    j = httpx.post(srv.url + "/sessions", json={"user_id": "alice",
                                                "agent": {"name": "claude-code", "version": version}}).json()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
    env.update({"ANTHROPIC_BASE_URL": f"{srv.url}/s/{j['sid']}", "ANTHROPIC_AUTH_TOKEN": j["token"],
                "CLAUDE_CONFIG_DIR": str(home), "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    if not a.tool_search:
        env["ENABLE_TOOL_SEARCH"] = "false"
    p = subprocess.run(["claude", "-p", a.prompt, "--output-format", "json", "--allowedTools", "Read"],
                       cwd=work, env=env, capture_output=True, text=True, timeout=180)
    httpx.post(srv.url + f"/s/{j['sid']}/end", headers={"x-api-key": j["token"]}, json={"reason": "probe"})
    sess = gw.sessions[j["sid"]]
    rep = verify_ledger(gw.ledger_path(j["sid"]), gw.blobs, gw.kp.public_str(), gw.registry, gw.anchor)
    recs = load_records(gw.ledger_path(j["sid"]))
    reasons: dict[str, int] = {}
    for r in recs:
        if r.kind == "user_text_unsigned":
            reasons[r.meta["reason"]] = reasons.get(r.meta["reason"], 0) + 1
    result = {"claude_code": version, "tool_search": a.tool_search, "exit_code": p.returncode,
              "claude_result": (json.loads(p.stdout).get("result") if p.stdout.strip().startswith("{") else
                                p.stdout[-500:]),
              "stderr": p.stderr[-500:], "integrity_ok": rep.ok, "errors": [e.to_dict() for e in rep.errors][:5],
              "stats": sess.stats, "unsigned_reasons": reasons, "entries": len(recs),
              "kinds": sorted({r.kind for r in recs}), "upstream_requests": len(fake.requests)}
    print(json.dumps(result, indent=1, ensure_ascii=False))
    if a.save_fixtures:
        dest = FIXTURES / version / ("tool-search" if a.tool_search else "no-tool-search")
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for f in sorted(rec.iterdir()):
            if f.suffix == ".json":
                (dest / f.name.split("_", 2)[-1]).write_text(
                    json.dumps(sanitize(json.loads(f.read_text()), tmp), ensure_ascii=False, indent=1))
            else:
                (dest / f.name.split("_", 2)[-1]).write_text(scrub(f.read_text(), tmp))
        (dest / "README.md").write_text(
            f"Peticiones reales de Claude Code {version} a través del gateway (upstream `fake`).\n"
            f"Grabadas con `uv run python -m bench.claude_code_probe --save-fixtures"
            f"{' --tool-search' if a.tool_search else ''}`.\n"
            f"`metadata` (device_id, session_id) y las rutas locales están redactados.\n"
            f"Archivos `NNNN_request.json` = lo que se reenvió al upstream (tras el filtrado);"
            f" `NNNN_response.sse` = la respuesta.\n")
        print(f"fixtures en {dest}")
    srv.stop()
    return result


if __name__ == "__main__":
    main()
