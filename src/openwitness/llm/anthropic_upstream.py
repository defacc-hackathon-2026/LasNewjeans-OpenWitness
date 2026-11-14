"""Upstream con protocolo Anthropic Messages: Claude (`anthropic`) y LM Studio/Ollama (`anthropic_compat`)."""
from __future__ import annotations

import json
import warnings
from urllib.parse import urlparse

import httpx

from .base import LLMProfile, UpstreamResponse
from .config import api_key, redact_headers

FORWARD_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept")


class AnthropicUpstream:
    def __init__(self, profile: LLMProfile, client: httpx.AsyncClient | None = None):
        self.profile = profile
        base = (profile.base_url or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]           # el cliente agrega /v1/messages (LM Studio: base_url sin /v1)
        self.base_url = base
        u = urlparse(base)
        if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
            warnings.warn(f"el perfil {profile.name} usa {base} sin https", stacklevel=2)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(profile.timeout_s, connect=10),
                                                  verify=profile.verify_tls)

    def _headers(self, incoming: dict[str, str]) -> dict[str, str]:
        h = {k: v for k, v in incoming.items() if k.lower() in FORWARD_HEADERS}
        h.setdefault("content-type", "application/json")
        h.setdefault("anthropic-version", "2023-06-01")
        h.update(self.profile.headers)
        key = api_key(self.profile)
        if key:
            h["x-api-key"] = key
        return h

    def prepare(self, body: dict) -> dict:
        if self.profile.forces_model() and self.profile.model:
            body = dict(body, model=self.profile.model)
        return body

    async def forward(self, protocol: str, body: dict, headers: dict[str, str], query: str = "") -> UpstreamResponse:
        if protocol != "anthropic_messages":
            raise ValueError(f"{self.profile.name} no habla {protocol}")
        url = self.base_url + "/v1/messages" + (f"?{query}" if query else "")
        req = self.client.build_request("POST", url, content=json.dumps(body, ensure_ascii=False).encode(),
                                        headers=self._headers(headers))
        r = await self.client.send(req, stream=True)
        return UpstreamResponse(r.status_code, dict(r.headers), r.aiter_raw(), r.aclose)

    async def passthrough(self, method: str, path: str, body: bytes, headers: dict[str, str],
                          query: str = "") -> UpstreamResponse:
        url = self.base_url + path + (f"?{query}" if query else "")
        req = self.client.build_request(method, url, content=body or None, headers=self._headers(headers))
        r = await self.client.send(req, stream=True)
        return UpstreamResponse(r.status_code, dict(r.headers), r.aiter_raw(), r.aclose)

    def describe(self) -> dict:
        return {"profile": self.profile.name, "provider": self.profile.provider, "base_url": self.base_url,
                "model_requested": self.profile.model, "headers": redact_headers(self.profile.headers)}
