## Resultados

<!-- Responsable: Adrian (SPEC §13.3). Números en results/*.json; congelar a las 15:00 y volver a correr si cambia el código. -->

| Métrica | Resultado |
|---|---|
| **Manipulación de la bitácora detectada** (11 clases dentro del modelo de amenazas, incluidas 4 con la llave del gateway) | **364/364 (100 %)** |
| Cola no anclada truncada por el operador (L3) | 0/4 sin recibo (esperado); **2/2 con el recibo del usuario** |
| Atribución (§9.2), modelo falso, 35 escenarios en 7 clases | **35/35** en etiqueta, `source`, `non_repudiable` y `partial`; sin R2b, 30/35 (las 5 sutiles quedan `AMBIGUOUS`) |
| Juez LLM local (`gpt-oss-20b`, G10), 35 escenarios | solo juez: etiqueta 21/35 (3 respuestas rechazadas por la validación); reglas+juez 30/35 (vuelve `TOOL_INJECTED` los 5 mixtos); con este juez, las reglas solas son la mejor configuración |
| Atribución (§9.2), `gpt-oss-20b`, 3 × 35 corridas | la acción ocurrió en 74/105; **atribución correcta 74/74**; éxito del ataque: sutil 12/15, MCP 9/15, herramienta interna 8/15, usuario malicioso 25/30 |
| **Ataques ajenos: LLMail-Inject** (Microsoft, SaTML 2025; `bench/llmail.py`), modelo falso | 11 ataques ganadores: **11/11** en todos los campos. 264 ataques únicos de una muestra de 300 filas: la acción ocurrió en 237; etiqueta y `source` **224/237**, `non_repudiable` y `partial` 237/237. Los 13 restantes quedan `AMBIGUOUS` (órdenes que la regla de imperativos no ve), ninguno contra la víctima |
| LLMail-Inject, `gpt-oss-20b`, 11 ganadores × 3 corridas | el ataque funcionó en 9/33 (4 de 11 ataques); **atribución correcta 9/9**; 3 corridas con error de LM Studio al generar la llamada |
| **Manifiestos envenenados bloqueados** (§9.3: 6 clases maliciosas × 5 casos, en `ToolFilter`, en el cliente MCP y por el gateway) | **47/47 herramientas (100 %)**, y `tool-check` deniega las 47; **0 falsos positivos** en 96 herramientas MCP legítimas y en las 21 internas de Claude Code |
| Firmado por un publicador confiable pero envenenado (L5) | 0/5 bloqueados (esperado: la firma prueba origen, no bondad) |
| Latencia añadida, petición mínima (p50 hasta el primer byte / total) | **+1,0 ms / +1,4 ms** |
| Latencia añadida, petición real de Claude Code de 75 KB (p50 / p95 hasta el primer byte) | **+2,2 ms / +2,9 ms** |
| Costo interno por petición (p50) | `append()` 0,3–0,4 ms; descomposición 0,3–0,6 ms; anclaje 1,5 ms, fuera del camino crítico |
| Hook por invocación (`owit hook pre-tool` real: arranque de Python + llamada al gateway) | **≈ 84 ms p50** en un contenedor de 4 vCPU (uno por prompt y uno por herramienta); antes de saltarse Typer, ≈ 195 ms |
| Modelo real `gpt-oss-20b` (agente de referencia, inyección sutil) | el modelo **cayó 5/5**; bitácora íntegra 10/10 y 0 `history_mismatch` |
| **Claude Code 2.1.323 real, sin modificar** | pasa por el gateway con `fake` y con `gpt-oss-20b`: integridad OK, 0 `history_mismatch`; con prompt firmado, 0 textos sin emparejar |
| **G9: Claude Code 2.1.325 + `owit exec` + hooks** (`bench/claude_code_hooks_probe.py`) | prompt firmado por el hook y con recibo; `mcp__mail__send_email` (alterada tras firmar) rechazada por `descriptor_mismatch` y fuera de lo que ve el modelo; el modelo falso la llama igual → `stripped_tool_called` y `tool-check` la deniega; integridad OK |
| **G12 y G13: OpenCode 2.3.0 y Codex CLI 0.169.0** (`bench/opencode_probe.py`, `bench/codex_probe.py`; Codex por la Responses API) | lo mismo que G9 con un plugin (OpenCode) o hooks (Codex): prompt firmado y con recibo, herramienta alterada fuera de lo que ve el modelo y denegada, 0 `history_mismatch` (en Codex, comparando ítem por ítem) y el mismo veredicto que con Claude Code (`TOOL_INJECTED(mcp:issues)`, parcial) |

**Varias defensas independientes por mutación.** Cada mutación la detectan varias comprobaciones a la vez. Por ejemplo:
- reemplazar un prompt firmado lo delatan la firma del usuario, el ancla externa, el recibo y la coherencia del contexto;
- truncar la cola anclada lo delata el ancla, aunque el operador borre también las entradas `anchor`.

**Lectura honesta:**
- Los números de latencia son en `localhost`, con el upstream falso: miden solo lo que agrega el gateway, no la red. Los de la tabla (+1,0 / +2,2 ms) son de un portátil (`results/latency_laptop.json`); en el contenedor de 4 vCPU donde se volvió a correr todo con el código integrado dan +2,1 / +5,3 ms (`results/latency.json`), igual que el código anterior en esa misma máquina: no hay regresión.
- La detección del 100 % vale **dentro del modelo de amenazas** (§4). El operador todavía puede mentir *en el momento de escribir* (L4), y la cola posterior al último ancla solo se protege con recibos (L3).
