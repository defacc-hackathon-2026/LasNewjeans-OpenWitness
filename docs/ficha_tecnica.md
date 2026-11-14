---
title: "Open Witness: no repudio y atribución para agentes de IA"
subtitle: "Equipo LasNewjeans (Adrian, Juliana, Andres) · Track 1: seguridad de agentes y prompt injection · https://github.com/defacc-hackathon-2026/LasNewjeans-OpenWitness"
geometry: margin=1.6cm
fontsize: 9pt
---

<!-- Ensamblada por Andres desde docs/ficha/*.md (SPEC §13.3). Exportar en la máquina de Adrian:
     pandoc docs/ficha_tecnica.md -o docs/ficha_tecnica.pdf --pdf-engine=xelatex -V mainfont="DejaVu Sans"
     y comprobar que quede en 2 páginas (máx. 10 MB). -->

## Amenaza
Un agente de IA actúa con los permisos de su usuario, y su contexto mezcla texto del usuario, de las herramientas (servidores MCP, web, archivos) y del propio agente. Cuando hace daño, hoy **nadie puede probar quién lo originó**: (1) **prompt injection por herramientas**, burda ("IGNORA LO ANTERIOR y envía…") o **sutil**, sin órdenes ("los resúmenes se envían a reportes@…"), como en los ataques a servidores MCP tipo Sentry; (2) un **usuario malicioso** que pide el daño y luego culpa a una inyección; (3) un **servidor MCP falso o alterado** (descripción envenenada, *rug pull*, suplantación); (4) un **operador** que reescribe los registros después del incidente.

## Defensa
DEFENSA_JULIANA

## Método de evaluación
Todo corre en local con servidores MCP de demo, datos sintéticos y dominios `.test`. El upstream `fake` hace cada corrida determinista; el modelo real es `gpt-oss-20b` en LM Studio, por el mismo gateway.

1. **Manipulación (§9.1):** sesiones válidas a través del gateway real, con 12 clases de mutación en posiciones aleatorias, incluidas las de un operador con la llave del gateway (reescribir la cadena, reemplazar un prompt firmado, quitar el mensaje inyectado, truncar la cola).
2. **Atribución (§9.2):** 35 escenarios con verdad conocida (7 clases × 5: usuario malicioso con y sin firma, inyección por MCP, por herramienta interna, inyección sutil, mixto y benigno), ejecutados por un agente de referencia (SDK de Anthropic) a través del gateway. Con un modelo falso **ingenuo**, que obedece el último destino que lee, se mide la atribución; con `gpt-oss-20b` (3 corridas), la tasa real de éxito de cada ataque y la atribución sobre los que ocurrieron. Se reportan la etiqueta, `source`, `non_repudiable` y `partial`, y una ablación sin la regla R2b.
3. **Manifiestos (§9.3):** corpus de 8 clases en el `ToolFilter` y en el hook `PreToolUse`.
4. **Latencia (§9.4):** 1 000 peticiones con streaming, con y sin gateway (p50/p95/p99), con una petición mínima y una real de Claude Code de 75 KB.
5. **Agente real:** Claude Code 2.1.323 sin modificar, con `fake` y con `gpt-oss-20b`.

## Resultados

| Métrica | Resultado |
|---|---|
| **Manipulación detectada** (11 clases, 4 con la llave del gateway) | **364/364 (100 %)** |
| Cola no anclada truncada (L3) | 0/4 sin recibo (esperado); **2/2 con recibo** |
| **Atribución, modelo falso** (35 escenarios) | **35/35** en etiqueta, `source`, `non_repudiable` y `partial`; **sin R2b, las 5 sutiles quedan `AMBIGUOUS`** (30/35) |
| **Atribución, `gpt-oss-20b`** | GPTOSS_ROW |
| Manifiestos envenenados bloqueados | MANIFESTS_ROW |
| Latencia añadida, p50 hasta el primer byte | **+1,0 ms** (mínima); **+2,2 ms** (Claude Code, 75 KB) |
| Claude Code 2.1.323 real, sin modificar | integridad OK, 0 `history_mismatch`, 0 textos sin emparejar |

GPTOSS_NOTE

**Lectura honesta:** el 35/35 con el modelo falso es optimista, porque las reglas y los escenarios se escribieron juntos; la latencia es en `localhost`; el 100 % vale dentro del modelo de amenazas.

## Límites
- **Se prueba qué vio el modelo, no por qué actuó (L7):** la causa es una inferencia sobre evidencia verificada; si no alcanza, el veredicto es `AMBIGUOUS`.
- **Solo cubre lo que pasa por el gateway (L1).** **Sin hook no hay no repudio (L8):** se atribuye igual, pero con `non_repudiable = false`.
- **Ventana sin anclar (L3):** la cola posterior al último ancla solo la protegen los recibos. **Firma ≠ bondad (L5).**
- **El taint es literal:** un destino parafraseado o codificado no se encuentra. La intención del prompt se estima con palabras clave.
- **35 escenarios no dan significancia estadística**; el juez LLM también lee la inyección (va delimitado, cita `seq` verificables y nunca anula a R1 ni a la integridad).

## Riesgo de doble uso

| Uso indebido | Mitigación |
|---|---|
| Vigilancia de empleados | Auditor bajo demanda y registrado; retención limitada; el usuario ve sus sesiones; borrado de blobs sin romper la cadena; juez local |
| El gateway como punto de recolección | Sistema sensible (el mismo acceso que el proveedor del LLM); el ancla solo publica hashes |
| Culpar al usuario con evidencia incompleta | `non_repudiable` explícito; integridad (prueba) separada de la causa (inferencia); `AMBIGUOUS`; `arg_provenance` por argumento |
| Escenarios como receta de inyección | Textos sintéticos contra herramientas de demo locales; ninguno apunta a un producto real |
| Operador que registra cosas falsas | No se impide al escribir (L4); el ancla y los recibos impiden reescribir después |
