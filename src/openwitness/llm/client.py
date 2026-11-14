"""`LLMClient` para el juez de atribución (SPEC §7.7). Dueño: Andres.

Llamada simple y síncrona con tipos neutrales:
- `openai_compat` (LM Studio, vLLM, APIs privadas): Chat Completions con `response_format` JSON schema si
  se pide salida estructurada;
- `anthropic` / `anthropic_compat`: Messages; el JSON se pide en el prompt (lo valida el juez);
- `fake`: respuestas fijas o un callable (tests).

Los secretos nunca salen en `describe()` ni en los mensajes de error.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..types import Message, ToolDef
from .base import LLMProfile
from .config import api_key, redact_headers


@dataclass
class LLMResponse:
    text: str
    model: str | None = None
    usage: dict = field(default_factory=dict)
    raw: Any = None


def _msg_text(m: Message) -> str:
    return "\n".join(b.get("text", "") for b in m.content if b.get("type") == "text")


class _Base:
    profile: LLMProfile

    def describe(self) -> dict:
        p = self.profile
        return {"profile": p.name, "provider": p.provider, "base_url": p.base_url, "model": p.model,
                "headers": redact_headers(p.headers)}


class OpenAIChatClient(_Base):
    def __init__(self, profile: LLMProfile, http: httpx.Client | None = None):
        self.profile = profile
        self.http = http or httpx.Client(timeout=httpx.Timeout(profile.timeout_s, connect=10),
                                         verify=profile.verify_tls)

    def chat(self, messages: list[Message], tools: list[ToolDef] | None = None,
             response_schema: dict | None = None) -> LLMResponse:
        base = (self.profile.base_url or "http://localhost:1234/v1").rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        body: dict[str, Any] = {"model": self.profile.model,
                                "messages": [{"role": m.role, "content": _msg_text(m)} for m in messages],
                                **self.profile.params}
        if response_schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "verdict", "strict": True, "schema": response_schema}}
        h = {"content-type": "application/json", **self.profile.headers}
        key = api_key(self.profile)
        if key:
            h["authorization"] = f"Bearer {key}"
        r = self.http.post(base + "/chat/completions", json=body, headers=h)
        if r.status_code != 200:
            raise RuntimeError(f"{self.profile.name}: HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        msg = (j.get("choices") or [{}])[0].get("message") or {}
        return LLMResponse(msg.get("content") or "", j.get("model"), j.get("usage") or {}, j)


class AnthropicClient(_Base):
    def __init__(self, profile: LLMProfile, http: httpx.Client | None = None):
        self.profile = profile
        self.http = http or httpx.Client(timeout=httpx.Timeout(profile.timeout_s, connect=10),
                                         verify=profile.verify_tls)

    def chat(self, messages: list[Message], tools: list[ToolDef] | None = None,
             response_schema: dict | None = None) -> LLMResponse:
        base = (self.profile.base_url or "https://api.anthropic.com").rstrip("/").removesuffix("/v1")
        system = "\n".join(_msg_text(m) for m in messages if m.role == "system")
        msgs = [{"role": m.role, "content": _msg_text(m)} for m in messages if m.role != "system"]
        body: dict[str, Any] = {"model": self.profile.model, "max_tokens": 2048, "messages": msgs,
                                **self.profile.params}
        if system:
            body["system"] = system
        h = {"content-type": "application/json", "anthropic-version": "2023-06-01", **self.profile.headers}
        key = api_key(self.profile)
        if key:
            h["x-api-key"] = key
        r = self.http.post(base + "/v1/messages", json=body, headers=h)
        if r.status_code != 200:
            raise RuntimeError(f"{self.profile.name}: HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        text = "\n".join(b.get("text", "") for b in j.get("content") or [] if b.get("type") == "text")
        return LLMResponse(text, j.get("model"), j.get("usage") or {}, j)


class FakeClient(_Base):
    def __init__(self, profile: LLMProfile | None = None, reply: str | Callable[[list[Message]], str] = "{}"):
        self.profile = profile or LLMProfile("fake", "fake", model="fake-judge")
        self.reply = reply

    def chat(self, messages: list[Message], tools: list[ToolDef] | None = None,
             response_schema: dict | None = None) -> LLMResponse:
        text = self.reply(messages) if callable(self.reply) else self.reply
        return LLMResponse(text, self.profile.model)


def make_client(profile: LLMProfile):
    if profile.provider == "openai_compat":
        return OpenAIChatClient(profile)
    if profile.provider in ("anthropic", "anthropic_compat"):
        return AnthropicClient(profile)
    if profile.provider == "fake":
        return FakeClient(profile)
    raise NotImplementedError(profile.provider)


def check_profile(profile: LLMProfile) -> dict:
    """`owit llm check`: conectividad, modelos, una llamada con JSON. No imprime secretos."""
    out: dict[str, Any] = {"profile": profile.name, "provider": profile.provider, "model": profile.model}
    if profile.provider == "fake":
        return out | {"ok": True}
    base = (profile.base_url or "").rstrip("/").removesuffix("/v1")
    h = dict(profile.headers)
    key = api_key(profile)
    if key:
        h["authorization"] = f"Bearer {key}"
        h["x-api-key"] = key
    try:
        r = httpx.get(base + "/v1/models", headers=h, timeout=15, verify=profile.verify_tls)
        out["models_status"] = r.status_code
        if r.status_code == 200:
            out["models"] = [m.get("id") for m in r.json().get("data", [])]
    except httpx.HTTPError as e:
        out["models_error"] = type(e).__name__
    try:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                  "additionalProperties": False}
        resp = make_client(profile).chat([Message("user", [{"type": "text", "text":
                                                            'Responde solo con el JSON {"ok": true}'}])],
                                         response_schema=schema)
        out["json_call"] = json.loads(resp.text.strip().strip("`").removeprefix("json").strip()).get("ok") is True
    except Exception as e:
        out["json_call"] = False
        out["json_error"] = str(e)[:200]
    out["ok"] = out.get("models_status") == 200 and out.get("json_call") is True
    return out
