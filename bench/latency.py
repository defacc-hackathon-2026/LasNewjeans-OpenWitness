"""Bench §9.4: latencia agregada por el gateway (upstream `fake` por HTTP, respuesta fija, localhost).

Mide, con y sin gateway, el tiempo hasta el primer byte y el total por petición (p50, p95, p99), el costo
interno de `append()`, del filtrado y de la descomposición, el anclaje (asíncrono, aparte) y la latencia de
un hook (arranque de Python + llamada al gateway).

    uv run python -m bench.latency --n 1000 --out results/
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from openwitness.gateway.server import Gateway
from openwitness.identity import KeyPair, KeyRegistry
from openwitness.ledger import MockAnchor
from openwitness.llm.anthropic_upstream import AnthropicUpstream
from openwitness.llm.base import LLMProfile
from openwitness.llm.fake import fake_app
from openwitness.llm.openai_upstream import OpenAIUpstream
from openwitness.serve import ServerThread

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "claude_code" / "2.1.323" / "no-tool-search"
CODEX_FIXTURE = ROOT / "tests" / "fixtures" / "codex" / "0.169.0" / "hooks"
# caso -> (ruta del LLM, protocolo de la sesión, upstream)
ANTHROPIC = ("/v1/messages", "anthropic_messages", "fake_http")
RESPONSES = ("/v1/responses", "openai_responses", "fake_http_openai")


def pct(xs: list[float]) -> dict:
    if not xs:
        return {}
    s = sorted(xs)
    q = lambda p: s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]  # noqa: E731
    return {"n": len(s), "p50": round(q(50), 3), "p95": round(q(95), 3), "p99": round(q(99), 3),
            "mean": round(statistics.fmean(s), 3)}


def timed_stream(client: httpx.Client, url: str, body: dict, headers: dict) -> tuple[float, float]:
    t0 = time.perf_counter()
    with client.stream("POST", url, json=body, headers=headers) as r:
        it = r.iter_raw()
        next(it)
        ttfb = time.perf_counter() - t0
        for _ in it:
            pass
    return ttfb * 1000, (time.perf_counter() - t0) * 1000


def bodies() -> dict[str, tuple[dict, tuple[str, str, str]]]:
    small = {"model": "fake-model", "max_tokens": 256, "stream": True,
             "system": "Eres un agente de prueba.",
             "messages": [{"role": "user", "content": "Resume los issues abiertos del proyecto demo"}],
             "tools": [{"name": "mcp__issues__list_issues", "description": "Lista issues.",
                        "input_schema": {"type": "object", "properties": {"project": {"type": "string"}}}}]}
    out = {"refagent_small": (small, ANTHROPIC)}
    reqs = sorted(FIXTURE.glob("*_request.json"))
    if reqs:
        cc = json.loads(reqs[0].read_text())
        cc["stream"] = True
        out["claude_code_fixture"] = (cc, ANTHROPIC)
    reqs = sorted(CODEX_FIXTURE.glob("*_request.json"))
    if reqs:                                        # Codex por la Responses API (§17.4)
        cx = json.loads(reqs[0].read_text())
        cx["stream"] = True
        out["codex_fixture"] = (cx, RESPONSES)
    return out


def hook_latency(gw_url: str, sid: str, token: str, n: int = 20) -> dict:
    """Con `owit hook pre-tool` de Juliana si existe; si no, un proceso Python que hace lo mismo que el hook."""
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "x"},
                          "tool_use_id": "toolu_x", "session_id": "cc"})
    env = {"OWIT_GATEWAY": gw_url, "OWIT_SESSION": sid, "OWIT_TOKEN": token, "PATH": str(Path(sys.executable).parent)}
    try:
        import openwitness.integrations.hooks  # noqa: F401
        cmd = [str(Path(sys.executable).parent / "owit"), "hook", "pre-tool"]
        mode = "owit hook pre-tool"
    except ImportError:
        code = ("import sys,json,os,httpx; j=json.load(sys.stdin); "
                "httpx.post(os.environ['OWIT_GATEWAY']+'/s/'+os.environ['OWIT_SESSION']+'/tool-check', "
                "json={'tool': j['tool_name'], 'args': j['tool_input'], 'tool_use_id': j['tool_use_id']}, "
                "headers={'x-api-key': os.environ['OWIT_TOKEN']})")
        cmd = [sys.executable, "-c", code]
        mode = "aproximación: python + httpx + POST tool-check (sin owit hook todavía)"
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run(cmd, input=payload, text=True, env=env, capture_output=True, timeout=30)
        xs.append((time.perf_counter() - t0) * 1000)
    return {"mode": mode, **pct(xs)}


def run(n: int, work: Path) -> dict:
    fake = ServerThread(fake_app()).start()
    up = AnthropicUpstream(LLMProfile("fake_http", "anthropic_compat", model="", base_url=fake.url,
                                      force_model=False))
    anchor = MockAnchor(work / "anchor" / "mock_anchor.jsonl")
    up_oa = OpenAIUpstream(LLMProfile("fake_http_openai", "openai_compat", model="", base_url=fake.url,
                                      protocols=["responses"], force_model=False))
    gw = Gateway(work, KeyPair.generate(), KeyRegistry(), upstreams={"fake_http": up, "fake_http_openai": up_oa},
                 default_upstream="fake_http", anchor=anchor)
    gws = ServerThread(gw.app()).start()
    out: dict = {"n": n, "env": {"python": sys.version.split()[0], "platform": sys.platform, "machine": platform.machine(),
                                  "cpus": os.cpu_count()}, "cases": {}}
    with httpx.Client(timeout=30) as c:
        for name, (body, (path, protocol, upstream)) in bodies().items():
            j = c.post(gws.url + "/sessions", json={"user_id": "alice", "agent": "bench", "protocol": protocol,
                                                    "upstream": upstream}).json()
            h = {"x-api-key": j["token"], "anthropic-version": "2023-06-01"}
            for _ in range(20):   # calentamiento
                timed_stream(c, fake.url + path, body, {})
                timed_stream(c, gws.url + f"/s/{j['sid']}{path}", body, h)
            gw.timings.clear()
            direct, via = ([], []), ([], [])
            for _ in range(n):      # intercalado para que el ruido afecte igual a ambos
                a = timed_stream(c, fake.url + path, body, {})
                b = timed_stream(c, gws.url + f"/s/{j['sid']}{path}?beta=true", body, h)
                direct[0].append(a[0]); direct[1].append(a[1]); via[0].append(b[0]); via[1].append(b[1])
            led = gw.sessions[j["sid"]].ledger
            out["cases"][name] = {
                "body_bytes": len(json.dumps(body)), "protocol": protocol,
                "ttfb_ms": {"direct": pct(direct[0]), "gateway": pct(via[0]),
                            "added_p50": round(pct(via[0])["p50"] - pct(direct[0])["p50"], 3)},
                "total_ms": {"direct": pct(direct[1]), "gateway": pct(via[1]),
                             "added_p50": round(pct(via[1])["p50"] - pct(direct[1])["p50"], 3)},
                "internal_ms": {"append": pct(led.append_times_ms),
                                "filter": pct([t["filter_ms"] for t in gw.timings]),
                                "decompose": pct([t["decompose_ms"] for t in gw.timings]),
                                "response": pct([t.get("response_ms", 0) for t in gw.timings])},
                "entries": len(led.records),
            }
            c.post(gws.url + f"/s/{j['sid']}/end", headers=h, json={})
        t_anchor = []
        for _ in range(50):
            led.append("user_text_unsigned", {"type": "agent", "id": "bench"}, {"text": "x"}, [], {"reason": "x"})
            t0 = time.perf_counter()
            led.force_anchor()
            t_anchor.append((time.perf_counter() - t0) * 1000)
        out["anchor_ms_async"] = {"backend": "mock", **pct(t_anchor)}
        j = c.post(gws.url + "/sessions", json={"user_id": "alice", "agent": "bench"}).json()
        out["hook_ms"] = hook_latency(gws.url, j["sid"], j["token"])
    gws.stop()
    fake.stop()
    return out


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--out", default="results")
    a = ap.parse_args(argv)
    res = run(a.n, Path(tempfile.mkdtemp(prefix="owit-lat-")))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latency.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    lines = [f"{name} ({c['body_bytes']} B): +{c['ttfb_ms']['added_p50']} ms TTFB p50, "
             f"+{c['total_ms']['added_p50']} ms total p50 · append p50 {c['internal_ms']['append']['p50']} ms · "
             f"descomposición p50 {c['internal_ms']['decompose']['p50']} ms" for name, c in res["cases"].items()]
    lines.append(f"ancla (asíncrona) p50 {res['anchor_ms_async']['p50']} ms · hook p50 {res['hook_ms']['p50']} ms "
                 f"({res['hook_ms']['mode']})")
    lines.append(f"máquina: {res['env']}")
    (out / "latency.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return res


if __name__ == "__main__":
    main()
