# Adrian — Gateway y bitácora

> Hoja de trabajo derivada de [SPEC.md](../../SPEC.md). Si algo no coincide, manda el SPEC.

**Tu parte en una frase:** el gateway que se pone entre el agente y el LLM, y la bitácora encadenada que deja la evidencia. Es el corazón del sistema y la parte más riesgosa: ya la validaste anoche en el prototipo.

## Estado (14 de noviembre)

Las ramas de Juliana (`juliana/manifests`, que incluye `juliana/identity-tests`) y de Andres (`andres/attribution`) están integradas en `claude/great-wright-vtjd8d`, listas para pasar a `main`. `uv run pytest -q`: 184 en verde.

| Módulo | SPEC | Estado |
|---|---|---|
| Repo, `pyproject.toml` (`owit = "openwitness.cli:main"`, `mcp>=2,<3`) | §8 | ✅ |
| `types.py` | §6, §7 | ✅ propuesto; falta el visto bueno de Juliana y Andres |
| `normalize.py` (normalización de §6.1, compartida) | §6.1 | ✅ |
| `ledger/`: `BlobStore`, `Ledger`, `MockAnchor` (`session_id`, `heads_for`), recibos | §6.4, §6.6, §6.7 | ✅ |
| `verify_ledger` con las 10 comprobaciones | §7.4 | ✅ (la 7 también exige la entrada `anchor` de cada cabeza anclada) |
| `gateway/`: servidor, sesiones y política, `Decomposer`, `prompt_match`, adaptador Anthropic con SSE | §6.5, §7.5 | ✅ |
| `llm/`: `anthropic`, `anthropic_compat` (LM Studio) y transporte `fake` | §7.7 | ✅ (LM Studio sin probar todavía con el entregable) |
| CLI: `owit gateway start`, `verify`, `tamper`, `bench tamper`, `bench latency`, `llm list`, `fake` | §7.10 | ✅ |
| Fixture de Claude Code 2.1.323 (con y sin búsqueda diferida) | §5.3, §12.2 | ✅ `tests/fixtures/claude_code/2.1.323/` |
| Pendiente de §5.3: ¿el token de sesión gana sobre el login de suscripción? | §5.3 | ✅ sí, con la configuración real (`bench/claude_code_probe.py --real-config`) |
| Bench §9.1 (12 mutaciones) | §9.1 | ✅ 364/364 dentro del modelo; cola no anclada 0/4 sin recibo (L3) y 2/2 con recibo |
| Bench §9.4 (latencia) | §9.4 | ✅ +1,0 ms (refagent) y +2,2 ms (petición de Claude Code de 75 KB) hasta el primer byte (p50) en el portátil (`results/latency_laptop.json`); en el contenedor de 4 vCPU, +2,1 / +5,3 ms, igual que antes de integrar. Hook real `owit hook pre-tool` ≈ 84 ms (era ≈ 195 ms pasando por Typer: `openwitness.entry` lo evita) |
| Upstream `anthropic_compat` con LM Studio (`gpt-oss-20b`) | §7.7 | ✅ `bench/local_model.py`: 10/10 bitácoras íntegras, 0 `history_mismatch`; el modelo cae 5/5 en la inyección sutil |
| Claude Code → gateway → LM Studio | §9.5 | ✅ `bench/claude_code_probe.py --lmstudio http://localhost:1234`: lee `NOTAS.md` con `Read`, contesta bien e integridad OK |
| Demo (§13.1) | §13.1 | ✅ `uv run python -m bench.demo && bash demo/demo.sh`: atribución de alice (`TOOL_INJECTED(mcp:issues)`, parcial) y de mallory (`USER_INSTRUCTED`, no repudio) → verify OK → borrado → verify FALLA → atribución `UNVERIFIABLE` (`FULL=1` agrega la reescritura con la llave del gateway). Grabada en `docs/demo.cast` con `bench.record_demo` |
| Ficha: "Método" y "Resultados" | §13.3 | ✅ con las filas de §9.2, §9.3 y G9; `docs/ficha_tecnica.pdf` (2 páginas, 633 KB) con `uv run python docs/ficha_pdf.py` |
| Integración de las ramas y G9 | §11, §2 | ✅ `ManifestGate` en el gateway; `bench.claude_code_hooks_probe` con Claude Code 2.1.325: prompt firmado por el hook, herramienta alterada quitada y denegada |

Cómo reproducir:

```bash
uv sync
uv run pytest -q
uv run python -m bench.tamper --out results
uv run python -m bench.latency --n 1000 --out results
uv run python -m bench.claude_code_probe            # Claude Code real → gateway → fake
uv run python -m bench.local_model --runs 5         # agente → gateway → LM Studio (gpt-oss-20b)
uv run python -m bench.claude_code_probe --lmstudio http://localhost:1234
uv run python -m bench.demo && bash demo/demo.sh    # demo (§13.1)
uv run python -m bench.record_demo --full           # grabación en docs/demo.cast
uv run python docs/ficha_pdf.py                     # ficha técnica en PDF (2 páginas)
```

