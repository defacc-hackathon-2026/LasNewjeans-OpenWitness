"""Tipos neutrales compartidos (SPEC §6, §7). Cambian solo con acuerdo de los tres (§11).

Convenciones:
- Lo que se firma o se guarda en disco se serializa con `to_dict()` / `from_dict()`.
- Los bloques de contenido (`Message.content`) son dicts con la forma normalizada de §6.1
  (`type`, `text`, `id`, `name`, `input`, `tool_use_id`, `content`, `is_error`, `thinking`, `tool_name`).
  Se dejan como dicts a propósito: así el adaptador de cada protocolo no tiene que modelar cada tipo de bloque.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------- protocolo neutral (gateway/protocols)

Role = Literal["user", "assistant", "system"]


@dataclass
class Message:
    role: Role
    content: list[dict]                    # bloques normalizados (§6.1)

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(d["role"], list(d["content"]))


@dataclass
class ToolDef:
    name: str                              # nombre tal como lo ve el modelo (p. ej. mcp__issues__list_issues)
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    def descriptor(self) -> dict:
        """Lo que cubre `tool_hash` (§6.3): {name, description, input_schema}."""
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def to_dict(self) -> dict:
        return self.descriptor()

    @classmethod
    def from_dict(cls, d: dict) -> "ToolDef":
        return cls(d.get("name", ""), d.get("description", "") or "", d.get("input_schema") or {})


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "args": self.args}


@dataclass
class NormalizedRequest:
    protocol: str                          # "anthropic_messages" | "openai_responses" | "openai_chat"
    system: str                            # texto del system prompt (concatenado)
    messages: list[Message]
    tools: list[ToolDef]
    model: str | None = None
    stream: bool = False

    def payload(self) -> dict:
        return {"system": self.system, "messages": [m.to_dict() for m in self.messages],
                "tools": [t.to_dict() for t in self.tools], "model": self.model}


@dataclass
class NormalizedResponse:
    content: list[dict]                    # bloques normalizados del asistente
    stop_reason: str | None = None
    model: str | None = None
    usage: dict = field(default_factory=dict)
    status: int = 200

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [ToolCall(b.get("id", ""), b.get("name", ""), b.get("input") or {})
                for b in self.content if b.get("type") == "tool_use"]

    def as_message(self) -> Message:
        return Message("assistant", self.content)


# ---------------------------------------------------------------- bitácora (§6.4, §6.6, §6.7)

ActorType = Literal["user", "agent", "model", "tool", "gateway", "hook"]


@dataclass
class Actor:
    type: ActorType
    id: str

    def to_dict(self) -> dict:
        return {"type": self.type, "id": self.id}


EntryKind = Literal["session_start", "manifest_decision", "user_prompt", "user_text_unsigned", "llm_request",
                    "llm_response", "tool_call", "tool_check", "tool_result", "context_reset", "anchor",
                    "session_end"]


@dataclass
class LedgerRecord:
    """Una línea de `data/ledger/<sid>.jsonl`: `{entry, hash, sig}` (§6.4)."""
    entry: dict
    hash: str
    sig: str

    @property
    def seq(self) -> int:
        return self.entry["seq"]

    @property
    def kind(self) -> str:
        return self.entry["kind"]

    @property
    def meta(self) -> dict:
        return self.entry.get("meta") or {}

    @property
    def context(self) -> list[str]:
        return self.entry.get("context") or []

    @property
    def payload_hash(self) -> str | None:
        return self.entry.get("payload_hash")

    def to_dict(self) -> dict:
        return {"entry": self.entry, "hash": self.hash, "sig": self.sig}

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerRecord":
        return cls(d["entry"], d["hash"], d["sig"])


@dataclass
class Receipt:
    """Recibo firmado por el gateway (§6.6). `body = {session_id, seq, entry_hash, prompt_hash}`."""
    body: dict
    sig: str
    pubkey: str

    def to_dict(self) -> dict:
        return {"body": self.body, "sig": self.sig, "pubkey": self.pubkey}

    @classmethod
    def from_dict(cls, d: dict) -> "Receipt":
        return cls(d["body"], d["sig"], d.get("pubkey", ""))


@dataclass
class AnchorReceipt:
    """Recibo del ancla (§6.7). `body = {index, session_id, head_hash, submitter, submitter_sig, integrated_time}`."""
    backend: str
    body: dict
    sig: str
    anchor_pubkey: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AnchorReceipt":
        return cls(d["backend"], d["body"], d["sig"], d["anchor_pubkey"])


@dataclass
class IntegrityError:
    seq: int | None                         # None = error de la sesión completa (p. ej. cabeza anclada ausente)
    error: str                              # hash_mismatch, bad_signature, seq_gap, broken_chain, ...
    detail: str = ""
    check: int = 0                          # número de la comprobación de §7.4 (1–10)

    def to_dict(self) -> dict:
        return asdict(self)


BlobStatus = Literal["present", "missing", "mismatch"]


@dataclass
class IntegrityReport:
    ok: bool
    session_id: str | None = None
    entries: int = 0
    last_anchored_seq: int = -1
    unanchored_tail: int = 0                # entradas (sin contar `anchor`) posteriores a la última cabeza anclada
    errors: list[IntegrityError] = field(default_factory=list)
    blob_status: dict[int, BlobStatus] = field(default_factory=dict)

    def first_error_seq(self) -> int | None:
        seqs = [e.seq for e in self.errors if e.seq is not None]
        if any(e.seq is None for e in self.errors):
            return 0                        # un error global invalida toda la sesión
        return min(seqs) if seqs else None

    def ok_up_to(self, seq: int) -> bool:
        """True si no hay errores en ninguna entrada ≤ seq (lo usa la atribución, §7.9 paso 1)."""
        f = self.first_error_seq()
        return f is None or f > seq

    def to_dict(self) -> dict:
        return {"ok": self.ok, "session_id": self.session_id, "entries": self.entries,
                "last_anchored_seq": self.last_anchored_seq, "unanchored_tail": self.unanchored_tail,
                "errors": [e.to_dict() for e in self.errors],
                "blob_status": {str(k): v for k, v in self.blob_status.items()}}


# ---------------------------------------------------------------- manifiestos (§6.3, §7.3)

GateReason = Literal["ok", "builtin", "unsigned", "bad_sig", "untrusted_key", "descriptor_mismatch", "rug_pull",
                     "shadowing", "unknown_builtin"]


@dataclass
class GateDecision:
    accepted: bool
    reason: GateReason
    tool: ToolDef
    origin: str                             # "mcp:<server>" | "builtin:<tool>"
    tool_hash: str | None = None            # hash del descriptor en vivo
    manifest_hash: str | None = None        # hash_obj(SignedManifest.body) que lo respalda; None si no hay

    def to_meta(self) -> dict:
        return {"tool": self.tool.name, "origin": self.origin, "decision": "accepted" if self.accepted else "rejected",
                "reason": self.reason, "tool_hash": self.tool_hash, "manifest_hash": self.manifest_hash}


# ---------------------------------------------------------------- atribución (§7.9)

VerdictLabel = Literal["USER_INSTRUCTED", "TOOL_INJECTED", "AMBIGUOUS", "MODEL_INITIATIVE", "UNVERIFIABLE"]
Provenance = str   # user_signed | user_unsigned | agent_injected | system | mcp:<s> | builtin:<t> | hosted:<t> | model | compaction


@dataclass
class EvidenceItem:
    seq: int
    kind: str
    hash: str
    fragment: str
    provenance: Provenance
    reason: str = ""


@dataclass
class Verdict:
    label: VerdictLabel
    source: str | None
    non_repudiable: bool
    confidence: float                       # solo en el reporte; nunca se firma
    method: Literal["rules", "judge", "rules+judge", "judge_failed"]
    evidence: list[EvidenceItem] = field(default_factory=list)
    judge: dict | None = None
    arg_provenance: dict[str, list[str]] = field(default_factory=dict)
    partial: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- formatos firmados (dicts, §6.2, §6.3)
# SignedPrompt   = {"body": {v, user_id, session_id, agent_session_id, nonce, ts, content, content_hash},
#                   "pubkey": "ed25519:...", "sig": "..."}
# SignedManifest = {"body": {v, server_id, server_version, publisher, tools: [{name, description, input_schema,
#                   tool_hash}], issued_at}, "pubkey": "ed25519:...", "sig": "..."}
SignedPrompt = dict
SignedManifest = dict
