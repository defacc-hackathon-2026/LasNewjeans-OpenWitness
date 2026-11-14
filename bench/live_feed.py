"""Sesiones en vivo para el monitor (SPEC §17.5): corre escenarios de `bench/scenarios/` despacio, a través del
gateway en proceso, escribiendo en un `data/` existente (el de `bench.demo`), para ver cómo llegan las entradas.

    uv run python -m bench.live_feed --data demo/data --keys demo/keys [--only subtle_injection] [--delay 1.5]

Usa la llave del gateway, el registro, las llaves de alice y mallory y el ancla que ya están en esas carpetas, así
que `owit verify` y el monitor verifican estas sesiones igual que las de la demo.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from openwitness.gateway.server import Gateway
from openwitness.identity import KeyPair, KeyRegistry
from openwitness.ledger import MockAnchor
from openwitness.llm.base import LLMProfile
from openwitness.llm.fake import FakeUpstream
from openwitness.llm.naive import NaiveResponder
from openwitness.refagent import InProcessToolbox, RefAgent
from openwitness.refagent.agent import in_process

from .run_bench import load_scenarios


class SlowToolbox(InProcessToolbox):
    def __init__(self, fixtures: dict, delay: float):
        super().__init__(fixtures)
        self.delay = delay

    async def call(self, name: str, args: dict):
        await asyncio.sleep(self.delay)
        return await super().call(name, args)


def slow(responder, delay: float):
    def r(body):
        time.sleep(delay)          # el "modelo" piensa; bloquea solo el hilo del feed
        return responder(body)
    return r


async def feed(data: Path, keys: Path, scenarios: list[dict], delay: float = 1.5, loop: bool = False,
               stop=None) -> int:
    gk = KeyPair.load(data / "gateway.key")
    reg = KeyRegistry.load(keys / "registry.json")
    users = {p.stem: KeyPair.load(p) for p in keys.glob("*.key")}
    anchor = MockAnchor(data / "anchor" / "mock_anchor.jsonl")
    n = 0
    while True:
        for s in scenarios:
            if stop is not None and stop.is_set():
                return n
            if s["user"] not in users:
                continue
            up = FakeUpstream(LLMProfile("fake", "fake", model="fake-model"),
                              responder=slow(NaiveResponder(s["fake_script"]), delay / 2))
            gw = Gateway(data, gk, reg, upstreams={"fake": up}, default_upstream="fake", anchor=anchor,
                         anchor_every=6)
            http, sdk = in_process(gw.app())
            agent = RefAgent("http://gw", SlowToolbox(s.get("tool_fixtures") or {}, delay / 2), user=s["user"],
                             key=users[s["user"]], http=http, sdk_http=sdk)
            await agent.run(s["user_prompt"], signed=bool(s.get("signed", True)))
            await gw.drain()
            await http.aclose()
            n += 1
            await asyncio.sleep(delay)
        if not loop:
            return n


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="demo/data")
    ap.add_argument("--keys", default="demo/keys")
    ap.add_argument("--only", default=None, help="una clase o un id de escenario")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args(argv)
    n = asyncio.run(feed(Path(a.data), Path(a.keys), load_scenarios(a.only), a.delay, a.loop))
    print(f"{n} sesiones escritas en {a.data}")


if __name__ == "__main__":
    main()
