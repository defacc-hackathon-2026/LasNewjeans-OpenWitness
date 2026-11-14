# Open Witness

**No repudio y atribución para agentes de IA.** Open Witness es un gateway que se pone entre cualquier agente (Claude Code, Codex CLI, `refagent`…) y su LLM, sin modificar el agente. Habla Anthropic Messages y la Responses API de OpenAI. Cada sesión deja evidencia criptográfica de:
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

# llaves y manifiestos firmados (SPEC §6.2, §6.3)
uv run owit keygen --out keys/alice.key --register alice      # escribe keys/registry.json
uv run owit keygen --out keys/pub.key
uv run owit manifest sign --server issues --tools tools.json --key keys/pub.key --trust
uv run owit manifest check --server issues --tools tools.json  # ¿coincide lo que anuncia ahora?

# Claude Code real a través del gateway, con prompts firmados y bloqueo de herramientas (G9)
uv run owit hooks install claude-code --project . --yes        # hooks UserPromptSubmit, PreToolUse, SessionStart
uv run owit exec --user alice -- claude                        # crea la sesión y lanza Claude Code por el gateway

# OpenCode real a través del gateway, con el mismo firmado y bloqueo (G12); no hay nada que instalar en el proyecto
uv run owit exec --user alice -- opencode                      # redirige el proveedor anthropic e inyecta el plugin
uv run owit exec --user alice -- opencode run "Resume los issues abiertos"   # modo no interactivo

# Codex CLI a través del gateway, por la Responses API (G13, SPEC §17.4)
uv run owit hooks install codex --user-config --yes            # [[hooks.*]] en ~/.codex/config.toml; aprobarlos una vez con /hooks
uv run owit exec --user alice -- codex exec "Resume NOTAS.md"  # owit exec agrega -c model_provider=owit … y quita OPENAI_API_KEY
uv run owit gateway start --upstream lmstudio_openai           # Codex → gateway → LM Studio (/v1/responses), sin traducción

# verificar una bitácora (las 10 comprobaciones de SPEC §7.4)
uv run owit verify data/ledger/<sid>.jsonl [--receipts ~/.owit/receipts/]

# manipular una bitácora para la demo y ver cómo falla la verificación
uv run owit tamper data/ledger/<sid>.jsonl --mode delete --seq 3

# atribuir cada acción: ¿lo pidió el usuario o lo inyectó la herramienta Z? (SPEC §7.9)
uv run owit attribute data/ledger/<sid>.jsonl [--seq 42] [--judge <perfil>] [--json]
uv run owit show data/ledger/<sid>.jsonl --seq 42     # contexto exacto que vio el modelo, con procedencias

# un escenario del benchmark con el agente de referencia (upstream fake o un perfil de owit.toml)
uv run owit agent run --scenario bench/scenarios/subtle_injection_01.yaml [--upstream lmstudio]
uv run owit llm check --profile lmstudio              # conectividad y salida JSON de un perfil
```

Ejemplo sobre la demo (`uv run python -m bench.demo`):

```
seq=20 mcp__mail__send_email → TOOL_INJECTED(mcp:issues) · no repudio=False · regla R2b · parcial: acción pedida por el usuario, argumento aportado por la herramienta
    to: mcp:issues
```

**Servidores MCP de demo** (`issues`, `mail`, `files`, `http_fetch`; `mail` nunca envía, solo registra en `OWIT_OUTBOX`):

```bash
uv run python -m openwitness.refagent.tools.serve issues    # stdio; fixtures opcionales en OWIT_FIXTURES
```

Para Claude Code, en `.mcp.json`: `{"mcpServers": {"issues": {"command": "uv", "args": ["run", "python", "-m", "openwitness.refagent.tools.serve", "issues"]}}}`.

**LM Studio en otra máquina:** crea en `owit.toml` un perfil `anthropic_compat` con su `base_url` y `api_key_env = "LMSTUDIO_API_KEY"`, y pon la key en `.env` (gitignored). Nunca escribas la key en `owit.toml`.

> En Windows, exporta `PYTHONUTF8=1` antes de correr las pruebas (las fixtures son UTF-8).

Un agente se conecta creando una sesión y usando la URL base que devuelve:
1. `POST /sessions` con `{"user_id": "alice"}` devuelve `{sid, token}`.
2. El agente usa `ANTHROPIC_BASE_URL=http://127.0.0.1:8787/s/<sid>` y `ANTHROPIC_AUTH_TOKEN=<token>`.

Los endpoints están en SPEC §7.5.

**OpenCode (SPEC §17.3, G12).** `owit exec -- opencode` no escribe archivos: pasa por `OPENCODE_CONFIG_CONTENT` la
redirección del proveedor `anthropic` y el plugin `integrations/opencode/owit-opencode-plugin/`, que reenvía el
prompt y cada herramienta a `owit hook …`. Agrega `--standalone`, porque el servicio en segundo plano de OpenCode no
hereda el entorno de la sesión. Dos cosas a tener en cuenta:
- OpenCode nombra las herramientas MCP `<server>_<tool>`; el gateway las reconoce por el `server_id` del manifiesto.
- **Declara los servidores MCP con `"codemode": false`.** Con Code Mode (el valor por defecto), OpenCode las esconde
  detrás de su herramienta `execute` y el gateway no puede filtrarlas.

