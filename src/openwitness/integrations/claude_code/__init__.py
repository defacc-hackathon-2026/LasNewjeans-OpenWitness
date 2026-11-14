"""Hooks de Claude Code (SPEC §5.3, §7.6): plantilla e instalación en la configuración local del proyecto.

`owit hooks install claude-code` escribe en `<proyecto>/.claude/settings.local.json` (no se comparte por git)
los hooks `SessionStart`, `UserPromptSubmit` y `PreToolUse`. Conserva el resto de la configuración y no
duplica los hooks si ya estaban.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path

SETTINGS = Path(".claude") / "settings.local.json"
EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "prompt", "PreToolUse": "pre-tool"}


def hooks_config(owit: str = "owit") -> dict:
    """La sección `hooks` que necesita Open Witness. `owit` es el comando (mejor con ruta absoluta)."""
    out = {}
    for event, sub in EVENTS.items():
        group: dict = {"hooks": [{"type": "command", "command": f"{owit} hook {sub}"}]}
        if event == "PreToolUse":
            group = {"matcher": "*", **group}           # todas las herramientas, internas y MCP
        out[event] = [group]
    return out


def _is_ours(group: dict, sub: str) -> bool:
    return any(str(h.get("command", "")).endswith(f" hook {sub}") for h in group.get("hooks", []) if isinstance(h, dict))


def merge(settings: dict, owit: str = "owit") -> dict:
    """Copia de `settings` con los hooks de Open Witness; reemplaza los nuestros y respeta los ajenos."""
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("`hooks` no es un objeto en la configuración existente")
    for event, groups in hooks_config(owit).items():
        current = [g for g in hooks.get(event, []) if not (isinstance(g, dict) and _is_ours(g, EVENTS[event]))]
        hooks[event] = current + groups
    return out


def plan_install(project: Path, owit: str = "owit") -> tuple[Path, str, str]:
    """Devuelve `(ruta, texto actual, texto nuevo)` sin escribir nada."""
    path = Path(project) / SETTINGS
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        settings = json.loads(old) if old.strip() else {}
    except ValueError as e:
        raise ValueError(f"{path} no es JSON válido: {e}") from e
    if not isinstance(settings, dict):
        raise ValueError(f"{path} no contiene un objeto JSON")
    new = json.dumps(merge(settings, owit), indent=2, ensure_ascii=False) + "\n"
    return path, old, new


def diff(path: Path, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"{path} (actual)", tofile=f"{path} (nuevo)"))


def write(path: Path, new: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
