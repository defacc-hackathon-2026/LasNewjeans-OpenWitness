"""Agente de referencia para el benchmark y la demo (SPEC §7.8). Dueño: Andres."""
from .agent import AgentRun, RefAgent
from .tools import TOOLS, InProcessToolbox, ToolContext, ToolSpec

__all__ = ["AgentRun", "InProcessToolbox", "RefAgent", "TOOLS", "ToolContext", "ToolSpec"]
