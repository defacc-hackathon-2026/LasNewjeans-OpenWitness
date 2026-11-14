"""Benchmark de atribución (SPEC §9.2). Dueño: Andres.

Cada escenario de `bench/scenarios/*.yaml` se ejecuta con `refagent` → gateway en proceso → upstream, se
verifica la bitácora, se busca la acción esperada y se atribuye.

    # modelo falso ingenuo (siempre cae): mide la atribución
    uv run python -m bench.run_bench --out results
    # modelo real (gpt-oss-20b en LM Studio): tasa real de éxito de cada ataque y atribución sobre los que ocurrieron
    uv run python -m bench.run_bench --upstream lmstudio_adrian --out results
    # con juez (G10): modos rules, judge y rules+judge
    uv run python -m bench.run_bench --judge lmstudio_adrian_openai --out results

Salida: `results/attribution[_<perfil>].json` y `.txt` (matriz de confusión y precisión por campo).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from openwitness.attribution import attribute_evidence, build_evidence
from openwitness.ledger import load_records, verify_ledger
from openwitness.llm.config import load_profiles, make_client
from openwitness.llm.naive import NaiveResponder
from openwitness.refagent import InProcessToolbox, RefAgent
from openwitness.refagent.agent import in_process

from .synthetic import make_env

SCEN_DIR = Path(__file__).parent / "scenarios"
LABELS = ["USER_INSTRUCTED", "TOOL_INJECTED", "AMBIGUOUS", "MODEL_INITIATIVE", "UNVERIFIABLE"]
CLASSES = ["malicious_signed", "malicious_unsigned", "mcp_injection", "builtin_injection", "subtle_injection",
           "mixed", "benign"]


def load_scenarios(only: str | None = None) -> list[dict]:
    out = []
    for p in sorted(SCEN_DIR.glob("*.yaml")):
        s = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(s.get("fake_script"), str):
            s["fake_script"] = (yaml.safe_load(Path(s["fake_script"]).read_text(encoding="utf-8")) or {}).get("steps")
        if only and only not in (s["class"], s["id"]):
            continue
        out.append(s)
    return out


def source_base(s: str | None) -> str | None:
    return s.split("(", 1)[0] if s else s


def source_ok(expected: str | None, got: str | None) -> bool:
    if expected is None:
        return got is None
    if got is None:
        return False
    return got == expected if "(" in expected and "(" in got else source_base(got) == source_base(expected)


def find_action(recs, blobs, expected: dict):
    for r in recs:
        if r.kind != "tool_call" or r.meta.get("tool") != expected["tool"]:
            continue
        p = blobs.get_obj(r.payload_hash) or {}
        blob = json.dumps(p.get("args") or {}, ensure_ascii=False)
        if all(c in blob for c in expected.get("args_contains") or []):
            return r
    return None


async def run_one(s: dict, upstream=None, model: str = "fake-model", judge=None, modes=("rules",),
                  temperature: float | None = None) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"owit-bench-{s['id']}-"))
    env = make_env(root, anchor_every=20, upstream=upstream,
                   responder=None if upstream else NaiveResponder(s["fake_script"]))
    tools = InProcessToolbox(s.get("tool_fixtures") or {})
    _, sdk_http = in_process(env.gateway.app())
    agent = RefAgent("http://gw", tools, user=s["user"], key=env.users[s["user"]], http=env.client, model=model,
                     temperature=temperature, sdk_http=sdk_http)
    t0 = time.perf_counter()
    run = await agent.run(s["user_prompt"], signed=bool(s.get("signed", True)))
    dt = time.perf_counter() - t0
    ledger = env.gateway.ledger_path(run.sid)
    rep = verify_ledger(ledger, env.gateway.blobs, env.gateway_pubkey, env.registry, env.anchor, run.receipts)
    recs = load_records(ledger)
    act = find_action(recs, env.gateway.blobs, s["expected_action"])
    out = {"id": s["id"], "class": s["class"], "expected": {k: s.get(k) for k in
                                                            ("label", "source", "non_repudiable", "partial")},
           "occurred": act is not None, "integrity_ok": rep.ok, "integrity_errors": [e.to_dict() for e in rep.errors][:3],
           "seconds": round(dt, 2), "error": run.error, "turns": run.turns, "stats": run.stats,
           "tool_calls": [{"name": c["name"], "input": c["input"], "decision": c["decision"]} for c in run.tool_calls],
           "final_text": run.final_text[:500], "verdicts": {}}
    if act is not None:
        ev = build_evidence(ledger, env.gateway.blobs, act.seq, rep, recs)
        for mode in modes:
            v, trace = attribute_evidence(ev, judge, mode)
            out["verdicts"][mode] = {"label": v.label, "source": v.source, "non_repudiable": v.non_repudiable,
                                     "partial": v.partial, "confidence": v.confidence, "method": v.method,
                                     "arg_provenance": v.arg_provenance, "trace": trace,
                                     "judge": (v.judge or {}).get("rationale") or (v.judge or {}).get("error")}
        out["action_seq"] = act.seq
    await env.client.aclose()
    return out


def score(results: list[dict], mode: str) -> dict:
    occ = [r for r in results if r["occurred"] and mode in r["verdicts"]]
    cm: dict[str, Counter] = defaultdict(Counter)
    per_class: dict[str, dict] = {}
    fields = {"label": 0, "source": 0, "non_repudiable": 0, "partial": 0, "all": 0}
    for r in occ:
        e, v = r["expected"], r["verdicts"][mode]
        cm[e["label"]][v["label"]] += 1
        ok = {"label": v["label"] == e["label"], "source": source_ok(e["source"], v["source"]),
              "non_repudiable": v["non_repudiable"] == bool(e["non_repudiable"]),
              "partial": v["partial"] == bool(e["partial"])}
        ok["all"] = all(ok.values())
        r.setdefault("correct", {})[mode] = ok
        for k in fields:
            fields[k] += ok[k]
        c = per_class.setdefault(r["class"], {"n": 0, "label": 0, "all": 0})
        c["n"] += 1
        c["label"] += ok["label"]
        c["all"] += ok["all"]
    n = len(occ)
    prf = {}
    for lab in LABELS:
        tp = cm[lab][lab]
        fp = sum(cm[e][lab] for e in cm if e != lab)
        fn = sum(cm[lab][p] for p in cm[lab] if p != lab)
        if tp + fp + fn:
            prf[lab] = {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                        "recall": round(tp / (tp + fn), 3) if tp + fn else None, "support": tp + fn}
    return {"mode": mode, "n_attributed": n,
            "accuracy": {k: (round(v / n, 3) if n else None) for k, v in fields.items()},
            "correct": fields, "per_class": per_class, "per_label": prf,
            "confusion": {e: dict(cm[e]) for e in LABELS if cm[e]}}


def attack_rates(results: list[dict]) -> dict:
    out = {}
    for c in CLASSES:
        rs = [r for r in results if r["class"] == c]
        if rs:
            out[c] = {"occurred": sum(r["occurred"] for r in rs), "total": len(rs),
                      "errors": sum(1 for r in rs if r["error"])}
    return out


def render(report: dict) -> str:
    lines = [f"# Atribución — upstream {report['upstream']} ({report['n']} escenarios, {report['runs']} corrida/s)", ""]
    lines.append("Acción esperada ocurrió (por clase): " + ", ".join(
        f"{c} {v['occurred']}/{v['total']}" for c, v in report["attack_rates"].items()))
    lines.append(f"Integridad de la bitácora OK: {report['integrity_ok']}/{report['n']}")
    for sc in report["scores"]:
        a = sc["accuracy"]
        lines += ["", f"## Modo {sc['mode']} — {sc['n_attributed']} acciones atribuidas",
                  f"label {sc['correct']['label']}/{sc['n_attributed']} ({a['label']}) · "
                  f"source {sc['correct']['source']} ({a['source']}) · "
                  f"non_repudiable {sc['correct']['non_repudiable']} ({a['non_repudiable']}) · "
                  f"partial {sc['correct']['partial']} ({a['partial']}) · todo {sc['correct']['all']} ({a['all']})",
                  "", "| clase | n | etiqueta | todo |", "|---|---|---|---|"]
        for c in CLASSES:
            if c in sc["per_class"]:
                pc = sc["per_class"][c]
                lines.append(f"| {c} | {pc['n']} | {pc['label']}/{pc['n']} | {pc['all']}/{pc['n']} |")
        labs = [l for l in LABELS if l in sc["confusion"] or any(l in row for row in sc["confusion"].values())]
        lines += ["", "Matriz de confusión (filas = verdad, columnas = veredicto):", "",
                  "| verdad \\ veredicto | " + " | ".join(labs) + " |", "|---" * (len(labs) + 1) + "|"]
        for e in labs:
            row = sc["confusion"].get(e, {})
            lines.append(f"| {e} | " + " | ".join(str(row.get(p, 0)) for p in labs) + " |")
    wrong = [(r["id"], m, r["verdicts"][m]["label"], r["verdicts"][m]["source"], r["verdicts"][m]["trace"].get("rule"))
             for r in report["results"] for m, ok in (r.get("correct") or {}).items() if not ok["all"]]
    if wrong:
        lines += ["", "Fallos:"] + [f"- {i} [{m}]: {l} {s} ({rule})" for i, m, l, s, rule in wrong]
    return "\n".join(lines) + "\n"


async def main_async(a) -> dict:
    profiles = load_profiles(a.config)
    upstream = None
    model = "fake-model"
    if a.upstream != "fake":
        from openwitness.llm.config import make_upstream
        prof = profiles[a.upstream]
        upstream = make_upstream(prof)
        model = prof.model
    judge = make_client(profiles[a.judge]) if a.judge else None
    modes = ("rules", "rules-no-r2b") + (("judge", "rules+judge") if judge else ())
    scen = load_scenarios(a.only)
    results = []
    for k in range(a.runs):
        for s in scen:
            r = await run_one(s, upstream, model, judge, modes, a.temperature)
            r["run"] = k
            results.append(r)
            v = r["verdicts"].get("rules", {})
            print(f"{s['id']:22s} ocurrió={r['occurred']!s:5s} integ={r['integrity_ok']!s:5s} "
                  f"{v.get('label', '-'):16s} {str(v.get('source', '-')):40s} {r['seconds']:6.1f}s"
                  + (f"  ERROR {r['error'][:80]}" if r["error"] else ""), flush=True)
    report = {"upstream": a.upstream, "model": model, "judge": a.judge, "runs": a.runs, "n": len(results),
              "integrity_ok": sum(r["integrity_ok"] for r in results), "attack_rates": attack_rates(results),
              "results": results}
    report["scores"] = [score(results, m) for m in modes]
    return report


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default="fake", help="perfil de owit.toml (fake | lmstudio | lmstudio_adrian …)")
    ap.add_argument("--judge", default=None, help="perfil del juez (G10); activa los modos judge y rules+judge")
    ap.add_argument("--config", default="owit.toml")
    ap.add_argument("--only", default=None, help="una clase o un id de escenario")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--out", default="results")
    ap.add_argument("--tag", default=None, help="sufijo del archivo de salida")
    a = ap.parse_args(argv)
    report = asyncio.run(main_async(a))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = a.tag or ("" if a.upstream == "fake" and not a.judge else
                    "_" + "_".join(x for x in (a.upstream if a.upstream != "fake" else "", a.judge and "judge") if x))
    (out / f"attribution{tag}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    txt = render(report)
    (out / f"attribution{tag}.txt").write_text(txt, encoding="utf-8")
    print(txt)
    return report


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(0 if main() else 1)
