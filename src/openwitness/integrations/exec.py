"""`owit exec --user <u> -- <agente>` (SPEC §5.3, §7.6). Dueña: Juliana.

Crea la sesión en el gateway, lanza el agente apuntando a ella y la cierra al terminar. El agente recibe solo
el token de sesión: las variables `ANTHROPIC_*` y `CLAUDE_CODE_OAUTH_TOKEN` del usuario no pasan al proceso.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from .hooks import GatewayError, http_post, save_receipt

DEFAULT_GATEWAY = "http://127.0.0.1:8787"
AGENTS = {"claude": "claude-code", "opencode": "opencode", "opencode-cli": "opencode",
          "codex": "codex"}                            # comando -> agente
_SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
               # Codex (§17.4): ni la API key de OpenAI ni otra base URL llegan al agente
               "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID")
AGENT_PROTOCOL = {"codex": "openai_responses"}        # el resto: anthropic_messages


class ExecError(Exception):
    pass


def detect_agent(cmd: list[str]) -> str:
    return AGENTS.get(Path(cmd[0]).name, Path(cmd[0]).name) if cmd else "unknown"


def agent_version(cmd: list[str]) -> str:
    """`<agente> --version`, si responde rápido; "unknown" si no. Queda en `session_start` (L10)."""
    try:
        p = subprocess.run([cmd[0], "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        m = re.search(r"\d+(?:\.\d+)+", p.stdout or "")
        return m.group(0) if m else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def agent_env(base: Mapping[str, str], agent: str, gateway: str, sid: str, token: str, user: str, key: Path,
              strict: bool) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in _SECRET_ENV}
    env.update({"OWIT_GATEWAY": gateway, "OWIT_SESSION": sid, "OWIT_TOKEN": token, "OWIT_USER": user,
                "OWIT_USER_KEY": str(Path(key).resolve())})
    if strict:
        env["OWIT_STRICT"] = "1"
    else:
        env.pop("OWIT_STRICT", None)
    if agent == "claude-code":
        env.update({"ANTHROPIC_BASE_URL": f"{gateway}/s/{sid}", "ANTHROPIC_AUTH_TOKEN": token,
                    "ENABLE_TOOL_SEARCH": "false"})      # todas las herramientas en `tools` desde la 1.ª petición
    elif agent == "opencode":                            # proveedor `anthropic` redirigido + plugin (§17.3)
        from . import opencode
        opencode.agent_env(env, base, gateway, sid, token)
    return env


def run_exec(cmd: list[str], user: str, key: Path, gateway: str = DEFAULT_GATEWAY, agent: str | None = None,
             upstream: str | None = None, strict: bool = False, prompt: str | None = None,
             env: Mapping[str, str] | None = None, version: str | None = None) -> int:
    """Devuelve el código de salida del agente. Lanza `ExecError` si no se pudo preparar la sesión."""
    if not cmd:
        raise ExecError("falta el comando del agente después de `--`")
    key = Path(key)
    if not key.exists():
        raise ExecError(f"no existe la llave {key}; créala con `owit keygen --out {key} --register {user}`")
    gateway = gateway.rstrip("/")
    agent = agent or detect_agent(cmd)
    body: dict = {"user_id": user, "agent": {"name": agent, "version": version or agent_version(cmd)}}
    if agent in AGENT_PROTOCOL:
        body["protocol"] = AGENT_PROTOCOL[agent]
    if upstream:
        body["upstream"] = upstream
    if strict:
        body["policy"] = {"require_signed_prompts": True}
    try:
        status, j = http_post(f"{gateway}/sessions", "", body)
    except GatewayError as e:
        raise ExecError(f"el gateway {gateway} no responde ({e}); ¿corre `owit gateway start`?") from e
    if status != 200 or not isinstance(j, dict) or "sid" not in j:
        raise ExecError(f"el gateway no creó la sesión ({status}: {j})")
    sid, token = j["sid"], j["token"]
    if agent == "codex":                                # el proveedor de Codex apunta a la sesión (§17.4)
        from .codex import exec_args
        cmd = [cmd[0], *exec_args(gateway, sid), *cmd[1:]]
    child_env = agent_env(os.environ if env is None else env, agent, gateway, sid, token, user, key, strict)
    print(f"Open Witness: sesión {sid} · usuario {user} · agente {agent} · upstream {j.get('upstream')}",
          file=sys.stderr)
    code, ledger = 1, None
    if agent == "opencode":
        from .opencode import with_standalone
        cmd = with_standalone(cmd)                      # sin esto, el prompt se va por el servicio en segundo plano
    try:
        if prompt is not None:                          # agentes sin hooks: se firma antes de lanzar (§7.6)
            from ..identity import KeyPair, sign_prompt
            sp = sign_prompt(KeyPair.load(key), user, sid, prompt)
            st, receipt = http_post(f"{gateway}/s/{sid}/prompts", token, sp)
            if st != 200:
                raise ExecError(f"el gateway rechazó el prompt firmado ({receipt})")
            save_receipt(receipt, child_env)
            cmd = [*cmd, prompt]
        try:
            code = subprocess.call(cmd, env=child_env)   # stdin, stdout y stderr heredados
        except FileNotFoundError as e:
            raise ExecError(f"no se encontró el comando {cmd[0]!r}") from e
        except KeyboardInterrupt:
            code = 130
    finally:
        try:
            _, end = http_post(f"{gateway}/s/{sid}/end", token, {"reason": "exec_exit"})
            ledger = end.get("ledger") if isinstance(end, dict) else None
        except GatewayError:
            print("Open Witness: no se pudo cerrar la sesión en el gateway.", file=sys.stderr)
    if ledger:
        print(f"Open Witness: bitácora en {ledger} · verifícala con `owit verify {ledger}`", file=sys.stderr)
    return code
