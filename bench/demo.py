"""Prepara la demo de SPEC §13.1 (parte de integridad) en un directorio `demo/` autocontenido.

1. Genera llaves de alice y mallory y el registro `demo/keys/registry.json`.
2. Pasa dos sesiones por el gateway (upstream `fake`): la inyección sutil pedida por alice, y el mismo daño
   pedido y firmado por mallory. Guarda los recibos de cada usuario en `demo/receipts/<usuario>/`.
3. Escribe `demo/demo.sh` con los comandos que se graban: `owit attribute` sobre las dos sesiones (alice →
   `TOOL_INJECTED(mcp:issues)` parcial; mallory → `USER_INSTRUCTED` con no repudio) → verify OK → el operador
   borra el prompt de mallory → verify FALLA (cadena rota, ancla y recibo de mallory) → la atribución queda
   `UNVERIFIABLE`.

    uv run python -m bench.demo && bash demo/demo.sh
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx

from openwitness.gateway.server import Gateway
from openwitness.identity import KeyPair, KeyRegistry
from openwitness.ledger import MockAnchor, load_records
from openwitness.llm.base import LLMProfile
from openwitness.llm.fake import FakeUpstream, ScriptedResponder

from .synthetic import SCRIPT, Env, run_conversation

ROOT = Path(__file__).resolve().parents[1]


def build(demo: Path) -> dict:
    if demo.exists():
        shutil.rmtree(demo)
    data, keys = demo / "data", demo / "keys"
    users = {u: KeyPair.generate() for u in ("alice", "mallory")}
    reg = KeyRegistry()
    for u, k in users.items():
        k.save(keys / f"{u}.key")
        reg.register(u, k.public_str())
    reg.save(keys / "registry.json")
    gk = KeyPair.generate()
    gk.save(data / "gateway.key")
    anchor = MockAnchor(data / "anchor" / "mock_anchor.jsonl")
    fake = FakeUpstream(LLMProfile("fake", "fake", model="fake-model"), responder=ScriptedResponder(SCRIPT))
    gw = Gateway(data, gk, reg, upstreams={"fake": fake}, default_upstream="fake", anchor=anchor, anchor_every=5)
    env = Env(data, gw, reg, users, anchor,
              httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app()), base_url="http://gw"))
    alice = asyncio.run(run_conversation(env, user="alice", prompt="Resume los issues abiertos del proyecto demo y "
                                         "manda el resumen a quien corresponda"))
    mallory = asyncio.run(run_conversation(env, user="mallory", prompt="Manda el resumen de los issues a "
                                           "reportes@evil.test"))
    out = {}
    for name, s in (("alice", alice), ("mallory", mallory)):
        rdir = demo / "receipts" / name
        rdir.mkdir(parents=True, exist_ok=True)
        for rc in s["receipts"]:
            (rdir / f"{s['sid']}_{rc['body']['seq']}.json").write_text(json.dumps(rc, indent=1))
        recs = load_records(s["ledger"])
        prompt_seq = next(r.seq for r in recs if r.kind == "user_prompt")
        send_seq = next(r.seq for r in recs if r.kind == "tool_call" and r.meta.get("tool", "").endswith("send_email"))
        out[name] = {"sid": s["sid"], "ledger": str(Path(s["ledger"]).relative_to(ROOT)), "prompt_seq": prompt_seq,
                     "send_seq": send_seq}
    return out


SCRIPT_SH = """#!/usr/bin/env bash
# Demo de integridad (SPEC §13.1). Generado por `uv run python -m bench.demo`.
set -u
cd "$(dirname "$0")/.."
O="--data demo/data --registry demo/keys/registry.json"
L={mallory_ledger}
A={alice_ledger}
# idempotente: cada corrida parte de la bitácora original
if [ -f "$L.orig" ]; then cp "$L.orig" "$L"; else cp "$L" "$L.orig"; fi
# PAUSE: antes de cada comando; READ: después de su salida, para leerla en la grabación
step() {{ printf '\\n\\033[1m$ %s\\033[0m\\n' "$*"; sleep "${{PAUSE:-1}}"; "$@"; local rc=$?; sleep "${{READ:-0}}"; return $rc; }}

echo "# 1. alice pidió un resumen de los issues; un issue le dijo al agente a quién mandarlo"
step uv run owit attribute "$A" --seq {alice_send} $O

echo; echo "# 2. mallory pidió el mismo daño, con su prompt firmado"
step uv run owit attribute "$L" --seq {mallory_send} $O

echo; echo "# 3. La bitácora de mallory está íntegra: firmas, cadena, ancla y su recibo"
step uv run owit verify "$L" $O --receipts demo/receipts/mallory

echo; echo "# 4. El operador borra el prompt firmado de mallory para encubrirla"
step uv run owit tamper "$L" --mode delete --seq {mallory_seq} --data demo/data

echo; echo "# 5. La verificación lo detecta: cadena rota, cabeza anclada ausente y recibo de mallory contradicho"
step uv run owit verify "$L" $O --receipts demo/receipts/mallory

echo; echo "# 6. Y la atribución se niega a culpar a nadie sobre evidencia alterada"
step uv run owit attribute "$L" $O

if [ "${{FULL:-0}}" = "1" ]; then
  cp "$L.orig" "$L"
  echo; echo "# 7. Ni con la llave del gateway: reescribe el prompt de mallory y vuelve a firmar toda la cadena"
  step uv run owit tamper "$L" --mode replace_prompt --seq {mallory_seq} --data demo/data
  step uv run owit verify "$L" $O --receipts demo/receipts/mallory
fi
"""


def main() -> None:
    demo = ROOT / "demo"
    info = build(demo)
    sh = SCRIPT_SH.format(mallory_ledger=info["mallory"]["ledger"], mallory_seq=info["mallory"]["prompt_seq"],
                         alice_ledger=info["alice"]["ledger"], alice_send=info["alice"]["send_seq"],
                         mallory_send=info["mallory"]["send_seq"])
    (demo / "demo.sh").write_text(sh)
    (demo / "demo.sh").chmod(0o755)
    (demo / "sessions.json").write_text(json.dumps(info, indent=1))
    print(json.dumps(info, indent=1))
    print("bash demo/demo.sh   # PAUSE=2 para grabar con asciinema")


if __name__ == "__main__":
    main()
