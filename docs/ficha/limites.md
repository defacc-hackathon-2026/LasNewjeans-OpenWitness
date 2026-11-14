## Límites

<!-- Responsable: Andres (SPEC §13.3). Fuente: SPEC §4 (L1–L10) y lo que mostró el benchmark. -->

- **La criptografía prueba qué vio el modelo, no por qué actuó (L7).** La causa es una inferencia: reglas sobre evidencia ya verificada. Por eso el veredicto separa la integridad (prueba) de la atribución (inferencia), y dice `AMBIGUOUS` cuando la evidencia no alcanza.
- **Solo cubre lo que pasa por el gateway (L1).** Un agente con su propia API key no deja rastro.
- **Sin hook no hay no repudio (L8).** El texto sin firma se atribuye igual, pero con `non_repudiable = false`.
- **Ventana sin anclar (L3).** El operador puede truncar la cola posterior al último ancla; solo la delata un recibo del usuario.
- **Firma ≠ bondad (L5).** Un publicador confiable puede firmar una descripción envenenada.
- **El taint es literal.** Si el modelo parafrasea o codifica un destino (base64, partes sueltas), las reglas no lo encuentran y caen en R4 o `AMBIGUOUS`. El texto libre (cuerpo de un correo) se reporta pero no decide.
- **La intención del prompt se estima con palabras clave** (en español e inglés). Un prompt muy vago cae en `AMBIGUOUS`, a propósito.
- **El 100 % con el modelo falso es optimista:** las reglas y los escenarios se escribieron juntos y ese modelo siempre cae. La corrida con `gpt-oss-20b` mide la tasa real de éxito de los ataques; con 35 escenarios no hay significancia estadística.
- **El juez LLM también lee la inyección.** Va delimitado, debe citar `seq` que existan y nunca anula a R1 ni a la integridad.
