## Riesgo de doble uso

<!-- Responsable: Andres (SPEC §13.3). Fuente: SPEC §10.2. -->

| Uso indebido | Mitigación |
|---|---|
| **Vigilancia de empleados**: cada acción queda ligada a una identidad firmada y a todo su contexto | Acceso de auditor solo bajo demanda y registrado; retención limitada; cada usuario ve sus sesiones y guarda sus recibos; los blobs se borran sin romper la cadena; juez local |
| **El gateway como punto de recolección** de código, secretos y datos | Tratarlo como sistema sensible (mismo acceso que el proveedor del LLM); el ancla externa solo publica hashes; cifrado de blobs en el roadmap |
| **Culpar al usuario con evidencia incompleta** | `non_repudiable` explícito; integridad (prueba) separada de la causa (inferencia); `AMBIGUOUS` cuando las reglas no deciden o contradicen al juez; `arg_provenance` muestra de dónde salió cada dato |
| **Usar los escenarios como receta de inyección** | Textos sintéticos y genéricos contra herramientas de demo locales (`mail` solo registra; dominios `.test`); ninguno apunta a un producto real; siempre junto con la defensa |
| **Operador que registra cosas falsas en el momento** | No se puede impedir al escribir (L4), pero el ancla externa y los recibos impiden reescribir después |
