"""TrustStore: publicadores de confianza, pins y manifiestos conocidos (SPEC §6.3, §7.3). Dueña: Juliana.

En disco es un directorio (`manifests/`):

    manifests/trust.json     {"publishers": {"<publisher>": ["ed25519:…"]},
                              "pins": {"<server_id>": {"manifest_hash": "sha256:…", "pubkey": "ed25519:…"}},
                              "builtin_allowlist": {"<agente>": ["Bash", "Read", …]}}
    manifests/<server>.json  un SignedManifest por archivo

Un pin fija qué llave firma los manifiestos de un servidor. Si no hay pin, el primer manifiesto válido y de
confianza lo crea (confianza en el primer uso). Un manifiesto posterior con **otra llave** es un rug pull.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..types import SignedManifest
from .signer import is_well_formed, manifest_hash, verify_manifest

TRUST_FILE = "trust.json"


class TrustStore:
    def __init__(self) -> None:
        self.publishers: dict[str, list[str]] = {}
        self.pins: dict[str, dict[str, str]] = {}
        self.manifests: dict[str, SignedManifest] = {}
        self.builtin_allowlist: dict[str, list[str]] = {}
        self.skipped: list[str] = []          # archivos de load_dir que no eran manifiestos legibles

    # ------------------------------------------------------------------ confianza y pins
    def trust(self, publisher: str, pubkey: str) -> None:
        keys = self.publishers.setdefault(publisher, [])
        if pubkey not in keys:
            keys.append(pubkey)

    def is_trusted(self, publisher: str, pubkey: str) -> bool:
        return pubkey in self.publishers.get(publisher, [])

    def pin(self, server_id: str, manifest_hash: str, pubkey: str) -> None:
        self.pins[server_id] = {"manifest_hash": manifest_hash, "pubkey": pubkey}

    def pin_for(self, server_id: str) -> dict[str, str] | None:
        return self.pins.get(server_id)

    # ------------------------------------------------------------------ manifiestos
    def add_manifest(self, manifest: SignedManifest) -> None:
        """Registra el manifiesto vigente de su servidor. No decide nada: eso lo hace `ManifestGate`.

        Un manifiesto mal formado se guarda igual si al menos trae `server_id`, para que el gate lo rechace
        con `bad_sig` en vez de tratar al servidor como si no tuviera manifiesto.
        """
        try:
            server_id = manifest["body"]["server_id"]
        except (KeyError, TypeError) as e:
            raise ValueError("manifiesto sin body.server_id") from e
        if not isinstance(server_id, str) or not server_id:
            raise ValueError("manifiesto sin body.server_id")
        self.manifests[server_id] = manifest
        self.ensure_pin(manifest)

    def ensure_pin(self, manifest: SignedManifest) -> None:
        """Crea el pin en el primer uso, solo con un manifiesto válido firmado por una llave de confianza."""
        if not is_well_formed(manifest):
            return
        body = manifest["body"]
        if body["server_id"] in self.pins:
            return
        if verify_manifest(manifest) and self.is_trusted(body["publisher"], manifest["pubkey"]):
            self.pin(body["server_id"], manifest_hash(manifest), manifest["pubkey"])

    def manifest_for(self, server_id: str) -> SignedManifest | None:
        return self.manifests.get(server_id)

    # ------------------------------------------------------------------ disco
    def load_dir(self, path: Path | str) -> None:
        """Carga `trust.json` y después cada `*.json` como manifiesto, en orden alfabético."""
        root = Path(path)
        tf = root / TRUST_FILE
        if tf.exists():
            data = json.loads(tf.read_text(encoding="utf-8"))
            for publisher, keys in (data.get("publishers") or {}).items():
                for k in keys:
                    self.trust(publisher, k)
            for server_id, p in (data.get("pins") or {}).items():
                self.pin(server_id, p["manifest_hash"], p["pubkey"])
            self.builtin_allowlist.update(data.get("builtin_allowlist") or {})
        for f in sorted(root.glob("*.json")):
            if f.name == TRUST_FILE:
                continue
            try:
                self.add_manifest(json.loads(f.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                self.skipped.append(f.name)

    def save_trust(self, path: Path | str) -> None:
        """Escribe `trust.json` (publicadores, pins y listas de herramientas internas) en el directorio."""
        root = Path(path)
        root.mkdir(parents=True, exist_ok=True)
        data = {"publishers": self.publishers, "pins": self.pins, "builtin_allowlist": self.builtin_allowlist}
        (root / TRUST_FILE).write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                                       encoding="utf-8")
