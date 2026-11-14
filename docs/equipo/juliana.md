# Juliana — Firmas, manifiestos e integración con Claude Code

> Hoja de trabajo derivada de [SPEC.md](../../SPEC.md). Si algo no coincide, manda el SPEC.

**Tu parte en una frase:** la criptografía de identidad (quién pidió qué) y de herramientas (qué herramientas son legítimas), y conectar Claude Code al gateway con sus hooks. Tu `canonical.py` es lo primero que necesitan los demás.

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
- **12:30** → Adrian: `ManifestGate.evaluate_tool(server, name, description, input_schema, seen_owner)`.
- **14:00–15:00** → Adrian: hooks y `owit exec` funcionando con Claude Code (si sigue G9).

## Lo que necesitas de los demás

- **Adrian, 9:45:** fixture de una sesión real de Claude Code, para ver la lista `tools` y el JSON de los hooks.
- **Adrian, 14:00:** gateway con `/prompts` y `/tool-check` funcionando.

## Checklist por hora

**9:00–10:00 — lo que desbloquea a todos**
- [ ] Revisar `types.py` con el equipo a las 9:40.
- [ ] `canonical.py`: claves ordenadas, `separators=(",", ":")`, `ensure_ascii=False`, error si hay floats.
- [ ] `KeyPair`: `generate`, `load`/`save` (permisos 600), `public_str` (`ed25519:<base64>`), `sign(domain, obj)` sobre `domain || 0x00 || sha256(canonical(obj))`, y `verify`.
- [ ] Tests: el orden de las claves no cambia el hash; un float falla; una firma de un dominio no verifica en otro.
- [ ] **10:00: push y aviso al equipo.**

**10:00–12:30 — identidad y manifiestos**
- [ ] `KeyRegistry` con `register`, `revoke(at)` e `is_valid(user, pubkey, at)`.
- [ ] `sign_prompt` y `verify_prompt`: firma, registro, `content_hash` y nonce repetido (`replay`).
- [ ] **11:00:** firmas con tests verdes.
- [ ] `sign_manifest` con `tool_hash = hash({name, description, input_schema})` por herramienta.
- [ ] `TrustStore` (`trust`, `add_manifest`, `pin`) y `ManifestGate` con los motivos `unsigned`, `bad_sig`, `untrusted_key`, `descriptor_mismatch`, `rug_pull` y `shadowing`, más `builtin` para las herramientas internas.
- [ ] Corpus §9.3: 5 casos por clase, incluidos los legítimos y los firmados pero envenenados (L5).
- [ ] **12:30:** `ManifestGate` entregado a Adrian.

**13:15–15:00 — integración**
- [ ] `tool_filter.py`: separar `mcp__<server>__<tool>`, evaluar, registrar `manifest_decision` (una vez por herramienta, o si cambia) y quitar de `tools` las rechazadas.
- [ ] Reemplazar `{"type": "tool_reference", "tool_name": X}` por un texto de bloqueo si X está rechazada (§6.3).
- [ ] `owit hook prompt`: lee `prompt` y `session_id` de stdin, firma con `OWIT_USER_KEY`, hace `POST /s/<sid>/prompts` y guarda el recibo. **No escribe nada en stdout con `exit 0`**, porque se agregaría al contexto del modelo.
- [ ] `owit hook pre-tool`: lee `tool_name`, `tool_input` y `mcp_server`, hace `POST /tool-check` y, si la respuesta es `deny`, imprime `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "…"}}`.
- [ ] `owit exec --user <u> -- claude`: crea la sesión y lanza Claude Code con `ANTHROPIC_BASE_URL=…/s/<sid>`, `ANTHROPIC_AUTH_TOKEN=<token>`, `ENABLE_TOOL_SEARCH=false` y las variables `OWIT_*`.
- [ ] Con Adrian (si sigue G9): Claude Code real con los servidores MCP de demo de Andres y un servidor alterado tras firmar. El servidor alterado se quita y, si el modelo lo llama igual, el hook lo deniega.

**15:00–16:00 — cierre**
- [ ] Bench §9.3 en `results/manifests.json`.
- [ ] Escribir "Defensa" en `docs/ficha_tecnica.md`: el diagrama y los tres mecanismos en pocas líneas.

## Listo cuando

- Los tests de firma, replay y revocación están verdes.
- En §9.3, 100 % de los maliciosos bloqueados en las 6 clases y 0 falsos positivos. Los firmados pero envenenados pasan, y se reporta aparte como L5.
- De punta a punta: una herramienta alterada se quita de la petición y `tool-check` la deniega.

## Lecciones del prototipo (13 de noviembre)

- **Claude Code no reescribe las descripciones MCP:** se puede firmar directamente sobre el `tools/list` del servidor.
- SDK de MCP 2.x: `from mcp.server.mcpserver import MCPServer` (antes `FastMCP`), y en el cliente `tool.input_schema` (antes `inputSchema`).
- `PreToolUse` trae `mcp_server`, que da el origen sin parsear el nombre.
- Cada hook tarda unos 105 ms en arrancar (Python). Es aceptable para el día.
