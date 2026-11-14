## Método de evaluación

<!-- Responsable: Adrian (SPEC §13.3). Andres lo ensambla en docs/ficha_tecnica.md. Fuente: SPEC §9. -->

Todo corre en local, con servidores MCP de demo, datos sintéticos y dominios `.test`. Con el upstream `fake` (respuestas guionizadas), cada corrida es **determinista y reproducible**, sin gastar tokens. El modelo real es `gpt-oss-20b` en LM Studio, a través del mismo gateway.

1. **Manipulación de la bitácora (§9.1).** Se generan sesiones válidas pasando por el gateway real:
   - prompt firmado;
   - herramientas MCP e internas;
   - un resultado con una inyección.

   Se les aplican **12 clases de mutación** en posiciones aleatorias, entre ellas las de un **operador que tiene la llave del gateway**: reescribe la cadena, reemplaza un prompt firmado, quita del contexto el mensaje inyectado o trunca la cola. La métrica es `detectadas / mutaciones` por clase. Truncar la cola *no anclada* se reporta aparte (límite L3), con y sin recibo del usuario.
2. **Atribución (§9.2).** Escenarios con verdad conocida (7 clases × 5), ejecutados con `refagent`. Se reportan la matriz de confusión, la precisión por clase y la exactitud de `source` y `non_repudiable`. Con `fake` se mide la atribución, porque el modelo siempre cae en el ataque; con `gpt-oss-20b`, la tasa real de éxito del ataque.
3. **Manifiestos envenenados (§9.3).** Un corpus de 8 clases (sin firma, firma inválida, llave fuera del trust store, descripción alterada, *rug pull*, *shadowing*, legítimos y firmados pero envenenados) se evalúa en el `ToolFilter` del gateway y en el hook `PreToolUse`.
4. **Latencia (§9.4).** Con el modelo falso servido por HTTP y una respuesta fija, se hacen 1 000 peticiones con streaming, con y sin gateway e intercaladas. Se mide el tiempo hasta el primer byte y el total (p50, p95 y p99), con dos tamaños de petición: una mínima y una real de Claude Code de 75 KB. Aparte se mide el costo de `append()`, de la descomposición, del anclaje (asíncrono) y de un hook.
5. **Agente real.** Claude Code 2.1.323, sin modificarlo, pasa por el gateway con el upstream `fake` y con `gpt-oss-20b`. Se comprueba:
   - la integridad de la bitácora;
   - 0 `history_mismatch`;
   - 0 textos de usuario sin emparejar cuando el prompt va firmado.

   Las peticiones reales quedan como fixtures de regresión.

Reproducir: `uv sync && uv run pytest -q`, y después `uv run python -m bench.tamper`, `bench.latency`, `bench.local_model` y `bench.claude_code_probe` (README).
