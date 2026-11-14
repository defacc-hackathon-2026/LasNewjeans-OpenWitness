"""Gateway: API de sesión y proxy del LLM con streaming (SPEC §5.3, §6.5, §6.6, §7.5)."""
from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..canonical import normalize_text
from ..identity import KeyPair, KeyRegistry, verify_prompt
from ..ledger import BlobStore, Ledger, MockAnchor
from ..ledger.anchor import Anchor
from ..types import Actor, LedgerRecord, Receipt
from .decompose import Decomposer
from .protocols.anthropic_messages import PROTOCOL as ANTHROPIC, AnthropicMessagesAdapter, SSEReassembler
from .sessions import Policy, Session, new_session_id, new_token
from .tool_filter import Gate, ToolFilter

HOP_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding", "keep-alive"}


class PromptRejected(Exception):
    pass


@dataclass
class Forwarded:
    status: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]
    media_type: str | None


class Gateway:
    def __init__(self, data_dir: Path | str, gateway_key: KeyPair, registry: KeyRegistry, *,
                 upstreams: dict[str, Any], default_upstream: str, gate: Gate | None = None,
                 anchor: Anchor | None = None, anchor_every: int = 20, anchor_interval_s: float = 30.0,
                 default_policy: Policy | dict | None = None, record_dir: Path | str | None = None):
        self.data = Path(data_dir)
        self.kp = gateway_key
        self.registry = registry
        self.blobs = BlobStore(self.data / "blobs")
        self.anchor = anchor if anchor is not None else MockAnchor(self.data / "anchor" / "mock_anchor.jsonl")
        self.anchor_every = anchor_every
        self.anchor_interval_s = anchor_interval_s
        self.upstreams = upstreams
        self.default_upstream = default_upstream
        self.gate = gate
        self.adapter = AnthropicMessagesAdapter()
        self.default_policy = (default_policy if isinstance(default_policy, Policy)
                               else Policy.from_dict(default_policy))
        self.sessions: dict[str, Session] = {}
        self.decomposers: dict[str, Decomposer] = {}
        self.record_dir = Path(record_dir) if record_dir else None
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ sesiones
    def ledger_path(self, sid: str) -> Path:
        return self.data / "ledger" / f"{sid}.jsonl"

    def create_session(self, user_id: str, agent: dict | str | None = None, upstream: str | None = None,
                       policy: dict | None = None) -> Session:
        agent = {"name": agent} if isinstance(agent, str) else dict(agent or {"name": "unknown"})
        up_name = upstream or self.default_upstream
        if up_name not in self.upstreams:
            raise KeyError(f"upstream desconocido: {up_name}")
        pol = Policy.from_dict({**self.default_policy.to_dict(), **(policy or {})})
        sid = new_session_id()
        led = Ledger(self.ledger_path(sid), self.kp, self.blobs, self.anchor, session_id=sid,
                     anchor_every=self.anchor_every, anchor_interval_s=self.anchor_interval_s, auto_anchor=False)
        s = Session(sid, new_token(), user_id, agent, led, pol, up_name)
        up = self.upstreams[up_name]
        led.append("session_start", Actor("gateway", "gateway"),
                   {"user_id": user_id, "agent": agent, "policy": pol.to_dict(), "upstream": up.describe()}, [],
                   {"user_id": user_id, "agent": {k: str(v) for k, v in agent.items()}, "protocol": ANTHROPIC,
                    "upstream": {k: v for k, v in up.describe().items() if isinstance(v, (str, int, bool))},
                    "gateway_pubkey": self.kp.public_str(), "policy": pol.to_dict()})
        self.sessions[sid] = s
        self.decomposers[sid] = Decomposer(ToolFilter(self.gate, pol), self.adapter)
        return s

    def add_prompt(self, s: Session, sp: dict) -> Receipt:
        """§6.6: verifica el SignedPrompt, lo registra de inmediato y devuelve el recibo."""
        vr = verify_prompt(sp, self.registry, s.seen_nonces)
        if not vr.ok:
            raise PromptRejected(vr.reason)
        body = sp["body"]
        if body.get("session_id") != s.sid:
            raise PromptRejected("wrong_session")
        rec = s.ledger.append("user_prompt", Actor("user", body["user_id"]), sp, [],
                              {"user_id": body["user_id"], "signed": True,
                               **({"agent_session_id": body["agent_session_id"]}
                                  if body.get("agent_session_id") else {})})
        s.prompts.append((normalize_text(body["content"]), rec.hash))
        rc = s.ledger.receipt(rec)
        s.receipts.append(rc.to_dict())
        return rc

    def tool_check(self, s: Session, tool: str, args: Any, tool_use_id: str | None = None,
                   mcp_server: Any = None) -> dict:
        d = s.tools.decisions.get(tool)
        origin, _ = self.adapter.tool_origin(tool)
        if d is not None and not d.accepted:
            res = {"decision": "deny", "reason": f"Open Witness rechazó {tool}: {d.reason}"}
        elif d is None and origin.startswith("mcp:"):
            res = {"decision": "deny", "reason": f"Open Witness no verificó {tool} en esta sesión"}
        elif d is None and s.policy.unknown_builtin == "deny":
            res = {"decision": "deny", "reason": f"herramienta interna desconocida: {tool}"}
        else:
            res = {"decision": "allow", "reason": d.reason if d else "unknown_builtin"}
        if res["decision"] == "deny" and not s.policy.deny_stripped_tool_calls:
            res = {"decision": "allow", "reason": "policy_allows:" + res["reason"]}
        call = s.tool_calls.get(tool_use_id or "")
        if call is None:   # sin tool_use_id: la última llamada con ese nombre
            cands = [c for c in s.tool_calls.values() if c.name == tool]
            call = max(cands, key=lambda c: c.seq) if cands else None
        s.ledger.append("tool_check", Actor("hook", "pre-tool"), {"tool": tool, "args": args},
                        [call.hash] if call else [],
                        {"decision": res["decision"], "reason": res["reason"], "tool": tool,
                         "tool_use_id": tool_use_id, "origin": origin})
        self._anchor_soon(s)
        return res

    def end_session(self, s: Session, reason: str = "end") -> LedgerRecord | None:
        if not s.closed:
            s.ledger.append("session_end", Actor("gateway", "gateway"), None, [], {"reason": reason})
            s.closed = True
        return s.ledger.force_anchor()

    # ------------------------------------------------------------------ proxy
    async def proxy_messages(self, s: Session, body: dict, headers: dict[str, str], query: str) -> Forwarded:
        up = self.upstreams[s.upstream]
        async with s.lock:
            req0 = self.adapter.parse_request(body)
            decisions, rejected = self.decomposers[s.sid].filter_tools(s, req0)
            fwd = self.adapter.strip_tools(body, rejected)
            if hasattr(up, "prepare"):
                fwd = up.prepare(fwd)
            req = self.adapter.parse_request(fwd)
            offered = [t.name for t in req0.tools]
            stripped = sorted(rejected & set(offered))
            outcome = self.decomposers[s.sid].on_request(
                s, req, offered=offered, stripped=stripped, upstream=up.describe(),
                extra_meta={"model_requested": body.get("model"), "model_forwarded": fwd.get("model")})
        if outcome.rejected:
            err = {"type": "error", "error": {"type": "permission_error",
                                              "message": "Open Witness: el turno trae texto de usuario sin firma "
                                                         "(require_signed_prompts)."}}
            return Forwarded(403, {}, _one(json.dumps(err).encode()), "application/json")
        req_rec = outcome.record
        self._record("request", s, req_rec.seq, fwd)
        r = await up.forward(ANTHROPIC, fwd, headers, query)
        media = r.headers.get("content-type", "application/json")
        out_headers = {k: v for k, v in r.headers.items()
                       if k.lower() not in HOP_HEADERS and k.lower() != "content-type"}
        if "text/event-stream" in media:
            return Forwarded(r.status, out_headers, self._stream(s, req_rec, r), media)
        return Forwarded(r.status, out_headers, self._json(s, req_rec, r), media)

    async def _stream(self, s: Session, req_rec: LedgerRecord, r) -> AsyncIterator[bytes]:
        """Devuelve los bytes tal cual y reensambla en paralelo.

        La respuesta se registra justo antes de reenviar el chunk que cierra el mensaje (`message_stop`): así,
        cuando el agente ve el final y dispara el hook `PreToolUse` o la siguiente petición, el `tool_call` ya está
        en la bitácora. El primer token nunca espera a la bitácora.
        """
        sse = SSEReassembler()
        recorded = False
        raw: list[bytes] = [] if self.record_dir else None
        try:
            async for chunk in r.body:
                sse.feed(chunk)
                if raw is not None:
                    raw.append(chunk)
                if sse.complete and not recorded and r.status == 200:
                    await self._on_response(s, req_rec, sse.response(r.status))
                    recorded = True
                yield chunk
        finally:
            await r.aclose()
            if not recorded and r.status == 200 and sse.blocks:
                await self._on_response(s, req_rec, sse.response(r.status))
            if raw is not None:
                self._record("response", s, req_rec.seq, b"".join(raw).decode("utf-8", "replace"))

    async def _json(self, s: Session, req_rec: LedgerRecord, r) -> AsyncIterator[bytes]:
        data = b""
        try:
            async for chunk in r.body:
                data += chunk
        finally:
            await r.aclose()
        if r.status == 200:
            try:
                resp = self.adapter.parse_json_response(json.loads(data), r.status)
                await self._on_response(s, req_rec, resp)
            except (json.JSONDecodeError, AttributeError):
                pass
        self._record("response", s, req_rec.seq, data.decode("utf-8", "replace"))
        yield data

    async def _on_response(self, s: Session, req_rec: LedgerRecord, resp) -> None:
        async with s.lock:
            self.decomposers[s.sid].on_response(s, req_rec, resp)
        self._anchor_soon(s)

    def _anchor_soon(self, s: Session) -> None:
        """El anclaje va fuera del camino crítico (§6.7)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            s.ledger.anchor_if_due()
            return
        t = loop.create_task(asyncio.to_thread(s.ledger.anchor_if_due))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    def _record(self, what: str, s: Session, seq: int, data: Any) -> None:
        if not self.record_dir:
            return
        p = self.record_dir / f"{s.sid}_{seq:04d}_{what}.{'json' if what == 'request' else 'sse'}"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1) if not isinstance(data, str) else data)

    # ------------------------------------------------------------------ HTTP
    def auth(self, request: Request) -> Session | None:
        s = self.sessions.get(request.path_params.get("sid", ""))
        if s is None:
            return None
        tok = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix(
            "Bearer ").strip()
        return s if tok and secrets.compare_digest(tok, s.token) else None

    def app(self) -> Starlette:
        return create_app(self)


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _unauthorized() -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": "authentication_error",
                                                    "message": "Open Witness: token de sesión inválido"}}, 401)


def create_app(gw: Gateway) -> Starlette:
    async def sessions(request: Request):
        j = await request.json()
        try:
            s = gw.create_session(j["user_id"], j.get("agent"), j.get("upstream"), j.get("policy"))
        except KeyError as e:
            return JSONResponse({"error": str(e)}, 400)
        return JSONResponse({"sid": s.sid, "token": s.token, "base_path": f"/s/{s.sid}",
                             "gateway_pubkey": gw.kp.public_str(), "upstream": s.upstream})

    async def prompts(request: Request):
        s = gw.auth(request)
        if not s:
            return _unauthorized()
        try:
            rc = gw.add_prompt(s, await request.json())
        except PromptRejected as e:
            return JSONResponse({"error": "prompt_rejected", "reason": str(e)}, 400)
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            return JSONResponse({"error": "malformed", "reason": str(e)}, 400)
        return JSONResponse(rc.to_dict())

    async def tool_check(request: Request):
        s = gw.auth(request)
        if not s:
            return _unauthorized()
        j = await request.json()
        return JSONResponse(gw.tool_check(s, j["tool"], j.get("args", {}), j.get("tool_use_id"),
                                          j.get("mcp_server")))

    async def receipts(request: Request):
        s = gw.auth(request)
        if not s:
            return _unauthorized()
        return JSONResponse(s.receipts)

    async def end(request: Request):
        s = gw.auth(request)
        if not s:
            return _unauthorized()
        try:
            j = await request.json()
        except json.JSONDecodeError:
            j = {}
        await gw.drain()
        gw.end_session(s, j.get("reason", "end"))
        return JSONResponse({"ok": True, "ledger": str(gw.ledger_path(s.sid)), "stats": s.stats})

    async def stats(request: Request):
        s = gw.auth(request)
        if not s:
            return _unauthorized()
        return JSONResponse(s.stats)

    async def messages(request: Request):
        s = gw.auth(request)
        if not s:
            return _unauthorized()
        try:
            body = json.loads(await request.body())
        except json.JSONDecodeError:
            return JSONResponse({"type": "error", "error": {"type": "invalid_request_error",
                                                            "message": "JSON inválido"}}, 400)
        f = await gw.proxy_messages(s, body, dict(request.headers), request.url.query)
        return StreamingResponse(f.body, status_code=f.status, headers=f.headers, media_type=f.media_type)

    async def passthrough(request: Request):
        """Conteo de tokens, listado de modelos, etc.: se reenvía sin registrar (§7.5)."""
        s = gw.auth(request)
        if not s:
            return _unauthorized()
        rest = "/" + request.path_params["rest"]
        up = gw.upstreams[s.upstream]
        r = await up.passthrough(request.method, rest, await request.body(), dict(request.headers),
                                 request.url.query)
        data = b""
        try:
            async for c in r.body:
                data += c
        finally:
            await r.aclose()
        return Response(data, status_code=r.status, media_type=r.headers.get("content-type"))

    async def health(request: Request):
        return JSONResponse({"ok": True, "gateway_pubkey": gw.kp.public_str(), "sessions": len(gw.sessions)})

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/sessions", sessions, methods=["POST"]),
        Route("/s/{sid}/prompts", prompts, methods=["POST"]),
        Route("/s/{sid}/tool-check", tool_check, methods=["POST"]),
        Route("/s/{sid}/receipts", receipts, methods=["GET"]),
        Route("/s/{sid}/end", end, methods=["POST"]),
        Route("/s/{sid}/stats", stats, methods=["GET"]),
        Route("/s/{sid}/v1/messages", messages, methods=["POST"]),
        Route("/s/{sid}/{rest:path}", passthrough, methods=["GET", "POST", "HEAD"]),
    ])
