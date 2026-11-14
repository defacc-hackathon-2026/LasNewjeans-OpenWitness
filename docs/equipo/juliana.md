# Juliana — Firmas, manifiestos e integración con Claude Code

> Hoja de trabajo derivada de [SPEC.md](../../SPEC.md). Si algo no coincide, manda el SPEC.

**Tu parte en una frase:** la criptografía de identidad (quién pidió qué) y de herramientas (qué herramientas son legítimas), y conectar Claude Code al gateway con sus hooks. Tu `canonical.py` es lo primero que necesitan los demás.

## Lo que ya está en `main` (Adrian, 14 de noviembre)

Para que nadie quedara bloqueado, Adrian dejó versiones **provisionales** de tus módulos, que cumplen el contrato de §7.1–7.2. Son tuyos: revísalos, agrégales los tests y reescríbelos si quieres, **sin cambiar la firma pública** (ver la regla en SPEC §11).

| Archivo | Qué hay | Qué te falta |
|---|---|---|
| `canonical.py` | `canonical_json` (falla con floats), `sha256_tagged`, `hash_obj`, `normalize_text`, `ZERO_HASH` | `tests/test_canonical.py` |
| `identity/keys.py` | `KeyPair` (`generate`, `load`/`save` con permisos 600, `public_str`, `sign(domain, obj)`), `verify` y las 5 etiquetas `DOMAIN_*` | Tests: una firma de un dominio no verifica en otro, una llave mal formada da `False` |
| `identity/registry.py` | `KeyRegistry` con `register`, `revoke(at)`, `is_valid(user, pubkey, at)` y `save`/`load` en JSON | Tests de revocación con fecha |
| `identity/prompts.py` | `sign_prompt` y `verify_prompt`, que devuelve `VerifyResult(ok, reason)` con `reason` entre `bad_sig`, `unknown_key`, `content_hash`, `replay` y `malformed` | `tests/test_identity.py`. `verify_ledger` arma el código de error como `"prompt_" + reason`: si agregas motivos, avísale a Andres |
| `gateway/tool_filter.py` | `ToolFilter(gate, policy).evaluate(state, tools) -> list[GateDecision]`; traduce `mcp__s__t` a `(s, t)` y aplica la `builtin_allowlist` | Probarlo con tu `ManifestGate` y con las fixtures |

**Ya no tienes que hacer:**
- registrar `manifest_decision`;
- quitar herramientas de `tools`;
- reemplazar los `tool_reference`.

Eso lo hace el gateway (`gateway/decompose.py` y `protocols/anthropic_messages.py`) con las decisiones de `ToolFilter`, para que la bitácora se escriba en un único sitio (SPEC §7.3).

**Cómo se enchufa tu `ManifestGate`:** `owit gateway start` hace `from openwitness.manifests import ManifestGate, TrustStore`, luego `TrustStore().load_dir(Path("manifests"))` y `ManifestGate(trust_store)`. Si ese import falla, el gateway acepta las herramientas MCP como `unsigned`. Así que en cuanto `openwitness/manifests/__init__.py` exporte esas dos clases, el gateway las usa sin tocar nada más.

**Fixtures de Claude Code 2.1.323:** en `tests/fixtures/claude_code/2.1.323/{no-tool-search,tool-search}/` están las peticiones reales (con la lista `tools` completa) y las respuestas SSE. Los payloads de los hooks están en SPEC §5.3. Para correr Claude Code real contra el gateway: `uv run python -m bench.claude_code_probe` (sin API key, con upstream `fake`). Ahí es donde se enchufan tus hooks para G9.

## Qué es tuyo

| Módulo | SPEC | Contenido |
|---|---|---|
| `canonical.py` | §6.1, §7.1 | JSON canónico (sin floats), `sha256_tagged`, `hash_obj`, `normalize_text` |
| `identity/` | §6.2, §7.2 | `KeyPair` (con etiquetas de dominio), `KeyRegistry` con revocación, `sign_prompt` y `verify_prompt` (anti-replay) |
| `manifests/` | §6.3, §7.3 | `sign_manifest`, `TrustStore` (publicadores y pins), `ManifestGate` con todos los motivos de rechazo |
| `gateway/tool_filter.py` | §6.3, §6.5 | `ManifestGate` aplicado a `tools` en cada petición, y reemplazo de `tool_reference` a herramientas rechazadas |
| `integrations/` | §7.6 | `owit hook prompt`, `owit hook pre-tool`, `owit exec`, `owit hooks install claude-code` |
| Benchmark | §9.3 | Corpus de manifiestos (8 clases) y el caso de punta a punta en el gateway |
| Ficha técnica | §13.3 | Sección "Defensa", con el diagrama de `docs/img/arquitectura.png` |

## Lo que entregas a los demás

- **10:00** → Adrian y Andres: `canonical.py` y `KeyPair` con tests. **Es lo que más bloquea a los demás: va primero.**
- **12:30** → Adrian: `ManifestGate.evaluate_tool(server_id, live: ToolDef, seen_owner: dict) -> GateDecision` (SPEC §7.3). `live.name` es el nombre original del servidor. Rellena `tool_hash` y `manifest_hash` (`hash_obj(SignedManifest.body)`) en la `GateDecision`.
- **14:00–15:00** → Adrian: hooks y `owit exec` funcionando con Claude Code (si sigue G9).

## Lo que necesitas de los demás

- ~~**Adrian, 9:45:** fixture de Claude Code~~ ✅ en `tests/fixtures/claude_code/2.1.323/`.
- ~~**Adrian, 14:00:** gateway con `/prompts` y `/tool-check`~~ ✅ en `main` (formatos en SPEC §7.5).

