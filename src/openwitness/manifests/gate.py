"""ManifestGate: decide si una herramienta MCP anunciada en vivo se acepta (SPEC §6.3, §7.3). Dueña: Juliana.

Orden de las comprobaciones (gana el primer motivo que aplica):

1. `unsigned`             no hay manifiesto para el servidor.
2. `bad_sig`              el manifiesto está mal formado o su firma no verifica.
3. `rug_pull`             hay un pin para el servidor y el manifiesto viene firmado por otra llave.
4. `untrusted_key`        la llave no está en el TrustStore para ese publicador.
5. `descriptor_mismatch`  la herramienta en vivo no está en el manifiesto o su `tool_hash` no coincide.
6. `shadowing`            otro servidor ya es dueño de ese nombre en la sesión.

Solo una herramienta **aceptada** reclama su nombre en `seen_owner`: una rechazada no le quita el nombre
al servidor legítimo.
"""
from __future__ import annotations

from ..types import GateDecision, SignedManifest, ToolDef
from .signer import manifest_hash, tool_hash, verify_manifest
from .trust_store import TrustStore


class ManifestGate:
    def __init__(self, trust_store: TrustStore | None = None):
        self.trust_store = trust_store or TrustStore()
        self.seen_owner: dict[str, str] = {}      # estado propio, para quien no lleva uno por sesión (refagent)

    # ------------------------------------------------------------------ contrato de §7.3
    def evaluate_tool(self, server_id: str, live: ToolDef, seen_owner: dict[str, str] | None = None) -> GateDecision:
        """Evalúa una herramienta contra el manifiesto vigente de `server_id` en el TrustStore.

        `live.name` es el nombre original del servidor. `seen_owner` (nombre → server_id) es estado de la
        sesión: se consulta y se actualiza aquí.
        """
        owners = self.seen_owner if seen_owner is None else seen_owner
        return self._evaluate(server_id, self.trust_store.manifest_for(server_id), live, owners)

    def evaluate_server(self, manifest: SignedManifest | None, live_tools: list[ToolDef],
                        server_id: str | None = None, seen_owner: dict[str, str] | None = None) -> list[GateDecision]:
        """Evalúa todas las herramientas de un servidor contra el manifiesto que este presenta.

        Para el cliente MCP del agente de referencia. `server_id` es el nombre con que el agente conoce al
        servidor; si se omite, se toma del manifiesto. Un manifiesto de otro servidor no respalda a este.
        """
        owners = self.seen_owner if seen_owner is None else seen_owner
        claimed = None
        if isinstance(manifest, dict) and isinstance(manifest.get("body"), dict):
            claimed = manifest["body"].get("server_id")
        sid = server_id or (claimed if isinstance(claimed, str) else None) or "unknown"
        if manifest is not None and isinstance(claimed, str) and claimed != sid:
            manifest = None                          # firmado para otro servidor: aquí es como no tener nada
        if manifest is not None:
            self.trust_store.ensure_pin(manifest)
        return [self._evaluate(sid, manifest, t, owners) for t in live_tools]

    # ------------------------------------------------------------------ política
    def _evaluate(self, server_id: str, manifest: SignedManifest | None, live: ToolDef,
                  owners: dict[str, str]) -> GateDecision:
        origin = f"mcp:{server_id}"
        th = tool_hash(live)

        def decide(accepted: bool, reason: str, mh: str | None = None) -> GateDecision:
            return GateDecision(accepted, reason, live, origin, th, mh)  # type: ignore[arg-type]

        if manifest is None:
            return decide(False, "unsigned")
        if not verify_manifest(manifest) or manifest["body"]["server_id"] != server_id:
            return decide(False, "bad_sig")
        body, pubkey = manifest["body"], manifest["pubkey"]
        mh = manifest_hash(manifest)
        pin = self.trust_store.pin_for(server_id)
        if pin is not None and pin["pubkey"] != pubkey:
            return decide(False, "rug_pull", mh)
        if not self.trust_store.is_trusted(body["publisher"], pubkey):
            return decide(False, "untrusted_key", mh)
        signed = next((t for t in body["tools"] if t["name"] == live.name), None)
        if signed is None or signed["tool_hash"] != th:
            return decide(False, "descriptor_mismatch", mh)
        owner = owners.get(live.name)
        if owner is not None and owner != server_id:
            return decide(False, "shadowing", mh)
        owners[live.name] = server_id
        return decide(True, "ok", mh)
