"""Descomposición de peticiones al LLM en entradas de bitácora (SPEC §6.5).

Cada petición trae todo el historial. Por hilo se guarda lo ya visto y solo se registra el sufijo nuevo.
El `llm_request` lleva en `context` las entradas que cubren cada mensaje presente y en `meta.segments` el
índice del mensaje que cubre cada una (lo usa la comprobación 10 de `verify_ledger`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..canonical import normalize_text, sha256_tagged
from ..ledger.blobs import encode_payload
from ..normalize import message_text
from ..types import Actor, GateDecision, LedgerRecord, NormalizedRequest, NormalizedResponse
from .prompt_match import find_prompt, looks_agent_injected, remainder
from .protocols.anthropic_messages import AnthropicMessagesAdapter
from .sessions import Session, ThreadState, ToolCallInfo
from .tool_filter import ToolFilter


def content_key(obj: Any) -> str:
    """Clave estable de un mensaje o bloque (admite floats, a diferencia de `hash_obj`)."""
    return sha256_tagged(encode_payload(obj))


def _int_only(d: dict) -> dict:
    """Para `meta` (firmada): solo enteros, strings, bools y dicts de eso."""
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, bool) or isinstance(v, (int, str)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _int_only(v)
    return out


@dataclass
class RequestOutcome:
    record: LedgerRecord | None          # el llm_request (None si se rechazó)
    thread_id: str
    unmatched: list[str] = field(default_factory=list)
    rejected: str | None = None          # motivo si la política impide reenviar
    stripped: list[str] = field(default_factory=list)


class Decomposer:
    def __init__(self, tool_filter: ToolFilter, adapter: AnthropicMessagesAdapter | None = None):
        self.filter = tool_filter
        self.adapter = adapter or AnthropicMessagesAdapter()

    # ------------------------------------------------------------------ herramientas (§6.5 paso 5)
    def filter_tools(self, s: Session, req: NormalizedRequest) -> tuple[list[GateDecision], set[str]]:
        decisions = self.filter.evaluate(s.tools, req.tools)
        for d in decisions:
            name = d.tool.name
            if s.tools.recorded.get(name) != d.tool_hash:
                s.ledger.append("manifest_decision", Actor("gateway", "tool_filter"), d.tool.descriptor(), [],
                                d.to_meta())
                s.tools.recorded[name] = d.tool_hash or ""
        rejected = {d.tool.name for d in decisions if not d.accepted} if s.policy.strip_untrusted_tools else set()
        # tool_reference a herramientas rechazadas en esta u otras peticiones de la sesión
        rejected |= s.tools.rejected & self.adapter.referenced_tools({"messages": [m.to_dict() for m in req.messages]})
        return decisions, rejected

    # ------------------------------------------------------------------ petición (§6.5 pasos 2–6)
    def on_request(self, s: Session, req: NormalizedRequest, *, offered: list[str], stripped: list[str],
                   upstream: dict | None = None, extra_meta: dict | None = None) -> RequestOutcome:
        msgs = [m.to_dict() for m in req.messages]
        thread_id = "t_" + content_key({"system": req.system, "first": msgs[0] if msgs else None})[7:23]
        th = s.threads.get(thread_id)
        if th is None:
            th = s.threads[thread_id] = ThreadState()
            s.stats["threads"] += 1
        keys = [content_key(m) for m in msgs]
        k = 0
        while k < min(len(keys), len(th.msg_keys)) and keys[k] == th.msg_keys[k]:
            k += 1
        context: list[str] = []
        segments: list[int] = []
        compaction = False
        if k < len(th.msg_keys):
            # El agente compactó o editó el historial: el prefijo ya visto no coincide.
            compaction = True
            s.stats["context_reset"] += 1
            r = s.ledger.append("context_reset", Actor("agent", s.agent.get("name", "agent")),
                                {"from_index": k, "messages": msgs[k:]}, [],
                                {"thread_id": thread_id, "reason": "compaction", "from_index": k})
            context.append(r.hash)
            segments.append(-1)
        refs_all = [list(x) for x in th.msg_refs[:k]]
        unmatched: list[str] = []
        for idx in range(k, len(msgs)):
            refs_all.append(self._message(s, msgs[idx], thread_id, compaction, unmatched))
        for idx, refs in enumerate(refs_all):
            for h in refs:
                context.append(h)
                segments.append(idx)
        th.msg_keys, th.msg_refs = keys, refs_all
        if unmatched and s.policy.require_signed_prompts:
            s.stats["rejected_unsigned"] += 1
            return RequestOutcome(None, thread_id, unmatched, "unsigned_user_text", stripped)
        meta = {"thread_id": thread_id, "protocol": req.protocol, "tools_offered": offered,
                "tools_stripped": stripped, "stream": req.stream, "segments": segments,
                "upstream": _int_only(upstream or {}), **_int_only(extra_meta or {})}
        rec = s.ledger.append("llm_request", Actor("agent", s.agent.get("name", "agent")), req.payload(), context,
                              meta)
        s.stats["requests"] += 1
        return RequestOutcome(rec, thread_id, unmatched, None, stripped)

    def _message(self, s: Session, m: dict, thread_id: str, compaction: bool, unmatched: list[str]) -> list[str]:
        agent = Actor("agent", s.agent.get("name", "agent"))
        role = m["role"]
        if role == "system":
            s.stats["system_role"] += 1
            text = normalize_text("\n".join(b.get("text", "") for b in m["content"] if b.get("type") == "text"))
            r = s.ledger.append("user_text_unsigned", agent, {"text": text}, [],
                                {"reason": "system_role", "thread_id": thread_id})
            return [r.hash]
        if role == "assistant":
            key = content_key(m)
            if key in s.assistant_refs:
                return list(s.assistant_refs[key])
            s.stats["history_mismatch"] += 1
            r = s.ledger.append("context_reset", agent, {"message": m}, [],
                                {"thread_id": thread_id, "reason": "history_mismatch"})
            refs = [r.hash]
            for b in m["content"]:
                if b.get("type") == "tool_use" and b.get("id") in s.tool_calls:
                    refs.append(s.tool_calls[b["id"]].hash)
            return refs
        # rol usuario
        refs: list[str] = []
        blocks = m["content"]
        has_tool_result = any(b.get("type") == "tool_result" for b in blocks)
        has_signed = any(b.get("type") == "text" and find_prompt(b.get("text", ""), s.prompts) for b in blocks)
        user = Actor("user", s.user_id)
        for b in blocks:
            t = b.get("type")
            if t == "tool_result":
                call = s.tool_calls.get(b.get("tool_use_id", ""))
                origin = call.origin if call else "unknown"
                r = s.ledger.append("tool_result", Actor("tool", origin), b, [call.hash] if call else [],
                                    {"tool": call.name if call else None, "origin": origin,
                                     "call_entry": call.hash if call else None, "is_error": bool(b.get("is_error")),
                                     "tool_use_id": b.get("tool_use_id"), "thread_id": thread_id})
                refs.append(r.hash)
            elif t == "text":
                text = normalize_text(b.get("text", ""))
                if not text:
                    continue
                match = find_prompt(text, s.prompts)
                if match:
                    refs.append(match[1])
                    rest = remainder(text, match[0])
                    if rest:
                        s.stats["agent_injected"] += 1
                        r = s.ledger.append("user_text_unsigned", agent, {"text": rest}, [],
                                            {"reason": "agent_injected", "thread_id": thread_id})
                        refs.append(r.hash)
                    continue
                if looks_agent_injected(text) or has_signed or has_tool_result:
                    reason, actor = "agent_injected", agent
                    s.stats["agent_injected"] += 1
                elif compaction:
                    reason, actor = "compaction", agent
                else:
                    reason, actor = ("no_match" if s.prompts else "no_hook"), user
                    s.stats["unmatched_user_text"] += 1
                    unmatched.append(text)
                r = s.ledger.append("user_text_unsigned", actor, {"text": text}, [],
                                    {"reason": reason, "thread_id": thread_id})
                refs.append(r.hash)
            else:
                r = s.ledger.append("user_text_unsigned", user, {"block": b}, [],
                                    {"reason": "non_text_block", "thread_id": thread_id, "block_type": t})
                refs.append(r.hash)
        if not refs:   # mensaje vacío: igual necesita una entrada que lo cubra
            r = s.ledger.append("user_text_unsigned", agent, {"text": message_text(m)}, [],
                                {"reason": "empty", "thread_id": thread_id})
            refs.append(r.hash)
        return refs

    # ------------------------------------------------------------------ respuesta (§6.5 paso 7)
    def on_response(self, s: Session, req_entry: LedgerRecord, resp: NormalizedResponse) -> list[LedgerRecord]:
        thread_id = req_entry.meta.get("thread_id")
        model = Actor("model", f"upstream:{s.upstream}")
        out = [s.ledger.append("llm_response", model,
                               {"content": resp.content, "stop_reason": resp.stop_reason, "model": resp.model,
                                "usage": resp.usage},
                               [req_entry.hash],
                               {"thread_id": thread_id, "stop_reason": resp.stop_reason,
                                "model_reported": resp.model, "usage": _int_only(resp.usage)})]
        offered = set(req_entry.meta.get("tools_offered") or [])
        stripped = set(req_entry.meta.get("tools_stripped") or [])
        for tc in resp.tool_calls:
            origin, _ = self.adapter.tool_origin(tc.name)
            d = s.tools.decisions.get(tc.name)
            meta = {"tool": tc.name, "origin": origin, "manifest_hash": d.manifest_hash if d else None,
                    "thread_id": thread_id, "tool_use_id": tc.id}
            if tc.name in stripped or (d is not None and not d.accepted):
                meta["anomaly"] = "stripped_tool_called"
            elif tc.name not in offered or d is None:
                meta["anomaly"] = "unknown_tool_called"
            r = s.ledger.append("tool_call", model, tc.to_dict(), [req_entry.hash], meta)
            s.tool_calls[tc.id] = ToolCallInfo(r.hash, tc.name, origin, r.seq)
            out.append(r)
        s.assistant_refs[content_key(resp.as_message().to_dict())] = [r.hash for r in out]
        return out
