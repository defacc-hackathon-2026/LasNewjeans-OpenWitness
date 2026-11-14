# Adrian — Gateway y bitácora

> Hoja de trabajo derivada de [SPEC.md](../../SPEC.md). Si algo no coincide, manda el SPEC.

**Tu parte en una frase:** el gateway que se pone entre el agente y el LLM, y la bitácora encadenada que deja la evidencia. Es el corazón del sistema y la parte más riesgosa: ya la validaste anoche en el prototipo.

## Estado (14 de noviembre)

Todo está en `main`. `uv run pytest -q` queda en verde.

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
| Bench §9.4 (latencia) | §9.4 | ✅ +1,0 ms (refagent) y +2,2 ms (petición de Claude Code de 75 KB) hasta el primer byte (p50); `append()` ≈ 0,3–0,4 ms; hook ≈ 92 ms (aproximado, hasta que exista `owit hook`) |

Cómo reproducir:

```bash
uv sync
uv run pytest -q
uv run python -m bench.tamper --out results
uv run python -m bench.latency --n 1000 --out results
uv run python -m bench.claude_code_probe            # Claude Code real → gateway → fake
```

## Lo que entregas a los demás

- ✅ Repo, `pyproject.toml` y `types.py` (en `main`).
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

- [ ] **Revisión de `types.py` y de §6.4 con el equipo** (`segments`, motivos nuevos de `user_text_unsigned`, `manifest_hash` en `manifest_decision`).
- [ ] Integrar el `ManifestGate` de Juliana en cuanto llegue:
  - correr `tests/` y `bench.claude_code_probe` con un servidor MCP alterado;
  - comprobar que se quita de `tools` y que `tool-check` lo deniega.
- [ ] **G9 con Juliana:** Claude Code real con los hooks.
  - Ampliar `bench/claude_code_probe.py` para instalar los hooks en el `CLAUDE_CONFIG_DIR` temporal y usar los servidores MCP de demo de Andres.
  - Meta: 0 textos sin emparejar con el prompt firmado.
- [ ] Probar `anthropic_compat` contra LM Studio (`lms server start`, `lms load openai/gpt-oss-20b --context-length 65536`) con `refagent` y con Claude Code.
- [ ] Bench §9.4: volver a medir el hook con `owit hook pre-tool` real (lo detecta solo si existe `openwitness.integrations.hooks`).
- [ ] Grabar la demo en terminal (asciinema, §13.1):
  - inyección sutil;
  - Mallory;
  - `owit tamper` borra el prompt de Mallory;
  - `owit verify` falla.
- [ ] Congelar los números en `results/` y escribir "Método de evaluación" y "Resultados" en `docs/ficha_tecnica.md`.
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
