"""Atribución de acciones: ¿lo pidió el usuario o lo inyectó la herramienta Z? (SPEC §7.9). Dueño: Andres."""
from .attribute import attribute, attribute_evidence
from .evidence import EvidenceBundle, build_evidence, provenance
from .judge import judge_verdict
from .rules import RuleOutcome, rule_outcome, rule_verdict
from .taint import SensitiveArg, extract_sensitive

__all__ = ["EvidenceBundle", "RuleOutcome", "SensitiveArg", "attribute", "attribute_evidence", "build_evidence",
           "extract_sensitive", "judge_verdict", "provenance", "rule_outcome", "rule_verdict"]
