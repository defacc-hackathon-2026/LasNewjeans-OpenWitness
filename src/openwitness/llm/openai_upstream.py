"""Upstream `openai_compat` con la Responses API: LM Studio, Ollama, vLLM u OpenAI (SPEC §7.7, §17.4).

Igual que `anthropic_upstream.py`: reenvío casi transparente con streaming (`httpx`), sin bufferizar. Codex habla
Responses de forma nativa, así que con LM Studio (`/v1/responses`) no hace falta traducir nada.
"""
from __future__ import annotations

import json
import warnings
from urllib.parse import urlparse

import httpx

from .base import LLMProfile, UpstreamResponse
from .config import api_key, redact_headers

FORWARD_HEADERS = ("content-type", "accept", "openai-beta", "openai-organization", "openai-project")
PATHS = {"openai_responses": "/v1/responses", "openai_chat": "/v1/chat/completions"}


class OpenAIUpstream:
    def __init__(self, profile: LLMProfile, client: httpx.AsyncClient | None = None):
        self.profile = profile
        base = (profile.base_url or "https://api.openai.com").rstrip("/")
        base = base.removesuffix("/v1")           # se agrega /v1/responses (LM Studio: http://localhost:1234/v1)
        self.base_url = base
        u = urlparse(base)
        if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
            warnings.warn(f"el perfil {profile.name} usa {base} sin https", stacklevel=2)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(profile.timeout_s, connect=10),
                                                  verify=profile.verify_tls)

    def _headers(self, incoming: dict[str, str]) -> dict[str, str]:
        h = {k: v for k, v in incoming.items() if k.lower() in FORWARD_HEADERS}
        h.setdefault("content-type", "application/json")
        h.update(self.profile.headers)
        key = api_key(self.profile)
        if key:
            h["authorization"] = f"Bearer {key}"   # nunca el token de sesión del agente
        return h

    def prepare(self, body: dict) -> dict:
        if self.profile.forces_model() and self.profile.model:
            body = dict(body, model=self.profile.model)
        return body

    async def forward(self, protocol: str, body: dict, headers: dict[str, str], query: str = "") -> UpstreamResponse:
        if protocol not in PATHS:
            raise ValueError(f"{self.profile.name} no habla {protocol}")
        url = self.base_url + PATHS[protocol] + (f"?{query}" if query else "")
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
