"""Adaptadores de protocolo: normalizan peticiones y respuestas a los tipos neutrales de `types.py`."""
from .anthropic_messages import AnthropicMessagesAdapter, SSEReassembler
from .openai_responses import OpenAIResponsesAdapter, ResponsesSSEReassembler

ADAPTERS = {"anthropic_messages": AnthropicMessagesAdapter, "openai_responses": OpenAIResponsesAdapter}
# protocolo del gateway -> nombre corto que declara un perfil de upstream (`LLMProfile.protocols`, §7.7)
UPSTREAM_PROTOCOL = {"anthropic_messages": "messages", "openai_responses": "responses"}

__all__ = ["ADAPTERS", "UPSTREAM_PROTOCOL", "AnthropicMessagesAdapter", "OpenAIResponsesAdapter",
           "ResponsesSSEReassembler", "SSEReassembler"]
