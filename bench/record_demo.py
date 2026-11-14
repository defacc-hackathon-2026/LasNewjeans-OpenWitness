"""Graba `demo/demo.sh` en `docs/demo.cast` (asciicast v2), sin depender de asciinema (SPEC §13.1).

    uv run python -m bench.demo && uv run python -m bench.record_demo [--pause 1.5] [--read 4] [--full]

Corre el script en una pseudo-terminal y guarda cada bloque de salida con su tiempo. Se reproduce con
`asciinema play docs/demo.cast` o en la web con asciinema-player. Con asciinema instalado da lo mismo:
`asciinema rec docs/demo.cast -c "PAUSE=1.5 READ=4 FULL=1 bash demo/demo.sh"`.
"""
from __future__ import annotations

import argparse
import json
import os
import pty
import select
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def record(cmd: list[str], out: Path, env: dict, cols: int = 150, rows: int = 40) -> int:
    master, slave = pty.openpty()
    try:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (ImportError, OSError):
        pass
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env, cwd=ROOT,
                         close_fds=True)
    os.close(slave)
    t0 = time.monotonic()
    events: list[list] = []
    while True:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if not data:
                break
            events.append([round(time.monotonic() - t0, 3), "o", data.decode("utf-8", "replace")])
        elif p.poll() is not None:
            break
    p.wait()
    os.close(master)
    header = {"version": 2, "width": cols, "height": rows, "timestamp": int(time.time()),
              "title": "Open Witness — demo (SPEC §13.1)", "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"}}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "demo.cast")
    ap.add_argument("--pause", default="1.5", help="segundos antes de cada comando")
    ap.add_argument("--read", default="4", help="segundos para leer la salida de cada comando")
    ap.add_argument("--full", action="store_true", help="incluye la reescritura con la llave del gateway")
    a = ap.parse_args(argv)
    if not (ROOT / "demo" / "demo.sh").exists():
        raise SystemExit("falta demo/demo.sh: corre antes `uv run python -m bench.demo`")
    env = os.environ | {"PAUSE": a.pause, "READ": a.read, "FULL": "1" if a.full else "0", "TERM": "xterm-256color"}
    env.pop("UV_NATIVE_TLS", None)  # evita el aviso de uv en la grabación
    code = record(["bash", "demo/demo.sh"], a.out, env)
    print(f"{a.out} ({a.out.stat().st_size // 1024} KB) · salida de demo.sh: {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
