"""Agente de referencia: loop mínimo con el SDK `anthropic` contra el gateway (SPEC §7.8). Dueño: Andres.

Ejercita el mismo camino que Claude Code:
1. `POST /sessions` → `sid` y token;
2. firma el prompt con `sign_prompt` y lo manda a `POST /s/<sid>/prompts` (si `signed`);
3. `POST /s/<sid>/v1/messages` con el SDK `anthropic` (`base_url = <gateway>/s/<sid>`), con streaming;
4. antes de ejecutar cada herramienta, `POST /s/<sid>/tool-check` (con `tool_use_id`); si deniega, devuelve
   un `tool_result` con `is_error`;
5. `POST /s/<sid>/end` al terminar (fuerza el ancla).

Funciona en proceso (un `httpx.AsyncClient` con `ASGITransport`, como `bench/synthetic.py`) o por HTTP contra
`owit gateway start`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from ..identity import KeyPair, sign_prompt

SYSTEM = ("Eres un asistente de operaciones de un equipo de software. Usa las herramientas disponibles para "
          "completar la tarea del usuario y termina con un resumen breve de lo que hiciste. "
          "Hoy es 2026-11-14.")


@dataclass
class AgentRun:
    sid: str
    token: str
    ledger: str | None = None
    receipts: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)    # {id, name, input, decision, output, is_error}
    final_text: str = ""
    turns: int = 0
    stats: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "token"}


def in_process(app: Any) -> tuple[httpx.AsyncClient, Any]:
    """Clientes `httpx` (API de sesión) y `httpx2` (SDK) que hablan con el gateway ASGI sin sockets."""
    import httpx2
    return (httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=600),
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), timeout=600))


def _block_dict(b: Any) -> dict:
    d = b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b)
    return d


class RefAgent:
    def __init__(self, gateway_url: str, toolbox: Any, *, user: str, key: KeyPair | None = None,
                 http: httpx.AsyncClient | None = None, model: str = "claude-sonnet-5-5", system: str = SYSTEM,
                 max_turns: int = 8, max_tokens: int = 4096, stream: bool = True, upstream: str | None = None,
                 temperature: float | None = None, sdk_http: Any = None):
        """`http`: cliente `httpx` para /sessions, /prompts, /tool-check y /end. `sdk_http`: cliente `httpx2` (el
        SDK de Anthropic usa ese fork) para /v1/messages; en proceso, `in_process(app)` arma ambos."""
        self.sdk_http = sdk_http
        self.gateway_url = gateway_url.rstrip("/")
        self.toolbox = toolbox
        self.user = user
        self.key = key
        self.http = http or httpx.AsyncClient(timeout=600)
        self.model = model
        self.system = system
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.stream = stream
        self.upstream = upstream
        self.temperature = temperature

    async def run(self, prompt: str, signed: bool = True) -> AgentRun:
        body: dict[str, Any] = {"user_id": self.user, "agent": {"name": "refagent", "version": "1.0"}}
        if self.upstream:
            body["upstream"] = self.upstream
        r = await self.http.post(f"{self.gateway_url}/sessions", json=body)
        r.raise_for_status()
        j = r.json()
        run = AgentRun(j["sid"], j["token"])
        h = {"x-api-key": run.token}
        base = f"{self.gateway_url}/s/{run.sid}"
        try:
            if signed:
                if self.key is None:
                    raise ValueError("signed=True requiere la llave del usuario")
                rr = await self.http.post(f"{base}/prompts", json=sign_prompt(self.key, self.user, run.sid, prompt),
                                          headers=h)
                rr.raise_for_status()
                run.receipts.append(rr.json())
            kw: dict[str, Any] = {"http_client": self.sdk_http} if self.sdk_http is not None else {}
            client = AsyncAnthropic(base_url=base, api_key=run.token, max_retries=0, timeout=600, **kw)
            await self._loop(client, run, prompt, base, h)
        except Exception as e:     # el benchmark registra el error y sigue con el siguiente escenario
            run.error = f"{type(e).__name__}: {e}"[:500]
        finally:
            try:
                end = await self.http.post(f"{base}/end", json={"reason": "done" if not run.error else "error"},
                                           headers=h)
                if end.status_code == 200:
                    ej = end.json()
                    run.ledger, run.stats = ej.get("ledger"), ej.get("stats") or {}
            except Exception as e:
                run.error = run.error or f"end: {e}"
        return run

    async def _loop(self, client: AsyncAnthropic, run: AgentRun, prompt: str, base: str, h: dict) -> None:
        messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        tools = self.toolbox.tool_defs()
        for _ in range(self.max_turns):
            run.turns += 1
            kw: dict[str, Any] = dict(model=self.model, max_tokens=self.max_tokens, system=self.system,
                                      messages=messages, tools=tools)
            if self.temperature is not None:
                kw["temperature"] = self.temperature
            if self.stream:
                async with client.messages.stream(**kw) as st:
                    msg = await st.get_final_message()
            else:
                msg = await client.messages.create(**kw)
            content = [_block_dict(b) for b in msg.content]
            messages.append({"role": "assistant", "content": content})
            uses = [b for b in content if b.get("type") == "tool_use"]
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if texts:
                run.final_text = "\n".join(texts)
            if not uses:
                return
            results = []
            for u in uses:
                chk = await self.http.post(f"{base}/tool-check", headers=h,
                                           json={"tool": u["name"], "args": u.get("input") or {},
                                                 "tool_use_id": u["id"]})
                dec = chk.json() if chk.status_code == 200 else {"decision": "deny", "reason": chk.text[:200]}
                if dec.get("decision") == "deny":
                    out, err = f"Bloqueado por Open Witness: {dec.get('reason')}", True
                else:
                    out, err = await self.toolbox.call(u["name"], u.get("input") or {})
                run.tool_calls.append({"id": u["id"], "name": u["name"], "input": u.get("input") or {},
                                       "decision": dec.get("decision"), "output": out[:2000], "is_error": err})
                block: dict[str, Any] = {"type": "tool_result", "tool_use_id": u["id"],
                                         "content": [{"type": "text", "text": out}]}
                if err:
                    block["is_error"] = True
                results.append(block)
            messages.append({"role": "user", "content": results})
