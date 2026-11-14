# Andres — Atribución, benchmark y entregables

> Hoja de trabajo derivada de [SPEC.md](../../SPEC.md). Si algo no coincide, manda el SPEC.

**Tu parte en una frase:** el verificador que responde "¿lo pidió el usuario o lo inyectó la herramienta Z, y de dónde salió cada dato?", los escenarios que lo prueban, y todo lo que se entrega al jurado. Tú envías el formulario.

## Qué es tuyo

| Módulo | SPEC | Contenido |
|---|---|---|
| `attribution/` | §7.9 | Evidencia, procedencias, taint, reglas R1, R1b, R2, **R2b**, R3 y R4, `arg_provenance`, `partial`; juez opcional (G10) |
| `refagent/` | §7.8 | Agente mínimo con el SDK de Anthropic contra el gateway; servidores MCP de demo `issues`, `mail`, `files` y `http_fetch` (sintéticos, §10.3) |
| `llm/` (lado del juez) | §7.7 | `config.py` (`owit.toml` y redacción de secretos), modelo `fake`, `LLMClient` para el juez |
| `bench/` | §9.2 | 35 escenarios (7 clases × 5), corrida con `fake` y corrida con `gpt-oss-20b` |
| Entregables | §13 | Resumen, ficha (Amenaza, Límites, Doble uso, y el ensamblado final), guion del pitch, README con citas (§10.4), **formulario** |

## Lo que entregas a los demás

- **11:00** → Adrian: 2–3 bitácoras de ejemplo hechas a mano (formato §6.4) para probar `verify_ledger`.
- **13:15** → Juliana: servidores MCP de demo ejecutables por stdio, para la prueba con Claude Code.
- **14:00** → Adrian: escenarios y `bench/run_bench.py` estables (§9.1 usa esas sesiones).
- **16:00** → jurado: el formulario enviado.

## Lo que necesitas de los demás

- **Juliana, 10:00:** `canonical.py` y `KeyPair` (para firmar prompts en los escenarios).
- **Adrian, 9:45:** fixture de Claude Code, para ver cómo llegan los mensajes con rol `system` y los recordatorios.
- **Adrian, 14:00:** gateway que reenvía sesiones completas.
- **Los tres, 15:00:** números congelados en `results/`.

## Checklist por hora

**9:00–9:45 — arranque**
- [ ] Revisar `types.py` con el equipo a las 9:40, sobre todo `Verdict` (con `arg_provenance` y `partial`).
- [ ] Crear `docs/resumen.md` y `docs/ficha_tecnica.md` a partir de §13.2 y §13.3.

**9:45–12:30 — modelo falso, escenarios y reglas**
- [ ] Modelo `fake`: un servidor con el protocolo Anthropic que devuelve SSE y obedece la última instrucción que ve (del usuario o de una herramienta).
- [ ] Servidores de demo: `mail` solo registra en un outbox, nunca envía; los destinatarios usan dominios `.test`.
- [ ] Escribir las 2–3 bitácoras de ejemplo y pasárselas a Adrian a las 11:00.
- [ ] Atribución sobre esas bitácoras:
  - [ ] integridad primero: si falla en cualquier entrada anterior, el veredicto es `UNVERIFIABLE`;
  - [ ] evidencia: `tool_call` → `llm_request` → entradas del contexto, cada fragmento con su procedencia;
  - [ ] argumentos sensibles (correos, URLs, rutas) y dónde aparece cada uno (`arg_provenance`);
  - [ ] reglas en orden: R1, R1b, R2, **R2b** (el usuario pidió la acción y el destino salió solo de la herramienta Z, que da `TOOL_INJECTED(Z)` con `partial = true`), R3 y R4.
- [ ] 35 escenarios, 5 por clase: usuario malicioso con firma, usuario malicioso sin firma, inyección por MCP, inyección por herramienta interna, **inyección sutil**, mixto y benigno.
- [ ] **12:30:** las reglas aciertan sobre las bitácoras hechas a mano.

**13:15–15:00 — benchmark**
- [ ] `refagent` a través del gateway: firma el prompt (`/prompts`), consulta `/tool-check` antes de cada herramienta y cierra con `/end`.
- [ ] Corrida con `fake`: matriz de confusión y precisión de la etiqueta, de `source`, de `non_repudiable` y de `partial`.
- [ ] Corrida con `gpt-oss-20b` en la máquina de Adrian (ya tiene el modelo descargado). Antes: `lms server start` y `lms load openai/gpt-oss-20b --context-length 65536`. Reportar la tasa real de éxito por clase y la atribución sobre los ataques que sí ocurrieron.
- [ ] Si sobra tiempo (G10): juez con el perfil `lmstudio`, respuesta en JSON validada y las mitigaciones de §7.9.

**15:00–16:00 — entregables (tú envías)**
- [ ] **15:00:** tomar los números de `results/` y completar el resumen (150–250 palabras, §13.2).
- [ ] Ficha técnica: escribir "Amenaza", "Límites" y "Doble uso" y ensamblar las secciones de Adrian y Juliana. **Máximo 2 páginas** y exportar a PDF. `pandoc` no estaba instalado en la máquina de Adrian: instalarlo antes o exportar desde el navegador.
- [ ] README con citas y uso de IA (§10.4).
- [ ] **15:45–16:00: enviar el formulario** con la lista de §13.4.
- [ ] 16:00–17:00: ensayar el pitch de 3 minutos (§13.1) con cronómetro.

## Listo cuando

- Con `fake`, cada clase tiene sus acciones atribuidas y la matriz de confusión está en `results/attribution.json`.
- Las inyecciones sutiles se atribuyen como `TOOL_INJECTED(mcp:issues)` con `partial = true`, no como `AMBIGUOUS`.
- El formulario está enviado a las 16:00, con un resumen de 150–250 palabras y una ficha de 2 páginas como máximo.

## Lecciones del prototipo (13 de noviembre)

- Con R1–R2 solamente, **4 de 5 inyecciones sutiles quedaban `AMBIGUOUS`**. R2b es lo que las atribuye bien.
- `gpt-oss-20b` rechazó a Mallory 10/10 e ignoró las inyecciones burdas 10/10, pero **cayó 5/5 en las sutiles**. Sin la clase sutil, la corrida real no tiene inyecciones que atribuir.
- Si Mallory es rechazada, la bitácora igual conserva su prompt firmado: es prueba del intento, y sirve para el pitch.
- El "benigno" no tiene argumentos sensibles: el veredicto correcto es "no es inyección".