## Fase 2: G13 — Codex y la Responses API (rama `adrian/codex`)

**Verificación de §17.4 (las 5 incógnitas, con Codex 0.169.0 y un servidor de prueba): todas a favor, G13 va completo.**

| # | Incógnita | Resultado |
|---|---|---|
| 1 | ¿`POST <base_url>/responses` por HTTP con SSE? | ✅ sí, con `wire_api = "responses"` y `env_key` (cabecera `Authorization: Bearer`) |
| 2 | ¿Historial completo, sin `previous_response_id`? | ✅ `store: false`, `include: ["reasoning.encrypted_content"]`, historial completo en cada petición. **Pero Codex no reenvía el `reasoning`** que le devolvió el modelo: por eso el historial se compara ítem por ítem |
| 3 | ¿El demonio hace peticiones por fuera? | ✅ no se vio ninguna; igual `owit exec` pasa `--disable daemon_auto_start` |
| 4 | ¿Nombres MCP y descripciones? | ✅ `{"type": "namespace", "name": "mcp__issues", "tools": [{"name": "list_issues", …}]}`, descripción y esquema intactos; el modelo llama `{"namespace": "mcp__issues", "name": "list_issues"}`. Nombre neutral `mcp__issues__list_issues`, como Claude Code: §9.3 sin falsos positivos |
| 5 | ¿Hooks en `codex exec`? ¿Campos de `PreToolUse`? | ✅ mismo JSON que Claude Code (`tool_name = "mcp__issues__list_issues"`, `tool_use_id = call_id`, salida `hookSpecificOutput.permissionDecision`). `exec_command` llega como **`Bash`** con `{"command"}`: `tool-check` decide por el `tool_call` registrado (`meta.model_tool`). Los hooks no corren hasta aprobarlos en `/hooks` (o `--dangerously-bypass-hook-trust`, que usa el probe) |

