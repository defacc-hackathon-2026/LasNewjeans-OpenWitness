"""Codex CLI (SPEC §17.4): flags de `owit exec -- codex` e instalación de los hooks.

Codex 0.169.0 (verificado con `bench/codex_probe.py`):
- con un `model_provider` propio y `wire_api = "responses"` llama a `POST <base_url>/responses` por HTTP con SSE,
  manda el historial completo con `store = false` y no usa `previous_response_id`;
- agrupa las herramientas MCP en `{"type": "namespace", "name": "mcp__<server>"}` sin tocar sus descripciones;
- los hooks (`[[hooks.<Evento>]]` en `config.toml`) reciben el mismo JSON que los de Claude Code, con
  `tool_name = "mcp__<server>__<tool>"` y `tool_use_id = call_id`; `exec_command` llega como `Bash`;
- los hooks nuevos **no corren hasta que se aprueban** una vez con `/hooks` en la TUI (se guarda un
  `trusted_hash` en `config.toml`). `codex exec --dangerously-bypass-hook-trust` los corre sin aprobar: solo
  para automatización que ya revisó los hooks (los probes de este repo).
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path

PROVIDER = "owit"
EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "prompt", "PreToolUse": "pre-tool"}
BEGIN, END = "# >>> Open Witness hooks (owit hooks install codex) >>>", "# <<< Open Witness hooks <<<"


def exec_args(gateway: str, sid: str) -> list[str]:
    """Flags globales que `owit exec` agrega después de `codex`: el proveedor apunta a la sesión del gateway.

    La API key es el token de sesión (`OWIT_TOKEN`); las credenciales de OpenAI y de Codex no pasan al
    proceso (`_SECRET_ENV` en `integrations/exec.py`). `daemon_auto_start` se apaga para que ninguna petición
    salga de un proceso que no heredó el entorno de la sesión.
    """
    p = f"model_providers.{PROVIDER}"
    return ["-c", f'model_provider="{PROVIDER}"',
            "-c", f'{p}.name="Open Witness"',
            "-c", f'{p}.base_url="{gateway.rstrip("/")}/s/{sid}/v1"',
            "-c", f'{p}.env_key="OWIT_TOKEN"',
            "-c", f'{p}.wire_api="responses"',
            "--disable", "daemon_auto_start"]


def hooks_block(owit: str = "owit") -> str:
    """El bloque TOML con los tres hooks; va entre marcadores para poder reemplazarlo sin tocar el resto."""
    lines = [BEGIN, "# Aprobarlos una vez con /hooks en la TUI de Codex; sin OWIT_* en el entorno no hacen nada."]
    for event, sub in EVENTS.items():
        lines.append(f"[[hooks.{event}]]")
        if event == "PreToolUse":
            lines.append('matcher = "*"')
        lines += [f"[[hooks.{event}.hooks]]", 'type = "command"', f'command = "{owit} hook {sub}"', ""]
    lines[-1] = END
    return "\n".join(lines) + "\n"


def merge(text: str, owit: str = "owit") -> str:
    """`text` (un config.toml) con el bloque de Open Witness al final; reemplaza uno anterior."""
    if BEGIN in text and END in text:
        head, _, rest = text.partition(BEGIN)
        _, _, tail = rest.partition(END)
        text = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
    sep = "" if not text.strip() else ("\n" if text.endswith("\n") else "\n\n")
    return (text + sep if text.strip() else "") + hooks_block(owit)


def config_path(project: Path | None = None, codex_home: str | None = None) -> Path:
    """`<proyecto>/.codex/config.toml` o, sin proyecto, `$CODEX_HOME/config.toml` (por defecto `~/.codex`)."""
    if project is not None:
        return Path(project) / ".codex" / "config.toml"
    return Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def plan_install(path: Path, owit: str = "owit") -> tuple[Path, str, str]:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = merge(old, owit)
    import tomllib
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path} no quedaría como TOML válido: {e}") from e
    return path, old, new


def diff(path: Path, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"{path} (actual)", tofile=f"{path} (nuevo)"))


def write(path: Path, new: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
