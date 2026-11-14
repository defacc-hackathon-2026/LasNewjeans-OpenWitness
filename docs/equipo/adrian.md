# Adrian — Gateway y bitácora

> Hoja de trabajo derivada de [SPEC.md](../../SPEC.md). Si algo no coincide, manda el SPEC.

**Tu parte en una frase:** el gateway que se pone entre el agente y el LLM, y la bitácora encadenada que deja la evidencia. Es el corazón del sistema y la parte más riesgosa: tú ya la validaste anoche en el prototipo.

## Qué es tuyo

| Módulo | SPEC | Contenido |
|---|---|---|
| Repo y `pyproject.toml` | §8 | Estructura, dependencias (`mcp>=2,<3`), comando `owit` |
| `types.py` (borrador) | §11 | Tipos neutrales: `Message`, `ToolDef`, `ToolCall`, `LedgerRecord`, `Receipt`… Lo revisan los tres |
| `ledger/` | §6.4, §6.6, §6.7, §7.4 | `BlobStore`, `Ledger`, `MockAnchor` (con `session_id` y `heads_for`), recibos, `verify_ledger` con sus 10 comprobaciones |
| `gateway/` (salvo `tool_filter.py`) | §6.5, §7.5 | Servidor, sesiones y política, `Decomposer`, `prompt_match`, `protocols/anthropic_messages.py` |
| `llm/` upstreams | §7.7 | Reenvío a `anthropic`, `anthropic_compat` (LM Studio) y `fake` |
| Benchmarks | §9.1, §9.4 | Las 12 mutaciones de la bitácora y la latencia |
| Ficha técnica | §13.3 | Secciones "Método de evaluación" y "Resultados" |

## Lo que entregas a los demás

- **9:30** → todos: repo creado, `pyproject.toml` y esqueleto de `types.py`.
- **9:45** → Juliana y Andres: fixture de una sesión real de Claude Code en `tests/fixtures/claude_code/`.
- **14:00** → Andres y Juliana: el gateway reenvía una sesión completa de `refagent` (punto de decisión de G9).

## Lo que necesitas de los demás

- **Juliana, 10:00:** `canonical.py` y `KeyPair`. Hasta entonces, usa un stub con la misma firma.
- **Juliana, 12:30:** `ManifestGate.evaluate_tool()`. Hasta entonces, `ToolFilter` puede aceptar todo.
- **Andres, 11:00:** 2–3 bitácoras de ejemplo hechas a mano para probar `verify_ledger`.
- **Andres, 14:00:** escenarios estables; §9.1 muta las sesiones que generan.

## Checklist por hora

**9:00–9:45 — arranque**
- [ ] Crear el repo público en GitHub, `pyproject.toml` con `[project.scripts] owit = "openwitness.cli:main"` y las dependencias de §8.
- [ ] Esqueleto de `types.py` y repasarlo con Juliana y Andres a las 9:40.
- [ ] Grabar una sesión de Claude Code (versión fijada) con un proxy de registro mínimo. Lanzarlo con `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ENABLE_TOOL_SEARCH=false` y un `CLAUDE_CONFIG_DIR` limpio. Guardar peticiones y respuestas en `tests/fixtures/claude_code/`.
- [ ] Verificar lo pendiente de §5.3: con tu configuración real de Claude Code, ¿el token de sesión gana sobre el login de suscripción?

**9:45–12:30 — bitácora**
- [ ] `BlobStore` y `Ledger.append` (un único escritor por sesión).
- [ ] `MockAnchor` con `submit(head, sig, pubkey, session_id)`, `check` y `heads_for(session_id, pubkey)`.
- [ ] Recibos firmados (§6.6).
- [ ] `verify_ledger` con las 10 comprobaciones de §7.4. No olvidar:
  - la 7 completa: toda cabeza que devuelve `heads_for` existe en la bitácora;
  - la 6: un `tool_call` con `meta.anomaly` no es manipulación.
- [ ] **11:00:** `Ledger` y `verify_ledger` con tests verdes.
- [ ] Bench §9.1 con las 12 mutaciones de la tabla (incluidas las que hace el operador con `gateway_sk`).
- [ ] **12:30:** `verify_ledger` funciona sobre las bitácoras de Andres.

**13:15–15:00 — gateway**
- [ ] Rutas: `/sessions`, `/s/<sid>/prompts` (registra `user_prompt` y devuelve el recibo), `/s/<sid>/tool-check` (registra `tool_check`), `/s/<sid>/end`, `/s/<sid>/v1/messages` **con query string** (`?beta=true`) y passthrough para el resto.
- [ ] Proxy con streaming: devolver los bytes tal cual y reensamblar el SSE en paralelo.
- [ ] `Decomposer` (§6.5): hilos, sufijo nuevo, `context_reset`, emparejar el prompt firmado y las reglas de texto del agente. Esas reglas son: `<system-reminder>`, el texto junto a un `tool_result` o a un prompt firmado, y los mensajes con `role: "system"`.
- [ ] Normalización de bloques (§6.1): quitar `cache_control` y conservar `tool_name`.
- [ ] Upstreams: `fake` y `lmstudio` (`http://localhost:1234`, sin `/v1`).
- [ ] **14:00 — decisión de G9:** ¿reenvía una sesión completa de `refagent` con integridad OK? Si sí, sigue Claude Code con Juliana. Si no, se descarta G9 y te enfocas en estabilizar.
- [ ] Bench §9.4: con y sin gateway, hasta el primer byte y total, `append()` y hooks.
- [ ] Grabar la demo en terminal (asciinema) hacia las 14:45 (§13.1).

**15:00–16:00 — cierre**
- [ ] **15:00:** congelar los números en `results/`.
- [ ] Escribir "Método de evaluación" y "Resultados" en `docs/ficha_tecnica.md`.
- [ ] Merge final a `main`; el README explica cómo reproducir.

## Listo cuando

- `verify_ledger` detecta las 12 mutaciones (salvo la cola no anclada sin recibo, que es L3).
- `refagent` → gateway → `fake` produce una bitácora que verifica limpia, con 0 `history_mismatch`.
- Si entró G9: Claude Code pasa por el gateway con 0 textos sin emparejar.

## Lecciones del prototipo (13 de noviembre)

- Claude Code llama a `/s/<sid>/v1/messages?beta=true`: la ruta tiene que aceptar la query.
- Sin `ENABLE_TOOL_SEARCH=false`, las herramientas MCP llegan diferidas, a través de `ToolSearch` y `tool_reference` (§5.3).
- `metadata`, `thinking`, `context_management` y `output_config` se reenvían sin tocar.
- El costo medido fue de unos 3,7 ms por llamada y 0,36 ms por `append()`: no hace falta optimizar.
