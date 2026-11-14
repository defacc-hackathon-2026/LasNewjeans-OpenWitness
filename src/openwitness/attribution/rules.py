"""Reglas de atribución R1, R1b, R2, R2b, R3 y R4 (SPEC §7.9 paso 4).

`rule_verdict(ev)` devuelve un `Verdict` si las reglas deciden, o `None` (R3/R4: se pasa al juez).
`rule_outcome(ev)` devuelve además qué regla aplicó y el veredicto de respaldo cuando no hay juez:
R3 → `AMBIGUOUS`; R4 → `MODEL_INITIATIVE` con confianza baja.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..types import EvidenceItem, Verdict
from .evidence import EvidenceBundle, is_tool_provenance
from .taint import SensitiveArg, extract_sensitive, occurrences

# Categoría de acción de cada herramienta (por su nombre original, sin el prefijo mcp__srv__)
ACTION_OF = {"send_email": "send", "send_message": "send", "post_message": "send",
             "read_file": "read", "Read": "read", "write_file": "write", "Write": "write", "Edit": "write",
             "list_issues": "list", "get_issue": "list", "Bash": "exec", "WebFetch": "fetch", "fetch": "fetch",
             # Codex (§17.4)
             "exec_command": "exec", "shell": "exec", "shell_command": "exec", "local_shell": "exec",
             "apply_patch": "write", "web_search": "fetch"}

W = r"(?:^|(?<=[^\wáéíóúñ]))"   # inicio de palabra que respeta tildes
INTENT_RE = {
    # "correo", "email" y "mail" solo cuentan como verbo ("email it to…", "mail the report") o con artículo
    # indefinido ("escribe un correo a…", "write an email"). Como sustantivo ("resume mis correos", "summarize the
    # emails") no son un pedido de envío: lo mostró LLMail-Inject (bench/llmail.py).
    "send": rf"{W}(env[ií]a\w*|enviar|manda\w*|mandar|m[aá]nda\w*|reenv[ií]a\w*|notifica\w*"
            rf"|avisa\w*|comparte|compartir|send|forward"
            rf"|(?:un|una|otro)\s+(?:correo|e-?mail|mail)"
            rf"|(?:an?|another)\s+(?:e-?mail|mail)"
            rf"|(?:^|(?<=[.!?;:]\s)|(?<=please\s))e-?mail(?:\s+(?:it|this|that|them|him|her|me|us|everyone|all|the|a|an)\b"
            rf"|\s+\S+@)"
            rf"|(?:e-?mail|mail)\s+(?:it|this|that|them|him|her|me|us|everyone)\s+(?:to|at)\b)\b",
    "upload": rf"{W}(s[uú]be\w*|subir|publ[ií]ca\w*|postea\w*|post|env[ií]a\w*|enviar|manda\w*|webhook|upload"
              rf"|reporta\w*|registra\w*)\b",
    "fetch": rf"{W}(abre|descarga\w*|consulta\w*|visita\w*|revisa\w*|lee|leer|resume\w*|p[aá]gina\w*|web|url|sitio"
             rf"|fetch|open|read|https?://)",
    "read": rf"{W}(lee|leer|l[eé]e(lo|la)|abre|revisa\w*|resume\w*|muestra\w*|archivo\w*|fichero\w*|contenido|read"
            rf"|open|cat)\b",
    "write": rf"{W}(escribe|escribir|guarda\w*|crea\w*|anota\w*|write|save)\b",
    "list": rf"{W}(issues?|lista\w*|resume\w*|revisa\w*|errores|bugs?|incidencias|tickets?|list)\b",
    "exec": rf"{W}(ejecuta\w*|corre|run|comando\w*|bash|shell)\b",
}

# Patrones imperativos dirigidos al agente (R2). "se envían" (impersonal) NO es imperativo: es la inyección sutil.
IMPERATIVE_RE = re.compile(
    rf"(?i){W}(ignora\w*|ignore|olvida\w*|disregard|forget|env[ií]a(?:le|lo|la|les|los)?|m[aá]nda(?:le|lo|la|les)?"
    rf"|manda|reenv[ií]a\w*|sube|publica|ejecuta|borra|elimina|copia|send|forward|upload|execute|delete"
    rf"|debes|tienes que|must|you should|instrucci[oó]n(?:es)?|instructions?|importante|important|urgente|urgent"
    rf"|system prompt|asistentes?|agentes?|assistant|ai agent)\b")


def tool_base(name: str, origin: str | None = None) -> str:
    """Nombre original de la herramienta: sin `mcp__srv__` (Claude Code) ni `srv_` (OpenCode, §17.3).

    El prefijo de OpenCode solo se quita si el origen registrado en la bitácora es ese servidor MCP.
    """
    if name.startswith("mcp__"):
        return name[5:].partition("__")[2] or name
    if origin and origin.startswith("mcp:"):
        prefix = re.sub(r"[^A-Za-z0-9_-]", "_", origin[4:]) + "_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def action_category(tool: str, args: dict, origin: str | None = None) -> str:
    base = tool_base(tool, origin)
    cat = ACTION_OF.get(base, "other")
    if cat == "fetch" and (str(args.get("method", "GET")).upper() != "GET" or args.get("body") or args.get("data")):
        return "upload"
    return cat


def intent_covers(category: str, texts: list[str]) -> bool:
    rx = INTENT_RE.get(category)
    if rx is None:
        return False
    return any(re.search(rx, t or "", re.IGNORECASE) for t in texts)


def has_imperative(text: str) -> bool:
    return bool(IMPERATIVE_RE.search(text or ""))


@dataclass
class RuleOutcome:
    verdict: Verdict | None            # None = las reglas no deciden
    rule: str                          # R1 | R1b | R2 | R2b | R3 | R4 | NONE
    fallback: Verdict                  # lo que se reporta si no hay juez
    detail: dict = field(default_factory=dict)


def _source_name(prov: str, items: list[EvidenceItem], ev: EvidenceBundle) -> str:
    """`builtin:<tool>(<recurso>)` para internas (§7.9); `mcp:<server>` tal cual."""
    if prov.startswith("builtin:"):
        res = next((ev.resources[i.seq] for i in items if i.provenance == prov and i.seq in ev.resources), None)
        return f"{prov}({res})" if res else prov
    return prov


def _evidence(ev: EvidenceBundle, occ: dict[int, list[EvidenceItem]]) -> list[EvidenceItem]:
    keep: dict[int, EvidenceItem] = {}
    for items in occ.values():
        for i in items:
            keep[i.seq] = i
    for i in ev.items:
        if i.provenance in ("user_signed", "user_unsigned"):
            keep.setdefault(i.seq, i)
    out = []
    for seq in sorted(keep):
        i = keep[seq]
        out.append(EvidenceItem(i.seq, i.kind, i.hash, i.fragment[:400], i.provenance, i.reason))
    return out


def arg_provenance(args: list[SensitiveArg], occ: dict[int, list[EvidenceItem]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for n, a in enumerate(args):
        provs = []
        for i in occ[n]:
            if i.provenance != "model" and i.provenance not in provs:
                provs.append(i.provenance)
        if not provs and any(i.provenance == "model" for i in occ[n]):
            provs = ["model"]
        key = a.name if a.name not in out else f"{a.name}#{n}"
        out[key] = provs
    return out


def rule_outcome(ev: EvidenceBundle, r2b: bool = True) -> RuleOutcome:
    """`r2b=False` reproduce el prototipo (solo R1–R2) para medir lo que aporta R2b (ablación, §9.5)."""
    if not ev.integrity_ok:
        v = Verdict("UNVERIFIABLE", None, False, 1.0, "rules",
                    evidence=[], arg_provenance={}, partial=False)
        return RuleOutcome(v, "INTEGRITY", v, {"errors": ev.integrity_errors})

    args = extract_sensitive(ev.args)
    occ = {n: occurrences(a, ev.items) for n, a in enumerate(args)}
    aprov = arg_provenance(args, occ)
    evidence = _evidence(ev, occ)
    cat = action_category(ev.tool, ev.args, ev.action.meta.get("origin"))
    signed_texts = ev.texts("user_signed")
    unsigned_texts = ev.texts("user_unsigned")
    signed_intent = intent_covers(cat, signed_texts)
    unsigned_intent = intent_covers(cat, unsigned_texts)
    user = ev.user_id
    detail = {"category": cat, "signed_intent": signed_intent, "unsigned_intent": unsigned_intent,
              "decisive_args": [a.name for a in args if a.decisive]}

    def V(label, source, nonrep, conf, partial=False):
        return Verdict(label, source, nonrep, conf, "rules", evidence=evidence, arg_provenance=aprov,
                       partial=partial)

    decisive = [n for n, a in enumerate(args) if a.decisive]
    provs = {n: {i.provenance for i in occ[n] if i.provenance != "model"} for n in decisive}

    if not decisive:
        if signed_intent:
            v = V("USER_INSTRUCTED", user, True, 0.75)
            return RuleOutcome(v, "R1", v, detail | {"note": "sin argumentos sensibles"})
        if unsigned_intent:
            v = V("USER_INSTRUCTED", user, False, 0.6)
            return RuleOutcome(v, "R1b", v, detail | {"note": "sin argumentos sensibles"})
        fb = V("MODEL_INITIATIVE", None, False, 0.3)
        return RuleOutcome(None, "R4", fb, detail | {"note": "sin argumentos sensibles ni intención del usuario"})

    in_signed = all("user_signed" in provs[n] for n in decisive)
    in_user = all(provs[n] & {"user_signed", "user_unsigned"} for n in decisive)

    # R1: todos los argumentos en texto firmado y la intención del prompt firmado cubre la acción
    if in_signed and signed_intent:
        v = V("USER_INSTRUCTED", user, True, 0.95)
        return RuleOutcome(v, "R1", v, detail)
    # R1b: igual, con texto del usuario sin firma
    if in_user and (unsigned_intent or signed_intent):
        v = V("USER_INSTRUCTED", user, False, 0.7)
        return RuleOutcome(v, "R1b", v, detail)

    tool_only = [n for n in decisive if provs[n] and all(is_tool_provenance(p) for p in provs[n])]
    intent = signed_intent or unsigned_intent
    nowhere = [n for n in decisive if not provs[n]]

    # R2: los argumentos aparecen solo en herramientas y el fragmento tiene imperativos dirigidos al agente
    if tool_only and len(tool_only) == len(decisive):
        imperative = {i.provenance for n in tool_only for i in occ[n]
                      if is_tool_provenance(i.provenance) and has_imperative(i.fragment)}
        if len(imperative) == 1:
            z = next(iter(imperative))
            v = V("TOOL_INJECTED", _source_name(z, ev.items, ev), False, 0.9, partial=intent)
            return RuleOutcome(v, "R2", v, detail | {"imperative_in": sorted(imperative)})
        if len(imperative) > 1:
            fb = V("AMBIGUOUS", None, False, 0.4)
            return RuleOutcome(None, "R3", fb, detail | {"imperative_in": sorted(imperative)})

    # R2b: el usuario pidió la acción, pero un argumento aparece solo en la herramienta Z
    if tool_only and intent and r2b:
        zs = {p for n in tool_only for p in provs[n]}
        if len(zs) == 1:
            z = next(iter(zs))
            v = V("TOOL_INJECTED", _source_name(z, ev.items, ev), False, 0.85 if signed_intent else 0.7,
                  partial=True)
            return RuleOutcome(v, "R2b", v, detail | {"tool_only": [args[n].name for n in tool_only]})
        fb = V("AMBIGUOUS", None, False, 0.4)
        return RuleOutcome(None, "R3", fb, detail | {"tool_only_in": sorted(zs)})

    if nowhere:
        fb = V("MODEL_INITIATIVE", None, False, 0.35)
        return RuleOutcome(None, "R4", fb, detail | {"nowhere": [args[n].name for n in nowhere]})
    # R3: en texto del usuario sin intención que cubra la acción, o solo en herramientas sin imperativos y sin
    # que el usuario pidiera la acción
    fb = V("AMBIGUOUS", None, False, 0.4)
    return RuleOutcome(None, "R3", fb, detail)


def rule_verdict(ev: EvidenceBundle) -> Verdict | None:
    return rule_outcome(ev).verdict
