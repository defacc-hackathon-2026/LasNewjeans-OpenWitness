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


@app.command("verify")
def verify_cmd(ledger: Path, data: Path = typer.Option(DATA),
               registry: Path = typer.Option(Path("keys/registry.json")),
               receipts: Optional[Path] = typer.Option(None, help="archivo o directorio de recibos"),
               gateway_pubkey: Optional[str] = typer.Option(None, help="por defecto, la de data/gateway.key"),
               as_json: bool = typer.Option(False, "--json")):
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
        for e in rep.errors:
            typer.echo(f"  [{e.check}] seq={e.seq} {e.error} {e.detail}")
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
