"""Sesiones, tokens y política por sesión (SPEC §3, §7.5)."""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict, dataclass, field
from typing import Literal

from ..ledger import Ledger
from .tool_filter import ToolState


@dataclass
class Policy:
    require_signed_prompts: bool = False   # true: rechaza (HTTP 403) un turno con texto de usuario nuevo sin firma
    strip_untrusted_tools: bool = True     # quita de `tools` las herramientas rechazadas por ManifestGate
    deny_stripped_tool_calls: bool = True  # tool-check deniega herramientas quitadas o desconocidas
    unknown_builtin: Literal["allow_and_log", "deny"] = "allow_and_log"
    builtin_allowlist: list[str] | None = None   # None = cualquier herramienta interna se acepta como builtin

    @classmethod
    def from_dict(cls, d: dict | None) -> "Policy":
        d = dict(d or {})
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ThreadState:
    msg_keys: list[str] = field(default_factory=list)            # clave de contenido de cada mensaje ya visto
    msg_refs: list[list[str]] = field(default_factory=list)      # entradas que cubren cada mensaje


@dataclass
class ToolCallInfo:
    hash: str
    name: str
    origin: str
    seq: int


@dataclass
class Session:
    sid: str
    token: str
    user_id: str
    agent: dict
    ledger: Ledger
    policy: Policy
    upstream: str                          # nombre del perfil
    protocol: str = "anthropic_messages"   # anthropic_messages | openai_responses (§17.4)
    prompts: list[tuple[str, str]] = field(default_factory=list)      # (contenido normalizado, hash de la entrada)
    threads: dict[str, ThreadState] = field(default_factory=dict)
    tool_calls: dict[str, ToolCallInfo] = field(default_factory=dict)  # tool_use_id -> tool_call
    assistant_refs: dict[str, list[str]] = field(default_factory=dict) # clave del mensaje asistente -> entradas
    assistant_block_refs: dict[str, list[str]] = field(default_factory=dict)  # clave de cada bloque -> entradas
    tools: ToolState = field(default_factory=ToolState)
    seen_nonces: set[str] = field(default_factory=set)
    receipts: list[dict] = field(default_factory=list)
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stats: dict = field(default_factory=lambda: {"requests": 0, "history_mismatch": 0, "unmatched_user_text": 0,
                                                 "agent_injected": 0, "system_role": 0, "threads": 0,
                                                 "context_reset": 0, "rejected_unsigned": 0})


def new_session_id() -> str:
    return "s_" + secrets.token_hex(8)


def new_token() -> str:
    return "owit_" + secrets.token_urlsafe(24)
