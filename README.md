# Open Witness

**No repudio y atribución para agentes de IA.** Open Witness es un gateway que se pone entre cualquier agente (Claude Code, `refagent`…) y su LLM, sin modificar el agente. Cada sesión deja evidencia criptográfica de:
- qué pidió el usuario (prompts firmados con Ed25519);
- qué herramientas vio el modelo (manifiestos firmados);
- qué había exactamente en el contexto cuando actuó (bitácora encadenada, firmada y anclada).

Con esa evidencia, ante un incidente se responde: *¿lo pidió el usuario o lo inyectó la herramienta Z?*

Defensive Acceleration Hackathon 2026 · Track 1 (seguridad de agentes y prompt injection). Diseño completo en [SPEC.md](SPEC.md); reparto del equipo en [docs/equipo/](docs/equipo/).

## Instalación

Requiere Python 3.14 y [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest -q
cp owit.example.toml owit.toml      # opcional: perfiles de LLM y política
```

## Uso

```bash
# gateway con upstream falso (no gasta tokens) o con LM Studio
uv run owit gateway start --upstream fake
uv run owit gateway start --upstream lmstudio

# verificar una bitácora (las 10 comprobaciones de SPEC §7.4)
uv run owit verify data/ledger/<sid>.jsonl [--receipts ~/.owit/receipts/]

# manipular una bitácora para la demo y ver cómo falla la verificación
uv run owit tamper data/ledger/<sid>.jsonl --mode delete --seq 3
```

Un agente se conecta creando una sesión y usando la URL base que devuelve:
1. `POST /sessions` con `{"user_id": "alice"}` devuelve `{sid, token}`.
2. El agente usa `ANTHROPIC_BASE_URL=http://127.0.0.1:8787/s/<sid>` y `ANTHROPIC_AUTH_TOKEN=<token>`.

Los endpoints están en SPEC §7.5.

## Benchmarks (SPEC §9)

```bash
uv run python -m bench.tamper --out results        # §9.1 detección de manipulación
uv run python -m bench.latency --n 1000 --out results   # §9.4 latencia agregada
uv run python -m bench.claude_code_probe           # Claude Code real → gateway → fake (sin API key)
uv run python -m bench.local_model --runs 5        # agente → gateway → LM Studio (gpt-oss-20b)
uv run python -m bench.demo && bash demo/demo.sh   # demo de integridad (SPEC §13.1)
```

Resultados preliminares (14 de noviembre, upstream `fake`, localhost):

| Métrica | Resultado |
|---|---|
| Manipulaciones detectadas (12 clases dentro del modelo de amenazas) | **364/364** |
| Cola no anclada truncada | 0/4 sin recibo (límite L3, esperado); 2/2 con recibo del usuario |
| Latencia añadida hasta el primer byte, p50 | +1,0 ms (petición pequeña); +2,2 ms (petición de Claude Code de 75 KB) |
| Latencia añadida total, p50 | +1,4 ms; +2,6 ms |
| `append()` p50 | 0,3–0,4 ms |
| Claude Code 2.1.323 real a través del gateway (con `fake` y con `gpt-oss-20b`) | integridad OK, 0 `history_mismatch`; con prompt firmado, 0 textos sin emparejar |
| `gpt-oss-20b` local ante una inyección sutil | el modelo cayó 5/5; bitácora íntegra 10/10 |

Faltan la atribución (§9.2) y los manifiestos (§9.3).

## Estructura

```
src/openwitness/   canonical, identity, manifests, ledger, gateway, llm, refagent, attribution, cli
bench/             benchmarks de §9 (synthetic, tamper, latency, claude_code_probe)
tests/             pruebas y fixtures (tests/fixtures/claude_code/<versión>/)
results/           salidas de los benchmarks
docs/              ficha técnica, resumen, diagramas y hojas del equipo
```

## Uso responsable

Todo corre en local, con servidores MCP de demo, datos sintéticos y dominios `.test`. Los escenarios de inyección son textos de ejemplo contra herramientas de demo y siempre van junto con la defensa (SPEC §10.3).

## Citas y uso de IA

Dependencias:
- PyNaCl (Apache-2.0)
- Starlette (BSD-3)
- uvicorn (BSD-3)
- httpx (BSD-3)
- SDK de Anthropic (MIT)
- SDK de OpenAI (Apache-2.0)
- SDK de MCP (MIT)
- PyYAML (MIT)
- Typer (MIT)

Herramientas usadas en la evaluación: Claude Code 2.1.323 y LM Studio.

El diseño (SPEC.md) y parte del código se escribieron con ayuda de asistentes de IA. Cada módulo lo revisó su responsable (SPEC §10.4).
