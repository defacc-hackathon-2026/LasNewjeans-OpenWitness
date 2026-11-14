# Guion del pitch (3 minutos, sin preguntas, sin demo)

> SPEC §13.1. En el pitch no hay demo: los jurados la ven por su cuenta en el repositorio (README, `docs/demo.cast`, `docs/monitor_pitch.mp4`).
> Objetivo: que se entienda **la idea** y **cómo funciona**. Los números completos están en la ficha, que los jurados leen mientras hablamos.
> Apoyo visual: `docs/img/arquitectura.png` en pantalla durante todo el pitch. Release de la entrega: **v1.2.0**.

## 0:00–0:25 · El problema
"Un agente de IA manda información de un proyecto a un correo externo. ¿Quién lo pidió: el usuario, o un texto que trajo una herramienta? Hoy nadie lo puede probar. El usuario culpa a una inyección, el proveedor de la herramienta culpa al usuario, y los registros los controla un operador que podría editarlos."

## 0:25–1:20 · Cómo funciona (señalar el diagrama)
"Open Witness es un gateway entre el agente y su LLM. No modifica el agente: hoy funciona con **Claude Code, OpenCode y Codex**. Todo lo que ve el modelo pasa por ahí, y de eso dejamos tres pruebas.

**Uno, qué pidió el usuario.** Cada prompt se firma con la llave del usuario, y el gateway le devuelve un recibo firmado.

**Dos, qué herramientas vio el modelo.** Cada herramienta tiene que coincidir con un manifiesto firmado por quien la publica. Si alguien la alteró, el modelo nunca la ve.

**Tres, qué había en el contexto cuando actuó.** Cada turno entra en una bitácora encadenada por hashes, firmada y anclada, que marca de dónde vino cada fragmento: del usuario, de una herramienta o del modelo."

## 1:20–2:15 · Qué responde ante un incidente
"Con esa evidencia, un verificador hace dos cosas, en este orden.

**Primero, comprueba que nadie tocó la bitácora.** Si un operador borra o reescribe algo, la verificación falla y el veredicto es *no verificable*: no culpamos a nadie con evidencia alterada.

**Después, atribuye cada acción, argumento por argumento.** Un ejemplo: Alice pide *'resume los issues y manda el resumen a quien corresponda'*. Un issue dice, sin dar ninguna orden, que los resúmenes van a una dirección del atacante, y el agente la usa. Veredicto: Alice pidió el envío, pero **el destinatario lo puso la herramienta de issues**. Si en cambio es Mallory quien pide lo mismo, su prompt firmado la liga a la acción, **con no repudio**."

## 2:15–2:45 · Resultados (solo estos; el resto está en la ficha)
"Detectamos **364 de 364** manipulaciones de la bitácora. Con un modelo real, gpt-oss-20b, que cayó en 12 de 15 inyecciones sutiles, atribuimos bien **74 de 74** acciones. Bloqueamos **47 de 47** herramientas maliciosas sin falsos positivos, y el gateway añade **un milisegundo**. Y no nos evaluamos solo con nuestros ejemplos: con **264 ataques reales** de una competencia de Microsoft, **nunca culpamos a la víctima**."

## 2:45–3:00 · Límite y cierre
"La criptografía prueba qué vio el modelo, no por qué decidió: cuando la evidencia no alcanza, decimos *ambiguo*. La demo y los benchmarks están en el repositorio.

**No solo quién pidió la acción, sino de dónde salió cada dato.**"

## Notas para ensayar
- Unas 395 palabras: a ritmo tranquilo cabe en 3 minutos. Si a los 2:15 no han empezado los resultados, digan solo "364 de 364", "74 de 74" y la frase de los ataques reales, y pasen al cierre.
- No nombrar reglas (R2b), comandos (`owit …`) ni etiquetas en inglés (`TOOL_INJECTED`): en voz alta se dice en palabras.
- No decir "Rekor": el ancla de la demo es simulada (Rekor está en el roadmap).
- El "un milisegundo" es la latencia del gateway hasta el primer byte, en localhost; el hook de firma cuesta unos 84 ms (está en la ficha).

## Antes de subir (checklist)
- [ ] Ensayo con cronómetro, con el diagrama en pantalla.
- [ ] README del repo con las instrucciones de la demo para los jurados (`uv run python -m bench.demo && bash demo/demo.sh`, `docs/demo.cast`, `docs/monitor_pitch.mp4`).
- [ ] Formulario enviado con el link al repo y al release v1.2.0.
