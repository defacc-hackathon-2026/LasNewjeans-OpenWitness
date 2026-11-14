"""Carga de `owit.toml`, precedencia y redacción de secretos (SPEC §7.7).

Dueño: Andres (lado del juez). Versión mínima (Adrian) para que el gateway pueda arrancar; respeta el contrato
`load_profiles(path) -> dict[str, LLMProfile]`.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from .base import LLMProfile

DEFAULT_PROFILES = {
    "fake": LLMProfile("fake", "fake", model="fake-model"),
    "lmstudio": LLMProfile("lmstudio", "anthropic_compat", model="openai/gpt-oss-20b",
                           base_url="http://localhost:1234", api_key_env="", timeout_s=300),
    "claude": LLMProfile("claude", "anthropic", model="claude-sonnet-5-5", base_url="https://api.anthropic.com",
                         api_key_env="ANTHROPIC_API_KEY", timeout_s=300),
}

SECRET_HEADER_HINTS = ("key", "token", "secret", "authorization", "auth")


def load_config(path: Path | str = "owit.toml") -> dict[str, Any]:
    p = Path(path)
    return tomllib.loads(p.read_text()) if p.exists() else {}


def load_profiles(path: Path | str = Path("owit.toml")) -> dict[str, LLMProfile]:
    profiles = dict(DEFAULT_PROFILES)
    cfg = load_config(path)
    fields = LLMProfile.__dataclass_fields__
    for name, d in (cfg.get("llm", {}).get("profiles", {}) or {}).items():
        kw = {k: v for k, v in d.items() if k in fields and k != "name"}
        profiles[name] = LLMProfile(name=name, **kw)
    return profiles


def resolve(cli_value: str | None, env_var: str, cfg_value: str | None, default: str) -> str:
    """Precedencia de §7.7: flag del CLI, luego variable de entorno, luego `owit.toml`."""
    return cli_value or os.environ.get(env_var) or cfg_value or default


def api_key(profile: LLMProfile) -> str | None:
    return os.environ.get(profile.api_key_env) if profile.api_key_env else None


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if any(h in k.lower() for h in SECRET_HEADER_HINTS) else v) for k, v in headers.items()}


def make_upstream(profile: LLMProfile):
    if profile.provider in ("anthropic", "anthropic_compat"):
        from .anthropic_upstream import AnthropicUpstream
        return AnthropicUpstream(profile)
    if profile.provider == "fake":
        from .fake import FakeUpstream
        return FakeUpstream(profile)
    raise NotImplementedError(f"upstream {profile.provider!r} todavía no tiene adaptador (roadmap §16)")
