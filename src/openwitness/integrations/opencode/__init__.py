"""OpenCode a través del gateway (SPEC §5.4, §7.6, §17.3). Dueña: Juliana.

OpenCode habla Anthropic Messages con su proveedor `anthropic`, así que usa la ruta `/s/<sid>/v1/messages` del
gateway sin cambios. `owit exec -- opencode` le pasa esta configuración por `OPENCODE_CONFIG_CONTENT`:

- el proveedor `anthropic` redirigido a `http://<gateway>/s/<sid>/v1`, con el token de sesión como API key;
- un modelo de ese proveedor por defecto (si el usuario tiene otro proveedor por defecto, las peticiones no
  pasarían por el gateway);
- el plugin `owit-opencode-plugin/`, que reenvía el prompt y cada herramienta a `owit hook …`.

Verificado con OpenCode 2.3.0 (`bench/opencode_probe.py`):
- hace falta `--standalone`: sin él, el prompt se va por el servicio en segundo plano, que no hereda el entorno;
- las herramientas MCP llegan al modelo como `<server>_<tool>`, con la descripción y el esquema sin tocar,
  **solo si el servidor se declara con `codemode: false`**. Con Code Mode (el valor por defecto), OpenCode las
  esconde detrás de su herramienta `execute` y el gateway no puede filtrarlas.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Mapping

PLUGIN_DIR = Path(__file__).parent / "owit-opencode-plugin"
DEFAULT_MODEL = "anthropic/claude-sonnet-5-5"


def exec_config(gateway: str, sid: str, token: str, model: str | None = None, existing: str | None = None,
                plugin: bool = True) -> dict:
    """La configuración que `owit exec` inyecta. Conserva lo que ya viniera en `OPENCODE_CONFIG_CONTENT`."""
    try:
        cfg = json.loads(existing) if existing else {}
    except ValueError:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("$schema", "https://opencode.ai/config.json")
    cfg["model"] = model or DEFAULT_MODEL
    providers = cfg.setdefault("providers", {})
    anthropic = providers.setdefault("anthropic", {})
    anthropic["settings"] = {**anthropic.get("settings", {}), "baseURL": f"{gateway}/s/{sid}/v1", "apiKey": token}
    if plugin:
        plugins = [p for p in cfg.get("plugins", []) if not (isinstance(p, dict) and p.get("package") == str(PLUGIN_DIR))]
        cfg["plugins"] = plugins + [{"package": str(PLUGIN_DIR)}]
    return cfg


def hook_command() -> list[str]:
    """Cómo invoca el plugin a `owit`: el mismo intérprete que corre `owit exec`, sin depender del PATH."""
    return [sys.executable, "-m", "openwitness.entry"]


def agent_env(env: dict[str, str], base: Mapping[str, str], gateway: str, sid: str, token: str,
              model: str | None = None) -> dict[str, str]:
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(exec_config(gateway, sid, token, model,
                                                            base.get("OPENCODE_CONFIG_CONTENT")))
    env["OWIT_HOOK_CMD"] = json.dumps(hook_command())
    return env


def with_standalone(cmd: list[str]) -> list[str]:
    """Agrega `--standalone` si falta (§17.3): servidor privado del proceso, que sí hereda el entorno."""
    if "--standalone" in cmd or "--server" in cmd:
        return list(cmd)
    return [cmd[0], "--standalone", *cmd[1:]]
