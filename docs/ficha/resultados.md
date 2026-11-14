## Resultados

<!-- Responsable: Adrian (SPEC §13.3). Números en results/*.json; congelar a las 15:00 y volver a correr si cambia el código. -->

| Métrica | Resultado |
|---|---|
| **Manipulación de la bitácora detectada** (11 clases dentro del modelo de amenazas, incluidas 4 con la llave del gateway) | **364/364 (100 %)** |
| Cola no anclada truncada por el operador (L3) | 0/4 sin recibo (esperado); **2/2 con el recibo del usuario** |
| Atribución (§9.2), modelo falso, 35 escenarios en 7 clases | **35/35** en etiqueta, `source`, `non_repudiable` y `partial`; sin R2b, 30/35 (las 5 sutiles quedan `AMBIGUOUS`) |
| Juez LLM local (`gpt-oss-20b`, G10), 35 escenarios | solo juez: etiqueta 21/35 (3 respuestas rechazadas por la validación); reglas+juez 30/35 (vuelve `TOOL_INJECTED` los 5 mixtos); con este juez, las reglas solas son la mejor configuración |
| Atribución (§9.2), `gpt-oss-20b`, 3 × 35 corridas | la acción ocurrió en 74/105; **atribución correcta 74/74**; éxito del ataque: sutil 12/15, MCP 9/15, herramienta interna 8/15, usuario malicioso 25/30 |
| Manifiestos envenenados bloqueados (§9.3) | *[Juliana: bloqueados / maliciosos por clase, falsos positivos, L5 aparte]* |
| Latencia añadida, petición mínima (p50 hasta el primer byte / total) | **+1,0 ms / +1,4 ms** |
| Latencia añadida, petición real de Claude Code de 75 KB (p50 / p95 hasta el primer byte) | **+2,2 ms / +2,9 ms** |
| Costo interno por petición (p50) | `append()` 0,3–0,4 ms; descomposición 0,3–0,6 ms; anclaje 1,5 ms, fuera del camino crítico |
| Hook por invocación (arranque de Python + llamada al gateway) | ≈ 92 ms p50 (uno por prompt y uno por herramienta) |
| Modelo real `gpt-oss-20b` (agente de referencia, inyección sutil) | el modelo **cayó 5/5**; bitácora íntegra 10/10 y 0 `history_mismatch` |
| **Claude Code 2.1.323 real, sin modificar** | pasa por el gateway con `fake` y con `gpt-oss-20b`: integridad OK, 0 `history_mismatch`; con prompt firmado, 0 textos sin emparejar |

**Varias defensas independientes por mutación.** Cada mutación la detectan varias comprobaciones a la vez. Por ejemplo:
- reemplazar un prompt firmado lo delatan la firma del usuario, el ancla externa, el recibo y la coherencia del contexto;
- truncar la cola anclada lo delata el ancla, aunque el operador borre también las entradas `anchor`.

**Lectura honesta:**
- Los números de latencia son en `localhost`, con el upstream falso: miden solo lo que agrega el gateway, no la red.
- La detección del 100 % vale **dentro del modelo de amenazas** (§4). El operador todavía puede mentir *en el momento de escribir* (L4), y la cola posterior al último ancla solo se protege con recibos (L3).
