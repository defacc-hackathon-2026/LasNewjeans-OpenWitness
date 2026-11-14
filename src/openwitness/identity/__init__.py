"""Identidades: llaves Ed25519, registro y prompts firmados (SPEC §6.2, §7.2). Dueña: Juliana."""
from .keys import (DOMAIN_ANCHOR, DOMAIN_LEDGER, DOMAIN_MANIFEST, DOMAIN_PROMPT, DOMAIN_RECEIPT, KeyPair,
                   verify)
from .prompts import sign_prompt, verify_prompt
from .registry import KeyRegistry

__all__ = ["DOMAIN_ANCHOR", "DOMAIN_LEDGER", "DOMAIN_MANIFEST", "DOMAIN_PROMPT", "DOMAIN_RECEIPT", "KeyPair",
           "KeyRegistry", "sign_prompt", "verify", "verify_prompt"]
