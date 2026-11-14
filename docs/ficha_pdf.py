"""Exporta `docs/ficha_tecnica.md` a `docs/ficha_tecnica.pdf` (SPEC §13.3: máximo 2 páginas y 10 MB).

    uv run python docs/ficha_pdf.py [--paper A4|Letter]

pandoc convierte el Markdown en HTML autocontenido (con la imagen embebida) y Chromium lo imprime a PDF con
Playwright de Node (`npx playwright` o el que ya esté instalado); si no hay Playwright, usa Chrome o Chromium en
modo headless (`--print-to-pdf`, o la ruta en `$CHROME`). Si hay LaTeX, también sirve:
`pandoc docs/ficha_tecnica.md -o docs/ficha_tecnica.pdf --pdf-engine=xelatex -V mainfont="DejaVu Sans"`.

Al final cuenta las páginas del PDF y falla si pasa de 2 páginas o de 10 MB.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DOCS = Path(__file__).resolve().parent
MAX_PAGES, MAX_BYTES = 2, 10 * 1024 * 1024

CSS = """
@page { size: %(paper)s; margin: 1.15cm 1.3cm; }
html { font-family: "DejaVu Sans", "Helvetica Neue", Arial, sans-serif; font-size: 8.5pt; line-height: 1.32;
       color: #111; }
body { margin: 0; max-width: none; padding: 0; }
header#title-block-header { margin: 0 0 .35em; border-bottom: 1.5px solid #333; padding-bottom: .25em; }
h1.title { font-size: 14pt; margin: 0; }
p.subtitle { font-size: 8pt; margin: .1em 0 0; color: #333; }
h2 { font-size: 10pt; margin: .55em 0 .2em; color: #1a3d6d; break-after: avoid; }
p { margin: .25em 0; text-align: justify; hyphens: auto; }
ol, ul { margin: .2em 0; padding-left: 1.4em; }
li { margin: .08em 0; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: .92em; background: #f1f1f1; padding: 0 .15em;
       border-radius: 2px; }
table { border-collapse: collapse; width: 100%%; margin: .3em 0; break-inside: avoid; }
th, td { border: .5px solid #999; padding: .12em .35em; vertical-align: top; text-align: left; }
th { background: #e6ecf4; }
figure { margin: .3em auto; text-align: center; break-inside: avoid; }
figure img { max-width: 100%%; }
figcaption { display: none; }
"""

PRINT_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const [html, pdf, paper] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto('file://' + html, { waitUntil: 'load' });
  await p.pdf({ path: pdf, format: paper, printBackground: true, preferCSSPageSize: true });
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


CHROMES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "/Applications/Chromium.app/Contents/MacOS/Chromium", "google-chrome", "chromium", "chromium-browser")


def chrome() -> str | None:
    for c in ([os.environ["CHROME"]] if os.environ.get("CHROME") else []) + list(CHROMES):
        found = c if Path(c).exists() else shutil.which(c)
        if found:
            return found
    return None


def print_pdf(tmp: Path, html: Path, out: Path, paper: str) -> None:
    """Playwright si está; si no, Chrome headless (respeta el `@page` del CSS)."""
    js = tmp / "print.js"
    js.write_text(PRINT_JS)
    r = subprocess.run(["node", str(js), str(html), str(out), paper], capture_output=True, text=True)
    if r.returncode == 0:
        return
    exe = chrome()
    if exe is None:
        sys.stderr.write(r.stderr)
        raise SystemExit("no hay Playwright ni Chrome/Chromium para imprimir el PDF")
    subprocess.run([exe, "--headless", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={out}",
                    f"--user-data-dir={tmp / 'chrome'}", "file://" + str(html)],
                   check=True, capture_output=True)


def count_pages(pdf: Path) -> int:
    # Sin dependencias: cuenta los objetos /Type /Page (no /Pages) del PDF de Chromium.
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf.read_bytes()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DOCS / "ficha_tecnica.md")
    ap.add_argument("--out", type=Path, default=DOCS / "ficha_tecnica.pdf")
    ap.add_argument("--paper", default="A4", choices=["A4", "Letter"])
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="owit-ficha-") as tmp:
        css, html = Path(tmp) / "ficha.css", Path(tmp) / "ficha.html"
        css.write_text(CSS % {"paper": a.paper})
        subprocess.run(["pandoc", str(a.src), "-o", str(html), "--standalone", "--embed-resources",
                        f"--resource-path={a.src.parent}", "--css", str(css), "--metadata", "lang=es",
                        "--metadata", "pagetitle=Open Witness"], check=True)
        print_pdf(Path(tmp), html, a.out.resolve(), a.paper)
    pages, size = count_pages(a.out), a.out.stat().st_size
    ok = pages <= MAX_PAGES and size <= MAX_BYTES
    print(f"{a.out}: {pages} páginas · {size / 1024:.0f} KB · {'OK' if ok else 'NO CUMPLE (máx. 2 páginas, 10 MB)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
