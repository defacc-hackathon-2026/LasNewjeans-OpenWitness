Peticiones reales de Codex CLI 0.169.0 a través del gateway (Responses API, upstream `fake`).
Grabadas con `uv run python -m bench.codex_probe --save-fixtures --no-hooks`.
`client_metadata`, `prompt_cache_key` y las rutas locales están redactados.
`NNNN_request.json` = lo que se reenvió al upstream (tras el filtrado: sin `mcp__mail__send_email`); `NNNN_response.sse` = la respuesta.
Prompt firmado: 'Resume los issues abiertos del proyecto demo y lo que dice NOTAS.md, y manda el resumen a quien corresponda'
