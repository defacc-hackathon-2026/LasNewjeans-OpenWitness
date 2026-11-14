"""`owit hook prompt | pre-tool | session-start` (SPEC §5.3, §7.6). Dueña: Juliana.

Los invoca el agente (Claude Code; Codex usa el mismo formato), no el usuario. Leen el JSON del hook por
stdin y hablan con el gateway usando las variables que exporta `owit exec`:

    OWIT_GATEWAY   URL del gateway              OWIT_SESSION   sid de la sesión del gateway
    OWIT_TOKEN     token de la sesión           OWIT_USER      user_id
    OWIT_USER_KEY  ruta de la llave privada     OWIT_STRICT    "1": si no se puede firmar o consultar, se bloquea
    OWIT_HOME      por defecto ~/.owit (recibos en $OWIT_HOME/receipts)

Reglas que vienen de Claude Code:
- Con `exit 0`, **stdout se agrega al contexto del modelo**. Por eso `prompt` y `session-start` nunca
  escriben en stdout; los avisos van a stderr.
- `exit 2` bloquea (el prompt o la herramienta) y muestra stderr.
- `pre-tool` deniega con el JSON `hookSpecificOutput.permissionDecision = "deny"`.

Si faltan las variables `OWIT_*` (el agente no se lanzó con `owit exec`), los hooks no hacen nada.

Solo usa la librería estándar para hablar con el gateway: cada invocación arranca un proceso de Python.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

TIMEOUT_S = 10.0
Post = Callable[[str, str, Any], tuple[int, Any]]


@dataclass
class HookResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class GatewayError(Exception):
    pass


def http_post(url: str, token: str, obj: Any) -> tuple[int, Any]:
    """POST JSON al gateway. Devuelve `(estado, cuerpo)`; lanza `GatewayError` si no hay respuesta."""
    req = urllib.request.Request(url, data=json.dumps(obj).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json", "x-api-key": token})
    # El gateway es local: no se usa el proxy del sistema aunque haya variables HTTP(S)_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT_S) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GatewayError(str(e)) from e


def owit_home(env: Mapping[str, str]) -> Path:
    return Path(env.get("OWIT_HOME") or Path.home() / ".owit")


def _strict(env: Mapping[str, str]) -> bool:
    return env.get("OWIT_STRICT", "").lower() in ("1", "true", "yes")


def _session(env: Mapping[str, str]) -> tuple[str, str, str] | None:
    gw, sid, token = env.get("OWIT_GATEWAY"), env.get("OWIT_SESSION"), env.get("OWIT_TOKEN")
    return (gw.rstrip("/"), sid, token) if gw and sid and token else None


def _payload(stdin_text: str) -> dict:
    try:
        j = json.loads(stdin_text or "{}")
    except ValueError:
        return {}
    return j if isinstance(j, dict) else {}


# ---------------------------------------------------------------- UserPromptSubmit

def hook_prompt(stdin_text: str, env: Mapping[str, str], post: Post = http_post) -> HookResult:
    """Firma el prompt, lo envía a `/s/<sid>/prompts` y guarda el recibo. Nunca escribe en stdout."""
    sess = _session(env)
    if sess is None:
        return HookResult()
    gw, sid, token = sess
    strict = _strict(env)

    def fail(msg: str) -> HookResult:
        if strict:
            return HookResult(2, "", f"Open Witness: {msg}; el prompt no se envió (modo estricto).\n")
        return HookResult(0, "", f"Open Witness: {msg}; el prompt queda sin firma.\n")

    j = _payload(stdin_text)
    prompt = j.get("prompt")
    if not isinstance(prompt, str):
        return fail("el hook no recibió el campo `prompt`")
    user, key_path = env.get("OWIT_USER"), env.get("OWIT_USER_KEY")
    if not user or not key_path:
        return fail("faltan OWIT_USER u OWIT_USER_KEY")
    try:
        from ..identity import KeyPair, sign_prompt
        kp = KeyPair.load(key_path)
    except Exception as e:  # noqa: BLE001 - llave ausente o ilegible
        return fail(f"no se pudo leer la llave {key_path} ({type(e).__name__})")
    agent_sid = j.get("session_id") if isinstance(j.get("session_id"), str) else None
    sp = sign_prompt(kp, user, sid, prompt, agent_session_id=agent_sid)
    try:
        status, receipt = post(f"{gw}/s/{sid}/prompts", token, sp)
    except GatewayError as e:
        return fail(f"el gateway no respondió ({e})")
    if status != 200 or not isinstance(receipt, dict) or "body" not in receipt:
        reason = receipt.get("reason") if isinstance(receipt, dict) else None
        return fail(f"el gateway rechazó el prompt firmado ({reason or status})")
    try:
        save_receipt(receipt, env)
    except (OSError, KeyError, TypeError) as e:
        return HookResult(0, "", f"Open Witness: prompt firmado, pero no se pudo guardar el recibo ({e}).\n")
    return HookResult()


def save_receipt(receipt: dict, env: Mapping[str, str]) -> Path:
    """`$OWIT_HOME/receipts/<sid>_<seq>.json`: el directorio que lee `owit verify --receipts`."""
    body = receipt["body"]
    d = owit_home(env) / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{body['session_id']}_{int(body['seq']):06d}.json"
    path.write_text(json.dumps(receipt, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- PreToolUse

def _deny(reason: str) -> HookResult:
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                  "permissionDecisionReason": reason}}
    return HookResult(0, json.dumps(out, ensure_ascii=False) + "\n", "")


def hook_pre_tool(stdin_text: str, env: Mapping[str, str], post: Post = http_post) -> HookResult:
    """Consulta `/s/<sid>/tool-check`. Si la respuesta es `deny`, la herramienta no se ejecuta."""
    sess = _session(env)
    if sess is None:
        return HookResult()
    gw, sid, token = sess
    j = _payload(stdin_text)
    tool = j.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return HookResult(0, "", "Open Witness: el hook no recibió `tool_name`.\n")
    body: dict[str, Any] = {"tool": tool, "args": j.get("tool_input") or {}}
    if isinstance(j.get("tool_use_id"), str):
        body["tool_use_id"] = j["tool_use_id"]
    if j.get("mcp_server"):
        body["mcp_server"] = j["mcp_server"]

    def unavailable(msg: str) -> HookResult:
        if _strict(env):
            return _deny(f"Open Witness no pudo verificar {tool}: {msg} (modo estricto)")
        return HookResult(0, "", f"Open Witness: no se pudo verificar {tool}: {msg}.\n")

    try:
        status, res = post(f"{gw}/s/{sid}/tool-check", token, body)
    except GatewayError as e:
        return unavailable(f"el gateway no respondió ({e})")
    if status != 200 or not isinstance(res, dict):
        return unavailable(f"respuesta {status} del gateway")
    if res.get("decision") == "deny":
        return _deny(str(res.get("reason") or f"Open Witness bloqueó {tool}"))
    return HookResult()


# ---------------------------------------------------------------- SessionStart

def hook_session_start(stdin_text: str, env: Mapping[str, str], post: Post = http_post) -> HookResult:
    """Asocia el id de sesión del agente con el `sid` del gateway, en `$OWIT_HOME/sessions/<sid>.json`.

    El id del agente también viaja firmado en cada prompt (`agent_session_id`); este archivo sirve para
    encontrar la bitácora a partir de una sesión del agente. No habla con el gateway y no escribe en stdout.
    """
    sess = _session(env)
    if sess is None:
        return HookResult()
    gw, sid, _ = sess
    j = _payload(stdin_text)
    try:
        d = owit_home(env) / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{sid}.json"
        known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        ids = known.get("agent_session_ids", [])
        if isinstance(j.get("session_id"), str) and j["session_id"] not in ids:
            ids.append(j["session_id"])
        data = {"sid": sid, "gateway": gw, "user": env.get("OWIT_USER"), "agent_session_ids": ids,
                "cwd": j.get("cwd"), "source": j.get("source")}
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, AttributeError) as e:
        return HookResult(0, "", f"Open Witness: no se pudo anotar la sesión ({e}).\n")
    return HookResult()


HOOKS = {"prompt": hook_prompt, "pre-tool": hook_pre_tool, "session-start": hook_session_start}


def main(kind: str, stdin_text: str | None = None, env: Mapping[str, str] | None = None) -> int:
    """Punto de entrada de `owit hook <kind>`: escribe la salida y devuelve el código de salida."""
    res = HOOKS[kind](sys.stdin.read() if stdin_text is None else stdin_text, os.environ if env is None else env)
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    return res.exit_code
