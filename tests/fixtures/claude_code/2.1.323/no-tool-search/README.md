Peticiones reales de Claude Code 2.1.323 a través del gateway (upstream `fake`).
Grabadas con `uv run python -m bench.claude_code_probe --save-fixtures`.
`metadata` (device_id, session_id) y las rutas locales están redactados.
Archivos `NNNN_request.json` = lo que se reenvió al upstream (tras el filtrado); `NNNN_response.sse` = la respuesta.