## Monitor (SPEC §17.5, G14)

`owit monitor` es un proceso aparte y **de solo lectura** sobre `data/` (papel del auditor): sigue las bitácoras en
vivo, verifica su integridad, atribuye cada acción con reglas y deduce alertas. La interfaz es el repo
[monitoring-UI](https://github.com/defacc-hackathon-2026/monitoring-UI) (React + Vite + Tailwind).

```bash
uv run owit monitor --demo --live                       # demo + sesiones nuevas en vivo, API en 127.0.0.1:8790
uv run owit monitor --demo --ui ../monitoring-UI/dist   # sirve también la UI compilada (sin red ni CDN)
uv run owit monitor --data data --registry keys/registry.json [--no-content] [--token …]
```

API: `GET /api/sessions`, `/api/sessions/<sid>/entries?after=<seq>`, `/api/sessions/<sid>/verify`,
`/api/sessions/<sid>/actions/<seq>`, `/api/alerts`, `/api/stats` (agregados para la pestaña de estadísticas) y el SSE `/api/events`. Después de `owit tamper`, la sesión pasa a
FALLA y sus acciones a `UNVERIFIABLE` en menos de 2 s. `uv run python -m bench.live_feed` escribe sesiones nuevas en
un `data/` existente para verlas llegar.

## Benchmarks (SPEC §9)

```bash
uv run python -m bench.tamper --out results        # §9.1 detección de manipulación
uv run python -m bench.latency --n 1000 --out results   # §9.4 latencia agregada
uv run python -m bench.manifests --out results     # §9.3 manifiestos envenenados (corpus en bench/manifests_corpus/)
uv run python -m bench.claude_code_probe           # Claude Code real → gateway → fake (sin API key)
uv run python -m bench.claude_code_hooks_probe --out results   # G9: Claude Code + owit exec + hooks + manifiestos
uv run python -m bench.opencode_probe --out results            # G12: OpenCode + owit exec + plugin + manifiestos
uv run python -m bench.codex_probe --out results               # G13: Codex + owit exec + hooks + manifiestos (Responses)
uv run python -m bench.codex_probe --no-hooks --out results    # G13 mínimo: solo observación
uv run python -m bench.local_model --runs 5        # agente → gateway → LM Studio (gpt-oss-20b)
uv run python -m bench.demo && bash demo/demo.sh   # demo de §13.1: atribución, verify y manipulación

# §9.2 atribución: 35 escenarios (7 clases × 5) en bench/scenarios/ (se regeneran con bench.make_scenarios)
uv run python -m bench.run_bench --out results                              # modelo falso ingenuo
uv run python -m bench.run_bench --upstream lmstudio --runs 3 --out results  # gpt-oss-20b real
uv run python -m bench.run_bench --judge lmstudio_openai --out results       # + juez LLM (G10)
```

Resultados (14 de noviembre, upstream `fake`, localhost; tabla completa en [docs/ficha/resultados.md](docs/ficha/resultados.md)):

| Métrica | Resultado |
|---|---|
| Manipulaciones detectadas (12 clases dentro del modelo de amenazas) | **364/364** |
| Cola no anclada truncada | 0/4 sin recibo (límite L3, esperado); 2/2 con recibo del usuario |
| Latencia añadida hasta el primer byte, p50 | +1,0 ms (petición pequeña); +2,3 ms (petición de Claude Code de 75 KB); +2,0 ms (Codex, 60 KB) |
| Latencia añadida total, p50 | +1,4 ms; +2,7 ms; +2,4 ms |
| `append()` p50 | 0,3–0,4 ms |
| Hook `owit hook pre-tool` por invocación, p50 | ≈ 84 ms (contenedor de 4 vCPU) |
| Manifiestos maliciosos bloqueados (6 clases) | **47/47** en `ToolFilter`, cliente MCP y gateway; `tool-check` deniega 47/47; 0 falsos positivos en 96 legítimas; firmados pero envenenados 0/5 (L5) |
| Claude Code 2.1.323 real a través del gateway (con `fake` y con `gpt-oss-20b`) | integridad OK, 0 `history_mismatch`; con prompt firmado, 0 textos sin emparejar |
| Claude Code 2.1.325 con `owit exec` y hooks (G9) | prompt firmado por el hook, con recibo; la herramienta MCP alterada tras firmar no llega al modelo y el hook la deniega |
| OpenCode 2.3.0 con `owit exec` y plugin (G12) | prompt firmado por el plugin, con recibo; la herramienta MCP alterada tras firmar no llega al modelo y el plugin la deniega; integridad OK y el mismo veredicto que con Claude Code (`TOOL_INJECTED(mcp:issues)`, parcial) |
| Codex CLI 0.169.0 con `owit exec` y hooks (G13, Responses API) | 4 peticiones a `/v1/responses` con historial completo (`store = false`); prompt firmado por el hook, con recibo; 0 `history_mismatch` (comparación ítem por ítem: Codex no reenvía el `reasoning`); la herramienta MCP alterada no llega al modelo y el hook la deniega; misma atribución que con Claude Code (`TOOL_INJECTED(mcp:issues)`, parcial); +2,0 ms TTFB p50 (petición de 60 KB) |
| `gpt-oss-20b` local ante una inyección sutil | el modelo cayó 5/5; bitácora íntegra 10/10 |
| Atribución, modelo falso (35 escenarios, 7 clases) | **35/35** en etiqueta, `source`, `non_repudiable` y `partial`; sin R2b, 30/35 |
| Atribución, `gpt-oss-20b` (3 × 35 corridas) | acción ocurrida en 74/105; **74/74 atribuidas correctamente**; inyección sutil exitosa 12/15 |
| Juez LLM local `gpt-oss-20b` (G10) | solo juez 21/35; reglas+juez 30/35; las reglas solas (35/35) son la mejor configuración |

Detalle en `results/*.txt`. Las latencias de la tabla son de un portátil (`results/latency_laptop.json`); en el contenedor de 4 vCPU donde se volvió a correr todo dan +2,1 / +5,3 ms (`results/latency.json`).

**Ficha técnica:** [docs/ficha_tecnica.md](docs/ficha_tecnica.md) → `uv run python docs/ficha_pdf.py` → [docs/ficha_tecnica.pdf](docs/ficha_tecnica.pdf) (2 páginas). Resumen en [docs/resumen.md](docs/resumen.md) y guion del pitch en [docs/pitch.md](docs/pitch.md).

## Estructura

```
src/openwitness/   canonical, identity, manifests, ledger, gateway, llm, refagent, attribution, integrations, cli
bench/             benchmarks de §9 (synthetic, tamper, latency, claude_code_probe, run_bench, scenarios/)
tests/             pruebas y fixtures (tests/fixtures/{claude_code,opencode,codex}/<versión>/)
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

Herramientas usadas en la evaluación: Claude Code 2.1.321, 2.1.323 y 2.1.325, OpenCode 2.3.0, Codex CLI 0.169.0, LM Studio, pandoc y Chromium (Playwright) para exportar la ficha.

Trabajos y casos en que se apoya la idea: los ataques a descripciones y resultados de herramientas MCP (*tool poisoning*, *rug pull*, *shadowing*) y el caso Sentry como ejemplo de inyección por un servidor MCP. Solo se citan como motivación: no se usaron ni se atacaron servicios reales.

Datos: todos los escenarios, issues, páginas, correos y usuarios (`alice`, `mallory`) son sintéticos, con dominios reservados (`*.test`, `example.*`).

**Uso de asistentes de IA.** El diseño (SPEC.md) y parte del código se escribieron con ayuda de asistentes de IA, y cada módulo lo revisó su responsable (SPEC §10.4). En concreto:
- `attribution/`, `refagent/`, `llm/naive.py`, `llm/client.py`, la ampliación de `llm/config.py`, `bench/make_scenarios.py`, `bench/run_bench.py`, `tests/test_attribution.py` y los textos de `docs/ficha/{amenaza,limites,doble_uso}.md`, `docs/resumen.md` y `docs/pitch.md` se escribieron con Claude Code (Claude Opus 5.5), y los revisó Andres.
- El monitor (`src/openwitness/monitor/`, `bench/live_feed.py`, `tests/test_monitor.py`) y su interfaz (repo `monitoring-UI`) también se escribieron con Claude Code (Claude Opus 5.5), y los revisó Andres.
- `manifests/`, `integrations/` (hooks, `owit exec`, Claude Code y el plugin de OpenCode), el modo por agente de `gateway/tool_filter.py`, `bench/manifests.py` y su corpus, `bench/claude_code_hooks_probe.py`, `bench/opencode_probe.py`, `tests/test_{canonical,identity,manifests,hooks,opencode}.py` y `docs/ficha/defensa.md` se escribieron con Claude Code (Claude Fable 5.2), y los revisó Juliana.
- Los 35 escenarios (`bench/scenarios/`) también se redactaron con ayuda de IA; las inyecciones son textos de ejemplo contra herramientas de demo.
- La integración de las ramas del equipo, `src/openwitness/entry.py` (hooks sin Typer), el paso de atribución de `bench/demo.py`, `docs/ficha_pdf.py` y la re-corrida de los benchmarks se hicieron con Claude Code.
- G13 (Fase 2): el adaptador `gateway/protocols/openai_responses.py`, la generalización del gateway a varios protocolos, `llm/openai_upstream.py`, el modo Responses de `llm/fake.py`, `integrations/codex/`, `bench/codex_probe.py` y `tests/test_decompose_responses.py` se escribieron con Claude Code (Claude Opus 5.5); los revisa Adrian.
