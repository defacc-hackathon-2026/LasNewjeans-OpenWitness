"""Adaptadores de protocolo: normalizan peticiones y respuestas a los tipos neutrales de `types.py`."""
from .anthropic_messages import AnthropicMessagesAdapter, SSEReassembler

__all__ = ["AnthropicMessagesAdapter", "SSEReassembler"]
