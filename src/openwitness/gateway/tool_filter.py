"""ToolFilter: `ManifestGate` aplicado a la lista `tools` de cada petición (SPEC §6.3, §6.5 paso 5).

Dueña: Juliana. Versión provisional (Adrian) que fija el contrato que usa el `Decomposer`:

    ToolFilter(gate, policy).evaluate(state, tools) -> list[GateDecision]   # una por herramienta, en orden

- `gate` es cualquier objeto con `evaluate_tool(server_id, live: ToolDef, seen_owner: dict) -> GateDecision`
  (el `ManifestGate` de §7.3). `live.name` va con el nombre original del servidor, no con `mcp__s__t`.
- Si `gate` es None (solo desarrollo), las herramientas MCP se aceptan con `reason="unsigned"`: quedan
  registradas como no verificadas, nunca como "ok".
- El registro de `manifest_decision`, el quitado de `tools` y el reemplazo de `tool_reference` los hace el
  gateway con estas decisiones; así el filtro no escribe en la bitácora.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..canonical import CanonicalError, hash_obj, sha256_tagged
from ..ledger.blobs import encode_payload
from ..types import GateDecision, ToolDef
from .protocols.anthropic_messages import AnthropicMessagesAdapter


def tool_hash(t: ToolDef) -> str:
    """`tool_hash` de §6.3. Si el esquema trae floats (p. ej. `minimum: 0.5`) se usa el hash de los bytes."""
    try:
        return hash_obj(t.descriptor())
    except CanonicalError:
        return sha256_tagged(encode_payload(t.descriptor()))


class Gate(Protocol):
    def evaluate_tool(self, server_id: str, live: ToolDef, seen_owner: dict[str, str]) -> GateDecision: ...


@dataclass
class ToolState:
    """Estado por sesión del filtrado."""
    decisions: dict[str, GateDecision] = field(default_factory=dict)   # nombre visto por el modelo -> decisión
    recorded: dict[str, str] = field(default_factory=dict)             # nombre -> tool_hash ya registrado
    seen_owner: dict[str, str] = field(default_factory=dict)           # nombre de herramienta -> server_id

    @property
    def rejected(self) -> set[str]:
        return {n for n, d in self.decisions.items() if not d.accepted}


class ToolFilter:
    def __init__(self, gate: Gate | None, policy: Any):
        self.gate = gate
        self.policy = policy

    def evaluate(self, state: ToolState, tools: list[ToolDef]) -> list[GateDecision]:
        out = []
        for t in tools:
            origin, original = AnthropicMessagesAdapter.tool_origin(t.name)
            if origin.startswith("mcp:"):
                server = origin.removeprefix("mcp:")
                live = ToolDef(original, t.description, t.input_schema)
                if self.gate is None:
                    d = GateDecision(True, "unsigned", t, origin, tool_hash(live), None)
                else:
                    d = self.gate.evaluate_tool(server, live, state.seen_owner)
                    d = GateDecision(d.accepted, d.reason, t, origin, d.tool_hash or tool_hash(live), d.manifest_hash)
            else:
                allow = self.policy.builtin_allowlist
                if allow is None or t.name in allow:
                    d = GateDecision(True, "builtin", t, origin, tool_hash(t), None)
                else:
                    d = GateDecision(self.policy.unknown_builtin != "deny", "unknown_builtin", t, origin,
                                     tool_hash(t), None)
            state.decisions[t.name] = d
            out.append(d)
        return out
