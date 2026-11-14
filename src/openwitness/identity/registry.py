"""KeyRegistry: user_id -> pubkey con revocación fechada (SPEC §3, §7.2). Provisional (Adrian); dueña: Juliana."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _parse(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class KeyRegistry:
    def __init__(self) -> None:
        self.users: dict[str, list[dict]] = {}

    def register(self, user_id: str, pubkey: str) -> None:
        self.users.setdefault(user_id, []).append({"pubkey": pubkey, "status": "active", "revoked_at": None})

    def revoke(self, user_id: str, at: datetime | str) -> None:
        for k in self.users.get(user_id, []):
            if k["status"] == "active":
                k["status"] = "revoked"
                k["revoked_at"] = _parse(at).isoformat()

    def is_valid(self, user_id: str, pubkey: str, at: datetime | str) -> bool:
        t = _parse(at)
        for k in self.users.get(user_id, []):
            if k["pubkey"] != pubkey:
                continue
            if k["status"] == "active":
                return True
            if k["revoked_at"] and t < _parse(k["revoked_at"]):
                return True
        return False

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.users, indent=1, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path | str) -> "KeyRegistry":
        r = cls()
        p = Path(path)
        if p.exists():
            r.users = json.loads(p.read_text())
        return r
