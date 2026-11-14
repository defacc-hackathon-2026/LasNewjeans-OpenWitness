"""Contratos de §7.7: `LLMProfile`, `Upstream` y `LLMClient`."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

Provider = Literal["anthropic", "anthropic_compat", "openai_compat", "fake"]
NATIVE_PROTOCOLS = {"anthropic": ["messages"], "anthropic_compat": ["messages"], "openai_compat": ["chat", "responses"],
                    "fake": ["messages", "chat", "responses"]}


@dataclass
class LLMProfile:
    name: str
    provider: Provider
    model: str = ""
    protocols: list[Literal["messages", "chat", "responses"]] = field(default_factory=list)
    base_url: str | None = None
    api_key_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    tool_calling: Literal["native", "prompted", "auto"] = "auto"
    timeout_s: int = 60
    verify_tls: bool | str = True
    force_model: bool | None = None      # None = True para anthropic_compat (LM Studio no conoce los modelos Claude)
    script: str | None = None            # solo provider = "fake"

    def effective_protocols(self) -> list[str]:
        return list(self.protocols) or NATIVE_PROTOCOLS[self.provider]

    def forces_model(self) -> bool:
        return self.force_model if self.force_model is not None else self.provider == "anthropic_compat"


@dataclass
class UpstreamResponse:
    """Respuesta del upstream: estado, cabeceras y el cuerpo como iterador de bytes (sin bufferizar)."""
    status: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]
    close: Any = None                    # corrutina opcional para liberar la conexión

    async def aclose(self) -> None:
        if self.close is not None:
            await self.close()


class Upstream(Protocol):
    """Reenvío casi transparente. Cambio respecto de §7.7: `forward` devuelve también el estado HTTP."""
    profile: LLMProfile

    async def forward(self, protocol: str, body: dict, headers: dict[str, str],
                      query: str = "") -> UpstreamResponse: ...

    async def passthrough(self, method: str, path: str, body: bytes, headers: dict[str, str],
                          query: str = "") -> UpstreamResponse: ...

    def describe(self) -> dict: ...
