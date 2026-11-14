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

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from ..manifests.signer import tool_hash   # una sola implementación de `tool_hash` (§6.3), con y sin floats
from ..types import GateDecision, ToolDef
from .protocols.anthropic_messages import AnthropicMessagesAdapter


# Herramientas internas de OpenCode 2.3.0 (fixture en tests/fixtures/opencode/2.3.0/). `execute` es su Code Mode.
OPENCODE_BUILTINS = frozenset({"edit", "glob", "grep", "question", "read", "shell", "skill", "subagent", "webfetch",
                               "websearch", "write", "execute", "todowrite", "todoread", "task", "bash", "patch"})


def _opencode_name(s: str) -> str:
    """OpenCode reemplaza por `_` todo lo que no sea letra, número, `_` o `-` en el nombre del servidor."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def resolve_origin(agent: str | None, tool_name: str, known_servers: Iterable[str] = ()) -> tuple[str, str]:
    """`(origen, nombre original)` de una herramienta según cómo la nombra cada agente (§5.4, §17.3).

    - Claude Code: `mcp__<server>__<tool>`; todo lo demás es interno.
    - OpenCode: las MCP van como `<server>_<tool>`, sin prefijo. Se busca el servidor conocido (con manifiesto)
      más largo que sea prefijo del nombre. Si no hay ninguno y la herramienta no es una interna de OpenCode,
      **se trata como MCP de un servidor desconocido**, para que el gate la rechace como `unsigned` en vez de
      dejarla pasar como interna.
    """
    origin, original = AnthropicMessagesAdapter.tool_origin(tool_name)
    if origin.startswith("mcp:") or agent != "opencode":
        return origin, original
    best = ""
    for server in known_servers:
        prefix = _opencode_name(server) + "_"
        if tool_name.startswith(prefix) and len(tool_name) > len(prefix) and len(server) > len(best):
            best = server
    if best:
        return f"mcp:{best}", tool_name[len(_opencode_name(best)) + 1:]
    if tool_name in OPENCODE_BUILTINS:
        return origin, original
    server, sep, rest = tool_name.partition("_")
    return (f"mcp:{server}", rest) if sep and server and rest else (f"mcp:{tool_name}", tool_name)


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
    def __init__(self, gate: Gate | None, policy: Any, agent: str | None = None):
        self.gate = gate
        self.policy = policy
        self.agent = agent                    # nombre del agente de la sesión: decide cómo se leen los nombres

    def _known_servers(self) -> list[str]:
        ts = getattr(self.gate, "trust_store", None)
        return list(getattr(ts, "manifests", {}) or {})

    def origin_of(self, state: ToolState, tool_name: str) -> str:
        """Origen de una herramienta por su nombre visible: el de su decisión, si ya se evaluó en la sesión."""
        d = state.decisions.get(tool_name)
        return d.origin if d is not None else resolve_origin(self.agent, tool_name, self._known_servers())[0]

    def evaluate(self, state: ToolState, tools: list[ToolDef]) -> list[GateDecision]:
        out = []
        known = self._known_servers()
        for t in tools:
            origin, original = resolve_origin(self.agent, t.name, known)
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
