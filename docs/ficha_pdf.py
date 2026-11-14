"""Exporta `docs/ficha_tecnica.md` a `docs/ficha_tecnica.pdf` (SPEC §13.3: máximo 2 páginas y 10 MB).

    uv run python docs/ficha_pdf.py [--paper Letter|A4]

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
/* Estilo de la plantilla DefAcc 2026: títulos con serifa, texto sin serifa, tablas claras y aire. */
@page { size: %(paper)s; margin: 1.7cm 1.9cm; }
html { font-family: "Helvetica Neue", "Inter", Arial, sans-serif; font-size: 9.6pt; line-height: 1.38; color: #1d1d1d; }
body { margin: 0; max-width: none; padding: 0; }
h1, h2 { font-family: "Iowan Old Style", Charter, Georgia, serif; font-weight: 500; color: #1d1d1d;
         letter-spacing: -.01em; break-after: avoid; }
h1 { font-size: 25pt; margin: .05em 0 .1em; line-height: 1.1; }
h2 { font-size: 14pt; margin: .75em 0 .2em; }
p.kicker { font-style: italic; font-size: 10.5pt; margin: 0; color: #333; }
p.tagline { font-style: italic; font-size: 11pt; margin: 0 0 .5em; line-height: 1.3; }
p { margin: .3em 0; }
a { color: inherit; text-decoration: none; }
ol, ul { margin: .25em 0; padding-left: 1.35em; }
li { margin: .12em 0; }
blockquote { margin: .4em 0; padding: .15em 0 .15em .9em; border-left: 3px solid #d6d6d6; }
blockquote p { margin: 0; }
code { font-family: "SF Mono", Menlo, monospace; font-size: .88em; background: #f2f2f2; padding: 0 .2em;
       border-radius: 2px; }
table { border-collapse: collapse; width: 100%%; margin: .5em 0 .3em; font-size: .94em; }
tr { break-inside: avoid; }
th, td { border: 1px solid #e3e3df; padding: .3em .5em; vertical-align: top; text-align: left; }
th { background: #f2f2f2; font-weight: 400; color: #555; }
figure { margin: .35em 0 .1em; break-inside: avoid; }
figure img { max-width: 100%%; display: block; margin: 0 auto; }
figcaption { font-size: .9em; line-height: 1.32; margin-top: .3em; color: #333; }
.refs { border-top: 1px solid #ddd; margin-top: .8em; }
.refs h2 { font-size: 11pt; margin: .5em 0 .15em; }
.refs ol { font-size: 7.6pt; line-height: 1.3; color: #444; margin: 0; }
.refs li { margin: 0; }
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
    ap.add_argument("--paper", default="Letter", choices=["A4", "Letter"])   # la plantilla es Letter
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
