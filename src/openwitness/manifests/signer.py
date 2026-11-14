"""Firma de manifiestos MCP (SPEC §6.3, §7.3). Dueña: Juliana.

Un `SignedManifest` es un dict `{"body": {...}, "pubkey": "ed25519:…", "sig": "…"}` firmado con la etiqueta
`openwitness/manifest/v1`. Los nombres de las herramientas son los **originales** del servidor (`list_issues`),
no los que ve el modelo (`mcp__issues__list_issues`).
"""
from __future__ import annotations

import json
from typing import Any

from ..canonical import CanonicalError, canonical_json, hash_obj, sha256_tagged
from ..identity.keys import DOMAIN_MANIFEST, KeyPair, verify
from ..timeutil import now_rfc3339
from ..types import SignedManifest, ToolDef


def _descriptor_bytes(descriptor: dict) -> bytes:
    """Los mismos bytes que `ledger.blobs.encode_payload` (claves ordenadas, sin espacios); admite floats."""
    return json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def tool_hash(tool: ToolDef) -> str:
    """`tool_hash` de §6.3: hash de `{name, description, input_schema}`.

    Si el esquema trae floats (p. ej. `minimum: 0.5`) no se puede canonizar; entonces se usa el hash de los
    bytes del blob (§6.1, "Blobs y floats"). Para estructuras sin floats ambos caminos dan el mismo hash.
    """
    d = tool.descriptor()
    try:
        return hash_obj(d)
    except CanonicalError:
        return sha256_tagged(_descriptor_bytes(d))


def _tool_entry(tool: ToolDef) -> dict:
    entry: dict[str, Any] = {"name": tool.name, "description": tool.description, "tool_hash": tool_hash(tool)}
    try:
        canonical_json(tool.input_schema)
        entry["input_schema"] = tool.input_schema
    except CanonicalError:
        # El cuerpo va firmado y no admite floats: el esquema se guarda como texto JSON exacto.
        entry["input_schema_json"] = _descriptor_bytes(tool.input_schema).decode("utf-8")
    return entry


def sign_manifest(kp: KeyPair, server_id: str, version: str, publisher: str, tools: list[ToolDef],
                  issued_at: str | None = None) -> SignedManifest:
    names = [t.name for t in tools]
    if len(set(names)) != len(names):
        raise ValueError(f"nombres de herramienta repetidos en el manifiesto de {server_id}")
    body = {"v": 1, "server_id": server_id, "server_version": version, "publisher": publisher,
            "tools": [_tool_entry(t) for t in tools], "issued_at": issued_at or now_rfc3339()}
    return {"body": body, "pubkey": kp.public_str(), "sig": kp.sign(DOMAIN_MANIFEST, body)}


def manifest_hash(manifest: SignedManifest) -> str:
    """`hash_obj(SignedManifest.body)`: lo que se fija en un pin y lo que registra `manifest_decision`."""
    return hash_obj(manifest["body"])


def is_well_formed(manifest: Any) -> bool:
    try:
        body = manifest["body"]
        if not (isinstance(manifest["pubkey"], str) and isinstance(manifest["sig"], str)):
            return False
        if not (isinstance(body["server_id"], str) and isinstance(body["publisher"], str)):
            return False
        tools = body["tools"]
        return isinstance(tools, list) and all(
            isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("tool_hash"), str)
            for t in tools)
    except (KeyError, TypeError):
        return False


def verify_manifest(manifest: Any) -> bool:
    """Bien formado y con firma válida bajo su propia `pubkey`. No dice si esa llave es de confianza."""
    return is_well_formed(manifest) and verify(DOMAIN_MANIFEST, manifest["body"], manifest["sig"], manifest["pubkey"])


def tools_from_manifest(manifest: SignedManifest) -> list[ToolDef]:
    """Las herramientas tal como las firmó el publicador (útil para el corpus y para el agente de referencia)."""
    out = []
    for t in manifest["body"]["tools"]:
        schema = json.loads(t["input_schema_json"]) if "input_schema_json" in t else t.get("input_schema") or {}
        out.append(ToolDef(t["name"], t.get("description", "") or "", schema))
    return out
