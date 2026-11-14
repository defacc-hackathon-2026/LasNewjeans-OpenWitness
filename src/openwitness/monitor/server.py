"""`owit monitor`: API JSON + SSE de solo lectura sobre `data/` (SPEC §17.5, G14). Dueño: Andres.

| Ruta | Devuelve |
|---|---|
| `GET /api/health` | estado del monitor (sin secretos) |
| `GET /api/sessions` | resumen de cada sesión con su insignia de integridad |
| `GET /api/sessions/<sid>/entries?after=<seq>` | entradas con procedencia, resumen y fragmento recortado |
| `GET /api/sessions/<sid>/verify` | el `IntegrityReport` (vuelve a verificar) |
| `GET /api/sessions/<sid>/actions/<seq>` | contexto, veredicto y `arg_provenance` de un `tool_call` |
| `GET /api/alerts[?sid=]` | alertas vigentes |
| `GET /api/stats` | agregados de todas las sesiones (veredictos, fuentes, reglas, serie temporal…) |
| `GET /api/events` | SSE: entradas nuevas, veredictos, alertas y resúmenes de todas las sesiones |

La interfaz (repo `monitoring-UI`, React) se sirve con `--ui <dist>` o desde el servidor de Vite con proxy a `/api`.
Si se escucha fuera de `127.0.0.1`, todas las rutas `/api` exigen el token (`Authorization: Bearer` o `?token=`,
porque `EventSource` no manda cabeceras).
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .state import Monitor

POLL_S = 0.5
KEEPALIVE_S = 15.0


class Bus:
    def __init__(self):
        self.queues: set[asyncio.Queue] = set()

    def publish(self, events: list[dict]) -> None:
        for q in list(self.queues):
            for e in events:
                if q.qsize() < 5000:
                    q.put_nowait(e)


class TokenAuth(BaseHTTPMiddleware):
    def __init__(self, app, token: str | None):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if self.token and request.url.path.startswith("/api"):
            got = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
                or request.query_params.get("token", "")
            if not hmac.compare_digest(got, self.token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def create_app(mon: Monitor, *, token: str | None = None, ui: Path | str | None = None,
               poll: bool = True) -> Starlette:
    bus = Bus()

    async def poller():
        while True:
            try:
                events = await asyncio.to_thread(mon.refresh)
                if events:
                    bus.publish(events)
            except Exception as e:   # el monitor nunca se cae por una bitácora rara
                bus.publish([{"type": "monitor_error", "error": f"{type(e).__name__}: {e}"[:300]}])
            await asyncio.sleep(POLL_S)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        mon.refresh()
        task = asyncio.create_task(poller()) if poll else None
        yield
        if task:
            task.cancel()

    async def health(request: Request):
        return JSONResponse(mon.health())

    async def sessions(request: Request):
        return JSONResponse(mon.list_sessions())

    async def entries(request: Request):
        try:
            after = int(request.query_params.get("after", "-1"))
        except ValueError:
            return JSONResponse({"error": "after debe ser un entero"}, status_code=400)
        out = mon.entries(request.path_params["sid"], after)
        if out is None:
            return JSONResponse({"error": "sesión desconocida"}, status_code=404)
        sv = mon.get(request.path_params["sid"])
        return JSONResponse({"session": mon.summary(sv), "entries": out})

    async def verify(request: Request):
        out = await asyncio.to_thread(mon.verify, request.path_params["sid"])
        return JSONResponse(out) if out is not None else JSONResponse({"error": "sesión desconocida"}, 404)

    async def action(request: Request):
        try:
            seq = int(request.path_params["seq"])
        except ValueError:
            return JSONResponse({"error": "seq inválido"}, status_code=400)
        out = mon.action(request.path_params["sid"], seq)
        return JSONResponse(out) if out is not None else JSONResponse({"error": "no es un tool_call"}, 404)

    async def alerts(request: Request):
        return JSONResponse(mon.alerts(request.query_params.get("sid")))

    async def stats(request: Request):
        return JSONResponse(await asyncio.to_thread(mon.stats))

    async def events(request: Request):
        q: asyncio.Queue = asyncio.Queue()
        bus.queues.add(q)

        async def gen():
            try:
                yield f"event: hello\ndata: {json.dumps({'sessions': len(mon.sessions)})}\n\n"
                while True:
                    try:
                        e = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            finally:
                bus.queues.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    routes = [
        Route("/api/health", health),
        Route("/api/sessions", sessions),
        Route("/api/sessions/{sid}/entries", entries),
        Route("/api/sessions/{sid}/verify", verify),
        Route("/api/sessions/{sid}/actions/{seq}", action),
        Route("/api/alerts", alerts),
        Route("/api/stats", stats),
        Route("/api/events", events),
    ]
    if ui and Path(ui).is_dir():
        routes.append(Mount("/", StaticFiles(directory=str(ui), html=True), name="ui"))
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(TokenAuth, token=token)])
    app.state.monitor = mon
    app.state.bus = bus
    return app
