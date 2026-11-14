"""Manifiestos MCP firmados: firma, TrustStore y ManifestGate (SPEC §6.3, §7.3). Dueña: Juliana."""
from .gate import ManifestGate
from .signer import manifest_hash, sign_manifest, tool_hash, tools_from_manifest, verify_manifest
from .trust_store import TrustStore

__all__ = ["ManifestGate", "TrustStore", "manifest_hash", "sign_manifest", "tool_hash", "tools_from_manifest",
           "verify_manifest"]
