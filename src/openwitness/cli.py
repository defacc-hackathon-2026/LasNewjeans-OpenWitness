"""Comando `owit` (SPEC §7.10). Cada integrante agrega sus subcomandos en su propio grupo.

Grupos: gateway (Adrian), verify/tamper/bench tamper|latency (Adrian), keygen/manifest/hook/exec (Juliana),
attribute/show/agent/bench attribution (Andres), llm (Adrian/Andres).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Open Witness: no repudio y atribución para agentes")
gateway_app = typer.Typer(no_args_is_help=True, help="Gateway entre el agente y su LLM")
bench_app = typer.Typer(no_args_is_help=True, help="Benchmarks de §9")
llm_app = typer.Typer(no_args_is_help=True, help="Perfiles de LLM (§7.7)")
app.add_typer(gateway_app, name="gateway")
app.add_typer(bench_app, name="bench")
app.add_typer(llm_app, name="llm")
manifest_app = typer.Typer(no_args_is_help=True, help="Manifiestos MCP firmados (§6.3)")
app.add_typer(manifest_app, name="manifest")

DATA = Path(os.environ.get("OWIT_DATA", "data"))
REPO = Path(__file__).resolve().parents[2]
if (REPO / "bench").is_dir() and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))   # bench/ vive en el repo, no en el paquete instalado


def _gateway_key(data: Path):
    from .identity import KeyPair
    p = data / "gateway.key"
    if p.exists():
        return KeyPair.load(p)
    kp = KeyPair.generate()
    kp.save(p)
    return kp


def _registry(path: Path):
    from .identity import KeyRegistry
    return KeyRegistry.load(path)


def _gate(manifests: Path | None):
    """El ManifestGate de Juliana, si ya está en el repo; si no, None (herramientas MCP como `unsigned`)."""
    try:
        from .manifests import ManifestGate, TrustStore  # type: ignore[attr-defined]
    except ImportError:
        return None
    ts = TrustStore()
    if manifests and manifests.exists():
        ts.load_dir(manifests)
    return ManifestGate(ts)


def build_gateway(config: Path, upstream: str | None, data: Path, registry: Path, manifests: Path | None,
                  record: Path | None = None, anchor_every: int | None = None):
    from .gateway.server import Gateway
    from .llm.config import load_config, load_profiles, make_upstream, resolve
    cfg = load_config(config)
    gcfg = cfg.get("gateway", {})
    profiles = load_profiles(config)
    default = resolve(upstream, "OWIT_UPSTREAM", gcfg.get("upstream"), "fake")
    ups = {}
    for name, prof in profiles.items():
        try:
            ups[name] = make_upstream(prof)
        except NotImplementedError:
            continue
    if default not in ups:
        raise typer.BadParameter(f"upstream {default!r} no existe o no habla Anthropic Messages")
    return Gateway(data, _gateway_key(data), _registry(registry), upstreams=ups, default_upstream=default,
                   gate=_gate(manifests), anchor_every=anchor_every or int(gcfg.get("anchor_every", 20)),
                   anchor_interval_s=float(gcfg.get("anchor_interval_s", 30)),
                   default_policy=cfg.get("policy"), record_dir=record)


@gateway_app.command("start")
def gateway_start(config: Path = typer.Option(Path("owit.toml"), help="owit.toml"),
                  upstream: Optional[str] = typer.Option(None, help="perfil del upstream por defecto"),
                  listen: Optional[str] = typer.Option(None, help="host:puerto (por defecto el de owit.toml)"),
                  data: Path = typer.Option(DATA, help="directorio de datos (bitácoras, blobs, ancla)"),
                  registry: Path = typer.Option(Path("keys/registry.json"), help="registro de llaves de usuario"),
                  manifests: Optional[Path] = typer.Option(Path("manifests"), help="manifiestos de confianza"),
                  record: Optional[Path] = typer.Option(None, help="guardar peticiones y respuestas (fixtures)")):
    """Arranca el gateway."""
    import uvicorn
    from .llm.config import load_config
    gw = build_gateway(config, upstream, data, registry, manifests, record)
    host, _, port = (listen or load_config(config).get("gateway", {}).get("listen", "127.0.0.1:8787")).rpartition(":")
    typer.echo(f"Open Witness gateway en http://{host}:{port} · upstream={gw.default_upstream} · "
               f"llave {gw.kp.public_str()[:24]}…", err=True)
    uvicorn.run(gw.app(), host=host or "127.0.0.1", port=int(port), log_level="warning")


CHECK_NAMES = {1: "hash", 2: "firma del gateway", 3: "cadena y secuencia", 4: "referencias de contexto",
               5: "prompts firmados", 6: "herramientas aceptadas", 7: "ancla externa", 8: "recibos del usuario",
               9: "blobs", 10: "coherencia del contexto"}


@app.command("verify")
def verify_cmd(ledger: Path, data: Path = typer.Option(DATA),
               registry: Path = typer.Option(Path("keys/registry.json")),
               receipts: Optional[Path] = typer.Option(None, help="archivo o directorio de recibos"),
               gateway_pubkey: Optional[str] = typer.Option(None, help="por defecto, la de data/gateway.key"),
               as_json: bool = typer.Option(False, "--json"),
               verbose: bool = typer.Option(False, "--verbose", "-v", help="listar cada error")):
    """Verifica la integridad de una bitácora (las 10 comprobaciones de §7.4)."""
    from .ledger import BlobStore, MockAnchor, verify_ledger
    pub = gateway_pubkey or _gateway_key(data).public_str()
    rcs = []
    if receipts:
        files = sorted(receipts.glob("*.json")) if receipts.is_dir() else [receipts]
        for f in files:
            d = json.loads(f.read_text())
            rcs += d if isinstance(d, list) else [d]
    rep = verify_ledger(ledger, BlobStore(data / "blobs"), pub, _registry(registry),
                        MockAnchor(data / "anchor" / "mock_anchor.jsonl"), rcs)
    if as_json:
        typer.echo(json.dumps(rep.to_dict(), indent=1, ensure_ascii=False))
    else:
        typer.echo(f"{'OK' if rep.ok else 'FALLA'} · {rep.entries} entradas · última anclada {rep.last_anchored_seq}"
                   f" · cola sin anclar {rep.unanchored_tail} · recibos {len(rcs)}")
        missing = sum(1 for v in rep.blob_status.values() if v == "missing")
        if missing:
            typer.echo(f"  blobs ausentes (borrado por privacidad): {missing}")
        if rep.errors:
            by_check: dict[int, list] = {}
            for e in rep.errors:
                by_check.setdefault(e.check, []).append(e)
            first = rep.first_error_seq()
            typer.echo(f"  primera entrada afectada: seq={first} · {len(rep.errors)} errores")
            for chk in sorted(by_check):
                es = by_check[chk]
                kinds = sorted({e.error for e in es})
                typer.echo(f"  [{chk}] {CHECK_NAMES.get(chk, '')}: {', '.join(kinds)} ({len(es)})")
            if verbose:
                for e in rep.errors:
                    typer.echo(f"    [{e.check}] seq={e.seq} {e.error} {e.detail}")
    raise typer.Exit(0 if rep.ok else 1)


@app.command("tamper")
def tamper_cmd(ledger: Path, mode: str = typer.Option(..., help="una de las mutaciones de bench/tamper.py"),
               seq: int = typer.Option(1), data: Path = typer.Option(DATA),
               out: Optional[Path] = typer.Option(None, help="por defecto reescribe el archivo")):
    """Aplica una manipulación a una bitácora (para la demo de §13.1)."""
    import random
    from bench.tamper import MUTATIONS, OverlayBlobs, Trial
    from .ledger import BlobStore, load_records
    from .ledger.ledger import write_records
    if mode not in MUTATIONS:
        raise typer.BadParameter(f"modos: {', '.join(MUTATIONS)}")
    blobs = BlobStore(data / "blobs")
    t = Trial(load_records(ledger), OverlayBlobs(blobs), [])
    if not MUTATIONS[mode][0](t, seq, _gateway_key(data), random.Random(0)):
        raise typer.BadParameter(f"{mode} no aplica en seq={seq}")
    for h, b in t.blobs.over.items():
        if b is not None:
            (blobs.root / h.removeprefix("sha256:")).write_bytes(b)
    write_records(out or ledger, t.records)
    typer.echo(f"{MUTATIONS[mode][1]} en seq={seq} → {out or ledger}")


@bench_app.command("tamper")
def bench_tamper(out: Path = typer.Option(Path("results")), sessions: int = 5, positions: int = 8):
    """§9.1: detección de manipulación."""
    from bench.tamper import main
    main(["--out", str(out), "--sessions", str(sessions), "--positions", str(positions)])


@bench_app.command("latency")
def bench_latency(out: Path = typer.Option(Path("results")), n: int = 1000):
    """§9.4: latencia agregada por el gateway."""
    from bench.latency import main
    main(["--out", str(out), "--n", str(n)])


@bench_app.command("manifests")
def bench_manifests(out: Path = typer.Option(Path("results")),
                    regen: bool = typer.Option(False, "--regen", help="regenerar bench/manifests_corpus/")):
    """§9.3: manifiestos envenenados bloqueados."""
    from bench.manifests import main
    main(["--out", str(out)] + (["--regen"] if regen else []))


@llm_app.command("list")
def llm_list(config: Path = typer.Option(Path("owit.toml"))):
    from .llm.config import load_profiles, redact_headers
    for name, p in load_profiles(config).items():
        typer.echo(f"{name:<16} {p.provider:<17} {p.model:<28} {p.base_url or ''} "
                   f"{json.dumps(redact_headers(p.headers)) if p.headers else ''}")


@app.command("fake")
def fake_serve(port: int = 8788, script: Optional[Path] = None):
    """Sirve el modelo falso por HTTP (protocolo Anthropic)."""
    import uvicorn
    from .llm.fake import ScriptedResponder, fake_app
    uvicorn.run(fake_app(ScriptedResponder.load(script) if script else None), host="127.0.0.1", port=port,
                log_level="warning")


# ---------------------------------------------------------------- llaves y manifiestos (Juliana)

@app.command("keygen")
def keygen(out: Path = typer.Option(..., help="archivo de la llave privada, p. ej. keys/alice.key"),
           register: Optional[str] = typer.Option(None, help="user_id con el que se registra la llave pública"),
           registry: Path = typer.Option(Path("keys/registry.json"), help="registro de llaves de usuario"),
           force: bool = typer.Option(False, "--force", help="sobrescribir una llave que ya existe")):
    """Genera una llave Ed25519 (permisos 600) y, con --register, la anota en el registro."""
    from .identity import KeyPair, KeyRegistry
    if out.exists() and not force:
        raise typer.BadParameter(f"{out} ya existe; usa --force para reemplazarla")
    kp = KeyPair.generate()
    kp.save(out)
    if register:
        reg = KeyRegistry.load(registry)
        reg.register(register, kp.public_str())
        registry.parent.mkdir(parents=True, exist_ok=True)
        reg.save(registry)
        typer.echo(f"registrada como {register} en {registry}", err=True)
    typer.echo(kp.public_str())


def _load_tools(path: Path):
    """tools.json: una lista de `{name, description, input_schema}` o `{"tools": [...]}` (el `tools/list` de MCP)."""
    from .types import ToolDef
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("tools") if isinstance(data, dict) else data
    if not isinstance(items, list) or not all(isinstance(t, dict) and t.get("name") for t in items):
        raise typer.BadParameter(f"{path}: se esperaba una lista de herramientas con `name`")
    return [ToolDef(t["name"], t.get("description") or "", t.get("input_schema") or t.get("inputSchema") or {})
            for t in items]


@manifest_app.command("sign")
def manifest_sign(server: str = typer.Option(..., help="server_id, tal como lo nombra el agente (mcp__<server>__…)"),
                  tools: Path = typer.Option(..., help="tools.json con las herramientas del servidor"),
                  key: Path = typer.Option(..., help="llave privada del publicador"),
                  out: Path = typer.Option(Path("manifests"), help="directorio de manifiestos de confianza"),
                  publisher: Optional[str] = typer.Option(None, help="por defecto, el server_id"),
                  version: str = typer.Option("1.0.0", help="server_version"),
                  trust: bool = typer.Option(False, "--trust", help="agregar la llave a trust.json para ese publicador")):
    """Firma el manifiesto de un servidor MCP y lo escribe en <out>/<server>.json."""
    from .identity import KeyPair
    from .manifests import TrustStore, manifest_hash, sign_manifest
    kp = KeyPair.load(key)
    pub = publisher or server
    m = sign_manifest(kp, server, version, pub, _load_tools(tools))
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{server}.json"
    dest.write_text(json.dumps(m, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    typer.echo(f"{dest} · {len(m['body']['tools'])} herramientas · {manifest_hash(m)}", err=True)
    if trust:
        ts = TrustStore()
        ts.load_dir(out)            # conserva publicadores y pins que ya había
        ts.trust(pub, kp.public_str())
        ts.save_trust(out)
        typer.echo(f"{pub} confía en {kp.public_str()} ({out / 'trust.json'})", err=True)
    typer.echo(manifest_hash(m))


@manifest_app.command("check")
def manifest_check(tools: Path = typer.Option(..., help="tools.json con lo que el servidor anuncia ahora"),
                   server: str = typer.Option(..., help="server_id"),
                   manifests: Path = typer.Option(Path("manifests"), help="directorio de manifiestos de confianza")):
    """Evalúa las herramientas en vivo de un servidor contra el TrustStore, como lo haría el gateway."""
    from .manifests import ManifestGate, TrustStore
    ts = TrustStore()
    ts.load_dir(manifests)
    gate = ManifestGate(ts)
    decisions = [gate.evaluate_tool(server, t, {}) for t in _load_tools(tools)]
    for d in decisions:
        typer.echo(f"{'ACEPTADA ' if d.accepted else 'RECHAZADA'} {d.origin}/{d.tool.name}: {d.reason}")
    raise typer.Exit(0 if all(d.accepted for d in decisions) else 1)


# ---------------------------------------------------------------- hooks y exec (Juliana)

hook_app = typer.Typer(no_args_is_help=True, help="Hooks del agente: los invoca Claude Code, no el usuario (§7.6)")
hooks_app = typer.Typer(no_args_is_help=True, help="Instalación de los hooks en un agente")
app.add_typer(hook_app, name="hook")
app.add_typer(hooks_app, name="hooks")


def _run_hook(kind: str) -> None:
    from .integrations.hooks import main as hook_main
    raise typer.Exit(hook_main(kind))


@hook_app.command("prompt")
def hook_prompt_cmd():
    """UserPromptSubmit: firma el prompt, lo envía al gateway y guarda el recibo."""
    _run_hook("prompt")


@hook_app.command("pre-tool")
def hook_pre_tool_cmd():
    """PreToolUse: consulta tool-check y deniega la herramienta si el gateway la rechazó."""
    _run_hook("pre-tool")


@hook_app.command("session-start")
def hook_session_start_cmd():
    """SessionStart: asocia la sesión del agente con la del gateway."""
    _run_hook("session-start")


@hooks_app.command("install")
def hooks_install(agent: str = typer.Argument(..., help="claude-code (opencode y codex: roadmap)"),
                  project: Path = typer.Option(Path("."), help="proyecto donde se instalan los hooks"),
                  command: Optional[str] = typer.Option(None, help="comando owit que usarán los hooks"),
                  yes: bool = typer.Option(False, "--yes", "-y", help="escribir sin preguntar"),
                  dry_run: bool = typer.Option(False, "--dry-run", help="solo mostrar el diff")):
    """Escribe los hooks en la configuración local del proyecto. Antes muestra el diff."""
    if agent != "claude-code":
        raise typer.BadParameter("por ahora solo claude-code; opencode y codex están en el roadmap")
    import shutil
    from .integrations import claude_code
    owit = command or shutil.which("owit") or "owit"
    try:
        path, old, new = claude_code.plan_install(project, owit)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    if old == new:
        typer.echo(f"{path}: los hooks ya están instalados")
        return
    typer.echo(claude_code.diff(path, old, new))
    if dry_run:
        return
    if not yes and not typer.confirm(f"¿Escribir {path}?"):
        raise typer.Exit(1)
    claude_code.write(path, new)
    typer.echo(f"hooks instalados en {path}; lanza el agente con `owit exec --user <usuario> -- claude`")


@app.command("exec", context_settings={"allow_interspersed_args": False})
def exec_cmd(cmd: list[str] = typer.Argument(..., help="comando del agente, después de `--`"),
             user: str = typer.Option(..., help="user_id registrado en el gateway"),
             key: Optional[Path] = typer.Option(None, help="llave privada del usuario (por defecto keys/<user>.key)"),
             gateway: Optional[str] = typer.Option(None, help="URL del gateway (por defecto $OWIT_GATEWAY o localhost:8787)"),
             upstream: Optional[str] = typer.Option(None, help="perfil del upstream para esta sesión"),
             agent: Optional[str] = typer.Option(None, help="claude-code; si se omite, se deduce del comando"),
             strict: bool = typer.Option(False, "--strict", help="rechazar los turnos sin prompt firmado"),
             prompt: Optional[str] = typer.Option(None, help="firmar este prompt y pasarlo como argumento (agentes sin hooks)")):
    """Crea una sesión en el gateway y lanza el agente a través de ella: `owit exec --user alice -- claude`."""
    from .integrations.exec import DEFAULT_GATEWAY, ExecError, run_exec
    try:
        code = run_exec(cmd, user, key or Path("keys") / f"{user}.key",
                        gateway or os.environ.get("OWIT_GATEWAY") or DEFAULT_GATEWAY,
                        agent=agent, upstream=upstream, strict=strict, prompt=prompt)
    except ExecError as e:
        typer.echo(f"owit exec: {e}", err=True)
        raise typer.Exit(2)
    raise typer.Exit(code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
