"""Pipeline de atribución de una acción (SPEC §7.9): integridad → evidencia → taint → reglas → juez.

Modos (`mode`):
- `rules`: solo reglas; si no deciden, el respaldo de R3 (`AMBIGUOUS`) o R4 (`MODEL_INITIATIVE`, baja confianza);
- `rules+judge`: reglas, y el juez solo si las reglas no deciden;
- `judge`: solo juez (para medirlo, §9.2). Igual nunca anula la integridad;
- `rules-no-r2b`: ablación con solo R1–R2 (como el prototipo del 13 de noviembre).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..ledger import BlobStore
from ..types import IntegrityReport, LedgerRecord, Verdict
from .evidence import EvidenceBundle, build_evidence
from .judge import judge_verdict
from .rules import rule_outcome


def attribute_evidence(ev: EvidenceBundle, llm=None, mode: str = "rules") -> tuple[Verdict, dict]:
    """Devuelve (veredicto, traza) a partir de una evidencia ya construida."""
    out = rule_outcome(ev, r2b=(mode != "rules-no-r2b"))
    trace = {"rule": out.rule, **out.detail}
    if out.rule == "INTEGRITY":
        return out.verdict, trace                               # nunca se atribuye sobre evidencia corrupta
    if mode == "judge" and llm is not None:
        jv = judge_verdict(ev, llm)
        jv = replace(jv, evidence=out.fallback.evidence, arg_provenance=out.fallback.arg_provenance)
        return jv, trace | {"judge_label": jv.label}
    if out.verdict is not None:
        return out.verdict, trace
    if mode == "rules+judge" and llm is not None:
        jv = judge_verdict(ev, llm)
        trace["judge_label"] = jv.label
        if jv.method == "judge_failed":
            return replace(out.fallback, method="judge_failed", judge=jv.judge), trace
        return replace(jv, method="rules+judge", evidence=out.fallback.evidence,
                       arg_provenance=out.fallback.arg_provenance), trace
    return out.fallback, trace


def attribute(ledger_path: Path | str, blobs: BlobStore, seq: int, integrity: IntegrityReport | None = None,
              llm=None, mode: str = "rules", records: list[LedgerRecord] | None = None) -> Verdict:
    ev = build_evidence(ledger_path, blobs, seq, integrity, records)
    return attribute_evidence(ev, llm, mode)[0]