## Checklist por hora

**9:00–10:00 — lo que desbloquea a todos**
- [ ] Revisar `types.py` con el equipo a las 9:40.
- [ ] `canonical.py`: revisar la versión provisional (claves ordenadas, `separators=(",", ":")`, `ensure_ascii=False`, error si hay floats).
- [ ] `KeyPair`: revisar la versión provisional (`generate`, `load`/`save` con permisos 600, `public_str` como `ed25519:<base64>`, `sign(domain, obj)` sobre `domain || 0x00 || sha256(canonical(obj))`, y `verify`).
- [ ] Tests: el orden de las claves no cambia el hash; un float falla; una firma de un dominio no verifica en otro.
- [ ] **10:00:** push de los tests y aviso al equipo.

**10:00–12:30 — identidad y manifiestos**
- [ ] `KeyRegistry` y `sign_prompt`/`verify_prompt`: ya hay una versión provisional; faltan los tests de firma, registro, `content_hash` y nonce repetido (`replay`).
- [ ] CLI (en tu grupo de `cli.py`): `owit keygen --out keys/alice.key --register alice`, que escribe en `keys/registry.json`, el mismo archivo que leen `owit gateway start` y `owit verify`.
- [ ] **11:00:** firmas con tests verdes.
- [ ] `sign_manifest` con `tool_hash = hash({name, description, input_schema})` por herramienta.
- [ ] `TrustStore` (`trust`, `add_manifest`, `pin`) y `ManifestGate` con los motivos `unsigned`, `bad_sig`, `untrusted_key`, `descriptor_mismatch`, `rug_pull` y `shadowing`, más `builtin` para las herramientas internas.
- [ ] Corpus §9.3: 5 casos por clase, incluidos los legítimos y los firmados pero envenenados (L5).
- [ ] **12:30:** `ManifestGate` entregado a Adrian.

**13:15–15:00 — integración**
- [ ] `tool_filter.py`: conectar tu `ManifestGate` y probarlo con las fixtures. El registro de `manifest_decision`, el quitado y el reemplazo de `tool_reference` ya los hace el gateway.
- [ ] Probar de punta a punta que una herramienta alterada después de firmarse queda fuera de `tools` y que `tool-check` la deniega. Base para el test: `tests/test_gateway.py`.
- [ ] `owit hook prompt`: lee `prompt` y `session_id` de stdin y firma con `OWIT_USER_KEY` usando `sign_prompt(kp, OWIT_USER, OWIT_SESSION, prompt, agent_session_id=session_id)`. El `session_id` del cuerpo firmado tiene que ser el `sid` del gateway, o el gateway responde 400 `wrong_session`. Después hace `POST /s/<sid>/prompts` con `x-api-key: $OWIT_TOKEN` y guarda el recibo en `~/.owit/receipts/<sid>_<seq>.json` (`owit verify --receipts` lee ese directorio). **No escribe nada en stdout con `exit 0`**, porque se agregaría al contexto del modelo.
- [ ] `owit hook pre-tool` (en `integrations/hooks.py`, que es lo que busca `bench/latency.py`): lee `tool_name`, `tool_input`, `tool_use_id` y `mcp_server`, hace `POST /s/$OWIT_SESSION/tool-check` con `{tool, args, tool_use_id}` y `x-api-key: $OWIT_TOKEN` y, si la respuesta es `deny`, imprime `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "…"}}`.
- [ ] `owit exec --user <u> -- claude`: crea la sesión (`POST /sessions` con `{user_id, agent: {name: "claude-code", version}}`, que devuelve `{sid, token}`) y lanza Claude Code con `stdin` heredado, `ANTHROPIC_BASE_URL=…/s/<sid>`, `ANTHROPIC_AUTH_TOKEN=<token>`, `ENABLE_TOOL_SEARCH=false` y `OWIT_GATEWAY`, `OWIT_SESSION`, `OWIT_TOKEN`, `OWIT_USER` y `OWIT_USER_KEY`.
- [ ] Con Adrian (si sigue G9): Claude Code real con los servidores MCP de demo de Andres y un servidor alterado tras firmar. El servidor alterado se quita y, si el modelo lo llama igual, el hook lo deniega.

**15:00–16:00 — cierre**
- [ ] Bench §9.3 en `results/manifests.json`.
- [ ] Escribir "Defensa" en `docs/ficha/defensa.md` (Andres la ensambla en `docs/ficha_tecnica.md`): el diagrama y los tres mecanismos en pocas líneas. Tu fila de §9.3 va en la tabla de `docs/ficha/resultados.md`.

## Listo cuando

- Los tests de firma, replay y revocación están verdes.
- En §9.3, 100 % de los maliciosos bloqueados en las 6 clases y 0 falsos positivos. Los firmados pero envenenados pasan, y se reporta aparte como L5.
- De punta a punta: una herramienta alterada se quita de la petición y `tool-check` la deniega.

## Lecciones del prototipo (13 de noviembre)

- **Claude Code no reescribe las descripciones MCP:** se puede firmar directamente sobre el `tools/list` del servidor.
- SDK de MCP 2.x: `from mcp.server.mcpserver import MCPServer` (antes `FastMCP`), y en el cliente `tool.input_schema` (antes `inputSchema`).
- `PreToolUse` trae `mcp_server`, que da el origen sin parsear el nombre.
- Cada hook tarda unos 105 ms en arrancar (Python). Es aceptable para el día.
