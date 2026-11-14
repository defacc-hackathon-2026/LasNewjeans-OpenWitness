"""Punto de entrada del comando `owit` (SPEC §7.10).

`owit hook <prompt|pre-tool|session-start>` lo invoca el agente en cada prompt y antes de cada herramienta, así
que su costo es arranque de Python + una llamada al gateway (§9.4). Typer y sus dependencias tardan más en
cargarse que todo lo demás, por eso los hooks van directo a `integrations.hooks`, que solo usa la librería
estándar (y PyNaCl para firmar). El resto de los comandos pasa por el CLI de `cli.py`.
"""
from __future__ import annotations

import sys

HOOK_KINDS = ("prompt", "pre-tool", "session-start")


def main() -> None:
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "hook" and args[1] in HOOK_KINDS:
        from .integrations.hooks import main as hook_main
        sys.exit(hook_main(args[1]))
    from .cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