| Pieza | Estado |
|---|---|
| `Gateway.adapters` (`anthropic_messages`, `openai_responses`), `POST /sessions` con `protocol`, 400 si el upstream no lo declara, `proxy(protocol, …)`, ruta `/s/<sid>/v1/responses`, `session_start.protocol` real | ✅ |
| `gateway/protocols/openai_responses.py` (tabla de §17.4) y `ResponsesSSEReassembler`; registro justo antes de reenviar `response.completed` | ✅ |
| Historial ítem por ítem (`Session.assistant_block_refs`; comprobación 10 acepta un turno cuyos bloques están todos en `llm_response` del contexto) | ✅ |
| `previous_response_id` → `meta.anomaly = "incomplete_history"` y atribución `AMBIGUOUS` | ✅ |
| `llm/openai_upstream.py` (`openai_compat`, `/v1/responses` y `/v1/chat/completions`) y modo Responses de `FakeUpstream` / `fake_app` | ✅ |
| `owit exec -- codex` (flags `-c model_provider=owit …`, sin `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`CODEX_API_KEY`) y `owit hooks install codex [--user-config]` | ✅ |
| Fixtures `tests/fixtures/codex/0.169.0/{hooks,no-hooks}/` y `tests/test_decompose_responses.py` (20 tests, incluidas las 11 mutaciones de §9.1 sobre una bitácora de Codex) | ✅ 204 en verde |
| `bench/codex_probe.py` con `fake` | ✅ `results/codex_probe.json` (`ok: true`) y `results/codex_probe_no_hooks.json` |
| `bench/codex_probe.py --lmstudio http://localhost:1234/v1` (`gpt-oss-20b`) | ⬜ aquí no hay LM Studio corriendo |
| §9.1 / §9.3 / §9.2 / §9.4 re-corridos | ✅ 364/364 · 47/47 sin falsos positivos · idéntico · +1,02 / +2,28 ms (antes +1,00 / +2,20, ruido), Codex +1,96 ms |

```bash
uv run python -m bench.codex_probe --out results              # Codex real → gateway → fake, con hooks
uv run python -m bench.codex_probe --no-hooks --out results   # mínimo de §17.2: solo observación
uv run python -m bench.codex_probe --save-fixtures            # regraba tests/fixtures/codex/<versión>/
uv run python -m bench.codex_probe --lmstudio http://localhost:1234/v1
```

Atribución sobre la bitácora del probe (reglas): `list_issues` → `USER_INSTRUCTED` (alice, no repudio); `send_email` → `TOOL_INJECTED(mcp:issues)` parcial, `to` aportado por la herramienta: lo mismo que con Claude Code. `exec_command` → `MODEL_INITIATIVE` (el prompt no pide ejecutar nada).

Toqué fuera de mis módulos, en cambios chicos y con tests: `integrations/exec.py` (Juliana: `codex` en `AGENTS`, `_SECRET_ENV`, `AGENT_PROTOCOL`), `cli.py` (bloque de `hooks install codex`), `attribution/attribute.py` y `rules.py` (Andres: `incomplete_history` → `AMBIGUOUS`; categorías de `exec_command`, `apply_patch`…), `ledger/verify.py` (comprobación 10 ítem por ítem). Avisar antes del merge (§17.2).

**Integración (14 de noviembre, 13:00–13:25):** G14 (monitor) → G12 (OpenCode, la mezcló Juliana) → G13 (PR #2) en `main`, en el orden de §17.7. Sobre el árbol integrado: 232 tests, §9.1 364/364, §9.3 47/47, §9.2 idéntico, §9.4 sin empeorar y los tres probes de agentes reales con `ok: true`. **Release v1.1.0** publicado con la ficha (2 páginas) y el resumen (243 palabras). Diagrama de arquitectura actualizado (OpenCode, Codex, Responses API y monitor). Clip del monitor grabado con la interfaz final de Andres (`docs/monitor_pitch.mp4`, `docs/monitor.mp4`) y captura en la ficha. **Entrega final: release v1.1.1** (incluye `/api/stats` del monitor, que la UI final necesita).

## Lo que entregas a los demás

- ✅ Repo, `pyproject.toml` y `types.py` (en `main`). El comando `owit` entra por `openwitness.entry:main`.
- ✅ Fixture de Claude Code en `tests/fixtures/claude_code/2.1.323/`.
- ✅ Gateway que reenvía sesiones completas: con `fake` (`tests/test_gateway.py`) y con Claude Code real (`bench/claude_code_probe.py`). **El punto de decisión de G9 se cumple:** se sigue con Claude Code.
- ✅ Versiones provisionales de `canonical.py`, `identity/`, `tool_filter.py` y `llm/config.py`, para que nadie quede bloqueado (regla en SPEC §11).

## Lo que necesitas de los demás

- **Juliana:**
  - revisar `canonical.py` e `identity/` y agregarles tests;
  - `openwitness.manifests` que exporte `ManifestGate` y `TrustStore` (el gateway los carga solo);
  - `owit hook prompt|pre-tool` y `owit exec`, para G9 con firma.
- **Andres:** `refagent` y sus escenarios (§9.2). §9.1 ya no depende de ellos.

## Lo que falta (en orden)

- [x] Integrar el `ManifestGate` de Juliana (§9.3 por el gateway: 47/47, `tool-check` deniega 47/47).
- [x] G9 con Juliana: Claude Code real con los hooks (`bench/claude_code_hooks_probe.py`).
- [x] Paso de atribución en `demo/demo.sh` y grabación (`docs/demo.cast`).
- [x] Bench §9.4 con `owit hook pre-tool` real.
- [x] Volver a correr los benchmarks sobre la rama integrada y actualizar `docs/ficha/resultados.md`.
- [ ] **Revisión de `types.py` y de §6.4 con el equipo** (`segments`, motivos nuevos de `user_text_unsigned`, `manifest_hash` en `manifest_decision`).
- [ ] Pasar `claude/great-wright-vtjd8d` a `main` (PR) y congelar el repo.
- [ ] Si se quiere la demo en el portátil: `brew install asciinema` y `asciinema play docs/demo.cast`, o regrabar con `asciinema rec`.
- [ ] Volver a correr `bench.run_bench --upstream lmstudio` y `bench.local_model` solo si cambia el código de atribución o del gateway (aquí no hay LM Studio).
- [ ] *(si sobra)* Rekor real (G11).

## Listo cuando

- `verify_ledger` detecta las 12 mutaciones (salvo la cola no anclada sin recibo, que es L3). ✅
- `refagent` → gateway → `fake` produce una bitácora que verifica limpia, con 0 `history_mismatch`. ✅ con las sesiones sintéticas; ⬜ con el `refagent` de Andres.
- Si entró G9: Claude Code pasa por el gateway con 0 textos sin emparejar. 🟡 ya pasa por el gateway; falta el hook de firma.

## Lecciones

**Del prototipo (13 de noviembre):**
- Claude Code llama a `/s/<sid>/v1/messages?beta=true`: la ruta tiene que aceptar la query.
- Sin `ENABLE_TOOL_SEARCH=false`, las herramientas MCP llegan diferidas, a través de `ToolSearch` y `tool_reference` (§5.3).
- `metadata`, `thinking`, `context_management` y `output_config` se reenvían sin tocar.

**De la implementación (14 de noviembre):**
- **Floats:** las peticiones reales los traen. Prohibidos solo en lo firmado; los blobs se hashean sobre sus bytes (§6.1).
- **Carrera agente ↔ bitácora:** si la respuesta se registra después del último chunk, el hook `PreToolUse` puede llegar antes que el `tool_call`. Se registra justo antes de reenviar el chunk con `message_stop` (§7.5).
- **`claude -p` lanzado desde un script:** pasar `stdin` vacío, o Claude Code espera 3 s.
- **Fixtures:** antes de versionarlas, redactar `metadata.user_id` (`device_id`) y las rutas locales.
