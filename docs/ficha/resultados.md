## Resultados

<!-- Responsable: Adrian (SPEC §13.3). Números en results/*.json; congelar a las 15:00 y volver a correr si cambia el código. -->

| Métrica | Resultado |
|---|---|
| **Manipulación de la bitácora detectada** (11 clases dentro del modelo de amenazas, incluidas 4 con la llave del gateway) | **364/364 (100 %)** |
| Cola no anclada truncada por el operador (L3) | 0/4 sin recibo (esperado); **2/2 con el recibo del usuario** |
| Atribución (§9.2, `fake` / `gpt-oss-20b`) | *[Andres: precisión por clase, `source`, `non_repudiable`]* |
| **Manifiestos envenenados bloqueados** (§9.3: 6 clases maliciosas × 5 casos, en `ToolFilter`, en el cliente MCP y por el gateway) | **47/47 herramientas (100 %)**, y `tool-check` deniega las 47; **0 falsos positivos** en 96 herramientas MCP legítimas y en las 21 internas de Claude Code |
| Firmado por un publicador confiable pero envenenado (L5) | 0/5 bloqueados (esperado: la firma prueba origen, no bondad) |
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
