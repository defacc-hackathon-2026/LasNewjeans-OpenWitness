"""Levantar una app ASGI con uvicorn en un hilo (tests, bench y demo)."""
from __future__ import annotations

import socket
import threading
import time

import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    def __init__(self, app, host: str = "127.0.0.1", port: int | None = None):
        self.host, self.port = host, port or free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port, log_level="warning",
                                                    lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "ServerThread":
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.01)
        raise RuntimeError("el servidor no arrancó")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
