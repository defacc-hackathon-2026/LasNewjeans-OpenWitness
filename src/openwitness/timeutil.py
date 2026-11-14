"""Tiempo RFC 3339 en UTC (SPEC §6.1)."""
from __future__ import annotations

from datetime import UTC, datetime


def now_rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
