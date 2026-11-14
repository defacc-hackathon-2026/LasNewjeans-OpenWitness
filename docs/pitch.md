# Guion del pitch (3 minutos, sin preguntas, sin demo)

> SPEC §13.1. En el pitch no hay demo: los jurados la ven por su cuenta en el repositorio (README, `docs/demo.cast`, `docs/monitor_pitch.mp4`).
> **No hay diapositivas: se proyecta la ficha** (`docs/ficha_tecnica.pdf`, 2 páginas). La página 1 (resumen, amenaza, lo que construimos y la Figura 1) queda en pantalla hasta los resultados. A los 2:15 se pasa a la página 2 (Tabla 1).
> Objetivo: que se entienda **la idea** y **cómo funciona**. El guion sigue el orden de la ficha y usa sus mismos números. Release de la entrega: **v1.2.0**.

## 0:00–0:25 · La amenaza (ficha, sección 1)
"Un agente de IA manda información de un proyecto a un correo externo. ¿Quién lo pidió: el usuario, o un texto que trajo una herramienta? Hoy nadie lo puede probar. El usuario culpa a una inyección, el proveedor de la herramienta culpa al usuario, y los registros los controla un administrador que podría editarlos."

## 0:25–1:20 · Lo que construimos (sección 2, señalar la Figura 1)
"Open Witness es un gateway entre el agente y su modelo. *(Señalar el centro de la figura.)* No modifica el agente: hoy funciona con **Claude Code, OpenCode y Codex**. Todo lo que ve el modelo pasa por ahí, y de eso dejamos tres pruebas.

**Uno, qué pidió el usuario.** Cada mensaje se firma con la llave del usuario, y el gateway le devuelve un recibo firmado.

**Dos, qué herramientas vio el modelo.** Cada herramienta tiene que coincidir con un manifiesto firmado por quien la publica. Si alguien la alteró, el modelo nunca la ve.

**Tres, qué había en el contexto cuando actuó.** Cada turno entra en una bitácora encadenada, firmada y anclada, que marca de dónde vino cada fragmento: del usuario, de una herramienta o del modelo."

## 1:20–2:15 · Qué responde ante un incidente (señalar "Auditoría", abajo en la figura)
"Con esa evidencia, un auditor hace dos cosas, en este orden.

**Primero, comprueba que nadie tocó la bitácora.** Si un administrador borra o reescribe algo, la verificación falla y el veredicto es *evidencia alterada*: no culpamos a nadie con evidencia alterada.

**Después, atribuye cada acción, dato por dato.** Un ejemplo: Alice pide *'resume los issues y manda el resumen a quien corresponda'*. Un issue dice, sin dar ninguna orden, que los resúmenes van a una dirección del atacante, y el agente la usa. Veredicto: Alice pidió el envío, pero **el destinatario lo puso la herramienta de issues**. Si en cambio es Mallory quien pide lo mismo, su mensaje firmado la liga a la acción, **sin que pueda negarlo**."

## 2:15–2:45 · Resultados (pasar a la página 2, Tabla 1)
"Detectamos **364 de 364** manipulaciones de la bitácora. Con un modelo real, gpt-oss-20b, atribuimos bien **74 de 74** acciones dañinas. Un juez LLM que lee el mismo contexto acierta 21 de 35 escenarios; nuestras reglas, **35 de 35**. Y no nos evaluamos solo con nuestros ejemplos: con **264 ataques reales** de una competencia de Microsoft, **nunca culpamos a la víctima**. El gateway añade **unos dos milisegundos**."

## 2:45–3:00 · Límites y cierre (sección 5)
"La criptografía prueba qué vio el modelo, no por qué decidió: cuando la evidencia no alcanza, lo decimos. La demo y los benchmarks están en el repositorio.

**No solo quién pidió la acción, sino de dónde salió cada dato.**"

## Notas para ensayar
- Unas 400 palabras: a ritmo tranquilo cabe en 3 minutos. Si a los 2:15 no han empezado los resultados, digan solo "364 de 364", "74 de 74" y la frase de los ataques reales, y pasen al cierre.
- La ficha está en pantalla: no la lean en voz alta, señálenla. Los veredictos se dicen como en la ficha: *lo pidió el usuario*, *lo metió la herramienta*, *no alcanza la evidencia*, *evidencia alterada*.
- No nombrar reglas (R2b), comandos (`owit …`) ni etiquetas en inglés (`TOOL_INJECTED`).
- No decir "Rekor": el ancla de la demo es simulada (Rekor está en el roadmap).
- "Unos dos milisegundos" es la latencia añadida hasta el primer byte con una petición real de Claude Code, en localhost (Tabla 1: 0,89 → 3,12 ms). El hook que firma el mensaje cuesta unos 84 ms, pero eso no está en la ficha; solo mencionarlo si preguntan.
- "264 ataques reales de una competencia de Microsoft" es LLMail-Inject (referencia [1] de la ficha). De los 237 ataques que ocurrieron, atribuimos bien 224; los otros 13 quedan *no alcanza la evidencia*, nunca contra la víctima.

## Antes de subir (checklist)
- [ ] Ensayo con cronómetro, con la ficha proyectada y el paso a la página 2 a los 2:15.
- [ ] Comprobar desde el fondo de la sala que la página 1 se lee, sobre todo la Figura 1.
- [ ] README del repo con las instrucciones de la demo para los jurados (`uv run python -m bench.demo && bash demo/demo.sh`, `docs/demo.cast`, `docs/monitor_pitch.mp4`).
- [ ] Formulario enviado con el link al repo y al release v1.2.0.
