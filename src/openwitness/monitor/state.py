"""Estado del monitor (SPEC §17.5, G14): lee `data/` en vivo, verifica, atribuye con reglas y deduce alertas.

Solo lectura: nunca escribe en `data/`, nunca tiene `gateway_sk` ni tokens de sesión (papel del auditor, §3).

- **Lectura en vivo:** revisa tamaño y fecha de cada `data/ledger/*.jsonl`; agrega solo líneas completas (ignora una
  última línea sin `\\n`, que el gateway todavía está escribiendo). Si el archivo se reescribió (p. ej. `owit tamper`),
  recarga la sesión entera.
- **Integridad:** `verify_ledger` al abrir una sesión, al llegar un ancla, al recargar y cada 5 s mientras haya
  cambios.
- **Atribución en vivo:** solo reglas, nunca el juez (§7.9 paso 5); es *provisional* y pasa a `UNVERIFIABLE` si la
  integridad falla en algún `seq` ≤ el de la acción.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..attribution import attribute_evidence, build_evidence
from ..attribution.evidence import entry_text, provenance
from ..attribution.taint import appears_in, extract_sensitive
from ..identity import KeyPair, KeyRegistry
from ..ledger import BlobStore, MockAnchor, verify_ledger
from ..types import IntegrityReport, LedgerRecord

FRAGMENT = 280
VERIFY_EVERY_S = 5.0
CHECK_NAMES = {1: "hash", 2: "firma del gateway", 3: "cadena y secuencia", 4: "referencias de contexto",
               5: "prompts firmados", 6: "herramientas aceptadas", 7: "ancla externa", 8: "recibos del usuario",
               9: "blobs", 10: "coherencia del contexto"}
LEVELS = ("critical", "high", "medium", "info")


class ReadOnlyBlobs(BlobStore):
    """`BlobStore` que no crea la carpeta si falta."""

    def __init__(self, root: Path | str):   # noqa: D107 — a propósito sin super().__init__ (que hace mkdir)
        self.root = Path(root)


class ReadOnlyAnchor(MockAnchor):
    """`MockAnchor` que no genera llave si falta (sin llave, ningún ancla verifica)."""

    def __init__(self, path: Path | str):   # noqa: D107
        self.path = Path(path)
        kp = self.path.with_suffix(".key")
        self.kp = KeyPair.load(kp) if kp.exists() else None
        self._lock = threading.Lock()

    def check(self, receipt, head_hash: str) -> bool:
        return self.kp is not None and super().check(receipt, head_hash)


def excerpt(text: str, needles: list[str], n: int = FRAGMENT) -> str:
    """Recorta a `n` caracteres, centrado en la primera coincidencia de `needles` si la hay."""
    t = text or ""
    if len(t) <= n:
        return t
    low = t.lower()
    pos = min((p for p in (low.find(x.lower()) for x in needles if x) if p >= 0), default=-1)
    if pos < 0:
        return t[: n - 1] + "…"
    start = max(0, min(pos - n // 3, len(t) - n))
    return ("…" if start else "") + t[start:start + n - 2] + ("…" if start + n - 2 < len(t) else "")


def color_of(rec: LedgerRecord) -> str:
    """Procedencia para colorear la línea de tiempo (§17.5 vista 2)."""
    if rec.kind in ("user_prompt", "user_text_unsigned", "tool_result", "llm_response", "tool_call",
                    "context_reset"):
        return provenance(rec)[0]
    if rec.kind == "llm_request":
        return "agent"
    if rec.kind == "tool_check":
        return "hook"
    return "gateway"


def summary_of(rec: LedgerRecord) -> str:
    """Resumen de una entrada solo con metadatos (no lee blobs: sirve también con `--no-content`)."""
    m, k = rec.meta, rec.kind
    if k == "session_start":
        ag = m.get("agent") or {}
        up = m.get("upstream") or {}
        return (f"{m.get('user_id')} · {ag.get('name', '?')} {ag.get('version', '')}".strip()
                + f" · upstream {up.get('profile', '?')}")
    if k == "manifest_decision":
        return f"{m.get('tool')} → {m.get('decision')} ({m.get('reason')})"
    if k == "user_prompt":
        return f"prompt firmado por {m.get('user_id')}"
    if k == "user_text_unsigned":
        return f"texto sin firma ({m.get('reason')})"
    if k == "llm_request":
        s = f"petición al LLM · {len(rec.context)} entradas de contexto · {len(m.get('tools_offered') or [])} herramientas"
        st = m.get("tools_stripped") or []
        return s + (f" · {len(st)} quitadas" if st else "")
    if k == "llm_response":
        return f"respuesta del modelo ({m.get('stop_reason')}) · {m.get('model_reported') or '?'}"
    if k == "tool_call":
        return f"{m.get('tool')} ({m.get('origin')})" + (f" · ANOMALÍA {m['anomaly']}" if m.get("anomaly") else "")
    if k == "tool_check":
        return f"hook {m.get('decision')}: {m.get('tool')} — {m.get('reason')}"
    if k == "tool_result":
        return f"resultado de {m.get('tool')} ({m.get('origin')})" + (" · error" if m.get("is_error") else "")
    if k == "context_reset":
        return f"historial reiniciado ({m.get('reason')})"
    if k == "anchor":
        return f"ancla hasta seq {m.get('head_seq')} ({m.get('backend')})"
    if k == "session_end":
        return f"fin de sesión ({m.get('reason')})"
    return k


def safe_meta(m: dict) -> dict:
    out = {k: v for k, v in m.items() if k not in ("segments", "tools_offered", "policy")}
    if "tools_offered" in m:
        out["tools_offered_count"] = len(m["tools_offered"] or [])
    return out


@dataclass
class SessionView:
    path: Path
    raw: bytes = b""
    stamp: tuple[int, int] | None = None              # (tamaño, mtime_ns) de la última lectura
    records: list[LedgerRecord] = field(default_factory=list)
    report: IntegrityReport | None = None
    verified_at: float = 0.0
    verify_ms: float = 0.0
    dirty: bool = False
    verdicts: dict[int, dict] = field(default_factory=dict)
    alerts: dict[str, dict] = field(default_factory=dict)
    reloads: int = 0

    @property
    def sid(self) -> str:
        return self.path.stem


class Monitor:
    def __init__(self, data: Path | str, registry: KeyRegistry | Path | str | None = None,
                 gateway_pubkey: str | None = None, no_content: bool = False):
        self.data = Path(data)
        self.blobs = ReadOnlyBlobs(self.data / "blobs")
        self.anchor = ReadOnlyAnchor(self.data / "anchor" / "mock_anchor.jsonl")
        if isinstance(registry, KeyRegistry):
            self.registry = registry
        else:
            self.registry = KeyRegistry.load(registry) if registry else KeyRegistry()
        self.pubkey, self.pubkey_source = self._pubkey(gateway_pubkey)
        self.no_content = no_content
        self.sessions: dict[str, SessionView] = {}
        self.lock = threading.RLock()

    def _pubkey(self, given: str | None) -> tuple[str | None, str]:
        if given:
            return given, "flag"
        pub = self.data / "gateway.pub"
        if pub.exists():
            return pub.read_text(encoding="utf-8").strip(), "gateway.pub"
        key = self.data / "gateway.key"
        if key.exists():   # demo: el auditor real recibiría solo la pública
            return KeyPair.load(key).public_str(), "gateway.key (solo la parte pública)"
        return None, "session_start (TOFU)"

    # ------------------------------------------------------------------ lectura en vivo
    def refresh(self, now: float | None = None) -> list[dict]:
        """Lee lo nuevo de todas las bitácoras. Devuelve eventos para el SSE."""
        now = time.monotonic() if now is None else now
        events: list[dict] = []
        ledger_dir = self.data / "ledger"
        paths = sorted(ledger_dir.glob("*.jsonl")) if ledger_dir.is_dir() else []
        with self.lock:
            for p in paths:
                sv = self.sessions.get(p.stem)
                if sv is None:
                    sv = self.sessions[p.stem] = SessionView(p)
                events += self._read(sv, now)
            for sid in [s for s in self.sessions if not self.sessions[s].path.exists()]:
                del self.sessions[sid]
                events.append({"type": "removed", "sid": sid})
        return events

    def _read(self, sv: SessionView, now: float) -> list[dict]:
        try:
            st = sv.path.stat()
        except FileNotFoundError:
            return []
        stamp = (st.st_size, st.st_mtime_ns)
        events: list[dict] = []
        if stamp != sv.stamp:
            sv.stamp = stamp
            data = sv.path.read_bytes()
            complete = data[: data.rfind(b"\n") + 1]
            reset = not complete.startswith(sv.raw)
            if reset:
                sv.raw, sv.records, sv.verdicts, sv.alerts = b"", [], {}, {}
                sv.reloads += 1
            new_bytes = complete[len(sv.raw):]
            new: list[LedgerRecord] = []
            for line in new_bytes.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    try:
                        new.append(LedgerRecord.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError):
                        continue
            sv.raw = complete
            if new or reset:
                first = not sv.records and not reset and sv.report is None
                sv.records += new
                sv.dirty = True
                must = first or reset or any(r.kind in ("anchor", "session_end") for r in new) or sv.report is None
                if must:
                    events += self._verify(sv, now)
                for r in new:
                    events += self._alerts_for(sv, r)
                events.append({"type": "reset" if reset else "entries", "sid": sv.sid,
                               "entries": [self.entry_view(sv, r) for r in (sv.records if reset else new)]})
        if sv.dirty and now - sv.verified_at >= VERIFY_EVERY_S:
            events += self._verify(sv, now)
        if events:
            events.append({"type": "session", "session": self.summary(sv)})
        return events

    def _verify(self, sv: SessionView, now: float) -> list[dict]:
        t0 = time.perf_counter()
        pub = self.pubkey
        if pub is None and sv.records:
            pub = (sv.records[0].meta or {}).get("gateway_pubkey")
        old_ok = sv.report.ok if sv.report else None
        sv.report = verify_ledger(sv.path, self.blobs, pub or "", self.registry, self.anchor)
        sv.verify_ms = round((time.perf_counter() - t0) * 1000, 2)
        sv.verified_at, sv.dirty = now, False
        events = []
        # las acciones cuya integridad cambió se vuelven a atribuir (p. ej. → UNVERIFIABLE)
        for r in sv.records:
            if r.kind == "tool_call":
                v = sv.verdicts.get(r.seq)
                if v is None or v["integrity_ok"] != sv.report.ok_up_to(r.seq):
                    events += self._attribute(sv, r)
        aid = f"{sv.sid}:integrity"
        if not sv.report.ok:
            e = sv.report.errors[0]
            alert = {"id": aid, "sid": sv.sid, "seq": sv.report.first_error_seq(), "level": "critical",
                     "type": "integrity_fail", "title": "La bitácora no verifica",
                     "detail": f"comprobación {e.check} ({CHECK_NAMES.get(e.check, '')}): {e.error} "
                               f"en seq={e.seq}" + (f" · {len(sv.report.errors)} errores" if len(sv.report.errors) > 1 else ""),
                     "ts": sv.records[-1].entry.get("ts") if sv.records else None}
            if sv.alerts.get(aid) != alert:
                sv.alerts[aid] = alert
                events.append({"type": "alert", "alert": alert})
        elif aid in sv.alerts:
            del sv.alerts[aid]
            events.append({"type": "alert_cleared", "id": aid, "sid": sv.sid})
        if old_ok is not None and old_ok != sv.report.ok:
            events.append({"type": "integrity", "sid": sv.sid, "ok": sv.report.ok})
        return events

    def _attribute(self, sv: SessionView, r: LedgerRecord) -> list[dict]:
        ev = build_evidence(sv.path, self.blobs, r.seq, sv.report, sv.records)
        v, trace = attribute_evidence(ev, None, "rules")
        sv.verdicts[r.seq] = {"label": v.label, "source": v.source, "non_repudiable": v.non_repudiable,
                              "partial": v.partial, "confidence": v.confidence, "rule": trace.get("rule"),
                              "arg_provenance": v.arg_provenance, "provisional": True,
                              "integrity_ok": ev.integrity_ok}
        aid = f"{sv.sid}:{r.seq}:verdict"
        events = [{"type": "verdict", "sid": sv.sid, "seq": r.seq, "verdict": sv.verdicts[r.seq]}]
        if v.label == "TOOL_INJECTED":
            alert = {"id": aid, "sid": sv.sid, "seq": r.seq, "level": "high", "type": "tool_injected",
                     "title": f"Acción atribuida a {v.source}" + (" (parcial)" if v.partial else ""),
                     "detail": f"{ev.tool}: " + ("acción pedida por el usuario, argumento aportado por la herramienta"
                                                 if v.partial else "la herramienta inyectó la acción")
                               + " · " + ", ".join(f"{k} ← {', '.join(p) or '?'}" for k, p in v.arg_provenance.items()),
                     "ts": r.entry.get("ts")}
            sv.alerts[aid] = alert
            events.append({"type": "alert", "alert": alert})
        elif aid in sv.alerts:
            del sv.alerts[aid]
            events.append({"type": "alert_cleared", "id": aid, "sid": sv.sid})
        return events

    def _alerts_for(self, sv: SessionView, r: LedgerRecord) -> list[dict]:
        m, k = r.meta, r.kind
        a = None
        if k == "manifest_decision" and m.get("decision") == "rejected":
            a = ("high", "tool_stripped", f"Herramienta quitada por su manifiesto: {m.get('tool')}", m.get("reason"))
        elif k == "tool_check" and m.get("decision") == "deny":
            a = ("high", "tool_denied", f"Herramienta denegada por el hook: {m.get('tool')}", m.get("reason"))
        elif k == "tool_call" and m.get("anomaly"):
            a = ("high", "anomaly", f"El modelo llamó a una herramienta quitada o desconocida: {m.get('tool')}",
                 m.get("anomaly"))
        elif k == "context_reset" and m.get("reason") == "history_mismatch":
            a = ("medium", "history_mismatch", "El historial no coincide con lo que reenvió el gateway", None)
        elif k == "user_text_unsigned" and m.get("reason") in ("no_hook", "no_match"):
            a = ("info", "unsigned_text", "Texto de usuario sin firma (sin no repudio, L8)", m.get("reason"))
        events = []
        if a:
            alert = {"id": f"{sv.sid}:{r.seq}:{a[1]}", "sid": sv.sid, "seq": r.seq, "level": a[0], "type": a[1],
                     "title": a[2], "detail": a[3] or "", "ts": r.entry.get("ts")}
            sv.alerts[alert["id"]] = alert
            events.append({"type": "alert", "alert": alert})
        if k == "tool_call" and r.seq not in sv.verdicts and sv.report is not None:
            events += self._attribute(sv, r)
        return events

    # ------------------------------------------------------------------ vistas
    def badge(self, sv: SessionView) -> dict:
        rep = sv.report
        if rep is None:
            return {"status": "unknown"}
        b = {"ok": rep.ok, "entries": rep.entries, "last_anchored_seq": rep.last_anchored_seq,
             "unanchored_tail": rep.unanchored_tail, "verify_ms": sv.verify_ms,
             "missing_blobs": sum(1 for v in rep.blob_status.values() if v == "missing")}
        if not rep.ok:
            e = rep.errors[0]
            return b | {"status": "fail", "first_error_seq": rep.first_error_seq(), "failed_check": e.check,
                        "failed_check_name": CHECK_NAMES.get(e.check, ""), "errors": [x.to_dict() for x in rep.errors[:8]],
                        "error_count": len(rep.errors)}
        return b | {"status": "pending_anchor" if rep.unanchored_tail else "ok", "ok_up_to": rep.entries - 1}

    def summary(self, sv: SessionView) -> dict:
        recs = sv.records
        start = recs[0].meta if recs and recs[0].kind == "session_start" else {}
        count = lambda pred: sum(1 for r in recs if pred(r))   # noqa: E731
        labels: dict[str, int] = {}
        for v in sv.verdicts.values():
            labels[v["label"]] = labels.get(v["label"], 0) + 1
        alerts = {lv: sum(1 for a in sv.alerts.values() if a["level"] == lv) for lv in LEVELS}
        up = start.get("upstream") or {}
        return {"sid": sv.sid, "user": start.get("user_id"), "agent": start.get("agent") or {},
                "upstream": {k: up.get(k) for k in ("profile", "provider", "model_requested") if k in up},
                "protocol": start.get("protocol"), "entries": len(recs),
                "first_ts": recs[0].entry.get("ts") if recs else None,
                "last_ts": recs[-1].entry.get("ts") if recs else None,
                "ended": any(r.kind == "session_end" for r in recs),
                "counts": {"prompts_signed": count(lambda r: r.kind == "user_prompt"),
                           "text_unsigned": count(lambda r: r.kind == "user_text_unsigned"
                                                  and r.meta.get("reason") in ("no_hook", "no_match")),
                           "llm_requests": count(lambda r: r.kind == "llm_request"),
                           "tool_calls": count(lambda r: r.kind == "tool_call"),
                           "tools_stripped": count(lambda r: r.kind == "manifest_decision"
                                                   and r.meta.get("decision") == "rejected"),
                           "tools_denied": count(lambda r: r.kind == "tool_check" and r.meta.get("decision") == "deny"),
                           "anomalies": count(lambda r: r.kind == "tool_call" and r.meta.get("anomaly")),
                           "history_mismatch": count(lambda r: r.kind == "context_reset"
                                                     and r.meta.get("reason") == "history_mismatch")},
                "verdicts": labels, "alerts": alerts, "integrity": self.badge(sv), "reloads": sv.reloads}

    def entry_view(self, sv: SessionView, r: LedgerRecord) -> dict:
        frag, args = None, None
        if not self.no_content and r.payload_hash and r.kind not in ("llm_request", "session_start", "anchor",
                                                                     "manifest_decision"):
            payload = self.blobs.get_obj(r.payload_hash)
            if payload is not None:
                frag = excerpt(entry_text(r, payload), [])
                if r.kind == "tool_call":
                    args = {k: (v[:160] + "…" if isinstance(v, str) and len(v) > 160 else v)
                            for k, v in (payload.get("args") or {}).items()}
        status = sv.report.blob_status.get(r.seq) if sv.report else None
        return {"seq": r.seq, "ts": r.entry.get("ts"), "kind": r.kind, "actor": r.entry.get("actor"),
                "provenance": color_of(r), "hash": r.hash, "payload_hash": r.payload_hash, "blob": status,
                "summary": summary_of(r), "fragment": frag, "args": args, "meta": safe_meta(r.meta),
                "verdict": sv.verdicts.get(r.seq)}

    # ------------------------------------------------------------------ API
    def list_sessions(self) -> list[dict]:
        with self.lock:
            out = [self.summary(sv) for sv in self.sessions.values()]
        return sorted(out, key=lambda s: s["last_ts"] or "", reverse=True)

    def get(self, sid: str) -> SessionView | None:
        return self.sessions.get(sid)

    def entries(self, sid: str, after: int = -1) -> list[dict] | None:
        with self.lock:
            sv = self.sessions.get(sid)
            return None if sv is None else [self.entry_view(sv, r) for r in sv.records if r.seq > after]

    def verify(self, sid: str) -> dict | None:
        with self.lock:
            sv = self.sessions.get(sid)
            if sv is None:
                return None
            self._verify(sv, time.monotonic())
            return sv.report.to_dict() | {"badge": self.badge(sv), "pubkey_source": self.pubkey_source}

    def alerts(self, sid: str | None = None) -> list[dict]:
        with self.lock:
            out = [a for sv in self.sessions.values() if sid in (None, sv.sid) for a in sv.alerts.values()]
        order = {lv: i for i, lv in enumerate(LEVELS)}
        return sorted(out, key=lambda a: (a.get("ts") or "", -order[a["level"]]), reverse=True)

    def action(self, sid: str, seq: int) -> dict | None:
        with self.lock:
            sv = self.sessions.get(sid)
            if sv is None or not any(r.seq == seq and r.kind == "tool_call" for r in sv.records):
                return None
            ev = build_evidence(sv.path, self.blobs, seq, sv.report, sv.records)
            v, trace = attribute_evidence(ev, None, "rules")
            sens = extract_sensitive(ev.args)
            items = []
            for i in ev.items:
                matches = sorted({a.name for a in sens if appears_in(a, i.fragment)})
                needles = [a.value for a in sens if a.name in matches]
                items.append({"seq": i.seq, "kind": i.kind, "provenance": i.provenance, "reason": i.reason,
                              "resource": ev.resources.get(i.seq), "matches": matches,
                              "fragment": None if self.no_content else excerpt(i.fragment, needles)})
            return {"sid": sid, "seq": seq, "tool": ev.tool, "integrity_ok": ev.integrity_ok,
                    "integrity_errors": ev.integrity_errors,
                    "request_seq": ev.request.seq if ev.request else None,
                    "args": ({k: "•••" for k in ev.args} if self.no_content else ev.args),
                    "sensitive": [{"name": a.name, "kind": a.kind, "decisive": a.decisive,
                                   "value": None if self.no_content else a.value,
                                   "provenance": v.arg_provenance.get(a.name, [])} for a in sens],
                    "verdict": {"label": v.label, "source": v.source, "non_repudiable": v.non_repudiable,
                                "partial": v.partial, "confidence": v.confidence, "method": v.method,
                                "rule": trace.get("rule"), "arg_provenance": v.arg_provenance, "provisional": True,
                                "detail": {k: val for k, val in trace.items() if k != "rule"}},
                    "context": items, "user": ev.user_id}

    def stats(self) -> dict[str, Any]:
        """Agregados de todas las sesiones para la pestaña de estadísticas (solo metadatos y veredictos)."""
        from datetime import datetime

        def epoch(ts: str | None) -> float | None:
            try:
                return datetime.fromisoformat((ts or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None

        def bump(d: dict, *keys, n: int = 1) -> None:
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = d.get(keys[-1], 0) + n

        with self.lock:
            svs = list(self.sessions.values())
            out: dict[str, Any] = {"sessions": len(svs), "open": 0, "actions": 0, "by_label": {}, "partial": 0,
                                   "non_repudiable": 0, "by_source": {}, "by_user": {}, "by_rule": {}, "by_tool": {},
                                   "arg_sources": {}, "integrity": {}, "verify_ms": [], "alerts": {}, "alert_types": {},
                                   "prompts": {"signed": 0, "unsigned": 0}, "entries_by_kind": {}, "points": []}
            for sv in svs:
                summ = self.summary(sv)
                out["open"] += 0 if summ["ended"] else 1
                bump(out["integrity"], summ["integrity"].get("status", "unknown"))
                if summ["integrity"].get("verify_ms") is not None:
                    out["verify_ms"].append(summ["integrity"]["verify_ms"])
                out["prompts"]["signed"] += summ["counts"]["prompts_signed"]
                out["prompts"]["unsigned"] += summ["counts"]["text_unsigned"]
                for a in sv.alerts.values():
                    bump(out["alerts"], a["level"])
                    bump(out["alert_types"], a["type"])
                user = summ["user"] or "?"
                for r in sv.records:
                    bump(out["entries_by_kind"], r.kind)
                    if r.kind != "tool_call":
                        continue
                    v = sv.verdicts.get(r.seq)
                    if v is None:
                        continue
                    lab = v["label"]
                    out["actions"] += 1
                    bump(out["by_label"], lab)
                    out["partial"] += int(bool(v["partial"]))
                    out["non_repudiable"] += int(bool(v["non_repudiable"]))
                    bump(out["by_user"], user, lab)
                    bump(out["by_rule"], v.get("rule") or "?")
                    tool = str(r.meta.get("tool", "?")).split("__")[-1]
                    bump(out["by_tool"], tool, lab)
                    if lab == "TOOL_INJECTED" and v.get("source"):
                        bump(out["by_source"], str(v["source"]).split("(")[0])
                    for arg, provs in (v.get("arg_provenance") or {}).items():
                        if "#" in arg:
                            continue
                        cls = ("nowhere" if not provs else "user_signed" if "user_signed" in provs
                               else "user_unsigned" if "user_unsigned" in provs
                               else "tool" if any(p.startswith(("mcp:", "builtin:", "hosted:")) for p in provs)
                               else "model")
                        bump(out["arg_sources"], cls)
                    t = epoch(r.entry.get("ts"))
                    if t is not None:
                        out["points"].append((t, lab))
            # serie temporal: el ancho de cubeta se elige para que haya como mucho ~36 columnas
            pts = sorted(out.pop("points"))
            series: list[dict] = []
            width = 0
            if pts:
                span = max(pts[-1][0] - pts[0][0], 1)
                width = next((w for w in (10, 30, 60, 300, 900, 3600, 21600, 86400) if span / w <= 36), 86400)
                start = pts[0][0] - pts[0][0] % width
                buckets: dict[int, dict] = {}
                for t, lab in pts:
                    i = int((t - start) // width)
                    bump(buckets.setdefault(i, {}), lab)
                last = max(buckets)
                for i in range(last + 1):
                    series.append({"t": datetime.fromtimestamp(start + i * width).astimezone().isoformat(),
                                   "counts": buckets.get(i, {})})
            out["timeline"] = {"bucket_s": width, "series": series}
            ms = sorted(out.pop("verify_ms"))
            out["verify"] = ({"p50": ms[len(ms) // 2], "p95": ms[min(len(ms) - 1, int(len(ms) * 0.95))], "max": ms[-1]}
                             if ms else None)
        return out

    def health(self) -> dict[str, Any]:
        return {"ok": True, "data": str(self.data), "sessions": len(self.sessions), "no_content": self.no_content,
                "pubkey_source": self.pubkey_source, "registry_users": sorted(self.registry.users)}
