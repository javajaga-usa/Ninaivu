"""The application guide as one colourful PDF, from the docs site's pages.

    python tools/build_guide_pdf.py [out.pdf]
    python tools/build_guide_pdf.py --lang ta [out.pdf]    # the Tamil guide
    python tools/build_guide_pdf.py --lang admin [out.pdf] # the technical administrator guide


Takes docs/site/*.md in reading order, renders them as an A4 booklet — a
cover, contents, one colour per chapter, the screenshots inline — and prints
it with Chromium (Playwright). ``--lang ta`` takes docs/site/ta/*.md, the
same pages in Tamil, with a Tamil cover and contents. ``--lang admin`` takes
docs/admin-guide.md, the technical page (English only) that the household
guides leave out: Docker, a checkout, the command line. Needs ``markdown`` and ``playwright`` with its
Chromium; nothing else. The pictures are docs/screens/*.jpg, the same ones
the site uses.
"""
from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

from markdown import markdown

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"
SCREENS = ROOT / "docs" / "screens"

PAGES = [
    ("index.md", "#e8590c"), ("install.md", "#0b7285"), ("first-day.md", "#2b8a3e"),
    ("family-and-roles.md", "#6741d9"), ("backup.md", "#c92a2a"), ("remote-access.md", "#1971c2"),
    ("ai.md", "#e67700"), ("troubleshooting.md", "#5c5f66"), ("guide-family.md", "#0ca678"),
    ("guide-console.md", "#7048e8"),
]

# Everything on the cover, the contents and the page footer, per language.
LANGUAGES = {
    "en": {
        "site": SITE,
        "home": "../index.md",   # the English home is the site's front page, docs/index.md
        "kickers": ["Ninaivu 1.0.2", *(f"Chapter {n}" for n in range(1, 8)),
                    "The guide · Part one", "The guide · Part two"],
        "title": "Ninaivu — the application guide",
        "footer": "Ninaivu · the application guide",
        "name": "Ninaivu", "under": "நினைவு · memory",
        "tag": "Your family's photographs, at home.",
        "sub": "The application guide · version 1.0.2",
        "parts": [("Ninaivu", "the library"), ("Mugil", "backup"), ("Sudar", "the photo studio")],
        "contents": "Contents",
        "fine": "The pictures use a generated sample library, not anybody's photographs. Everything "
                "here runs on a computer you own; nothing leaves the house unless you choose where it goes.",
    },
    "ta": {
        "site": SITE / "ta",
        "home": "index.md",
        "kickers": ["நினைவு 1.0.2", *(f"அத்தியாயம் {n}" for n in range(1, 8)),
                    "வழிகாட்டி · பகுதி ஒன்று", "வழிகாட்டி · பகுதி இரண்டு"],
        "title": "நினைவு — பயன்பாட்டு வழிகாட்டி",
        "footer": "நினைவு · பயன்பாட்டு வழிகாட்டி",
        "name": "நினைவு", "under": "Ninaivu · memory",
        "tag": "உங்கள் குடும்பப் புகைப்படங்கள், உங்கள் வீட்டிலேயே.",
        "sub": "பயன்பாட்டு வழிகாட்டி · பதிப்பு 1.0.2",
        "parts": [("நினைவு", "நூலகம்"), ("முகில்", "காப்புப்பிரதி"), ("சுடர்", "புகைப்பட ஸ்டுடியோ")],
        "contents": "பொருளடக்கம்",
        "fine": "படங்கள் உருவாக்கப்பட்ட ஒரு மாதிரி நூலகத்தைப் பயன்படுத்துகின்றன; யாருடைய புகைப்படங்களும் "
                "அல்ல. இங்குள்ள அனைத்தும் நீங்கள் வைத்திருக்கும் ஒரு கணினியில் இயங்குகின்றன; நீங்கள் "
                "தேர்ந்தெடுக்காத இடத்துக்கு எதுவும் வீட்டை விட்டு வெளியே போவதில்லை. கட்டளைகளும் "
                "அமைப்புகளின் பெயர்களும் ஆங்கிலத்தில் உள்ளன — நீங்கள் தட்டச்சு செய்வது அவைதான்.",
    },
    "admin": {
        "site": ROOT / "docs",
        "pages": [("admin-guide.md", "#495057")],
        "kickers": ["Ninaivu 1.0.2"],
        "title": "Ninaivu — technical administrator guide",
        "footer": "Ninaivu · technical administrator guide",
        "name": "Ninaivu", "under": "நினைவு · memory",
        "tag": "For whoever looks after the technical side.",
        "sub": "Technical administrator guide · version 1.0.2",
        "parts": [("Docker", "on a NAS"), ("Source", "a checkout"), ("Command line", "flags and tools")],
        "contents": "Contents",
        "fine": "",
        "html_lang": "en",
    },
}

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm;
  @bottom-center { content: "__FOOTER__"; font: 9px system-ui; color: #888 }
  @bottom-right { content: counter(page); font: 9px system-ui; color: #888 } }
* { box-sizing: border-box }
body { font: 11pt/1.5 "Segoe UI", system-ui, -apple-system, "Noto Sans Tamil", "Tamil Sangam MN",
  "Nirmala UI", "Latha", sans-serif; color: #222; margin: 0 }
.cover { height: 250mm; background: linear-gradient(160deg, #ff922b, #e8590c 45%, #862e9c); color: #fff;
  border-radius: 12px; padding: 30mm 22mm; page-break-after: always; display: flex; flex-direction: column }
.cover img { width: 34mm; border-radius: 20%; box-shadow: 0 8px 30px rgba(0,0,0,.3); border: 0 }
.cover h1 { font-size: 54pt; margin: 14mm 0 0; letter-spacing: -1px; border: 0; color: #fff }
.tamil { font-size: 20pt; opacity: .9 }
.cover .tag { font-size: 20pt; margin: 12mm 0 2mm; font-weight: 600 } .cover .sub { font-size: 12pt; opacity: .85 }
.parts { margin-top: auto; display: flex; gap: 6mm }
.parts div { background: rgba(255,255,255,.18); border-radius: 8px; padding: 5mm; flex: 1; font-size: 10pt }
.parts b { display: block; font-size: 14pt; color: #fff }
.toc { page-break-after: always } .toc ol { list-style: none; padding: 0 }
.toc li { display: flex; align-items: center; gap: 5mm; padding: 3.5mm 0 3.5mm 4mm; border-bottom: 1px solid #eee;
  font-size: 13pt; font-weight: 600; border-left: 5px solid var(--c); margin-bottom: 2mm }
.toc li span { color: var(--c); font-size: 9pt; text-transform: uppercase; letter-spacing: .08em; min-width: 36mm; font-weight: 700 }
.fine { color: #777; font-size: 9.5pt; margin-top: 10mm }
.chapter { page-break-before: always }
.kicker { color: var(--c); font-weight: 700; text-transform: uppercase; letter-spacing: .1em; font-size: 9.5pt }
h1 { font-size: 28pt; margin: 2mm 0 5mm; color: #111; border-bottom: 4px solid var(--c); padding-bottom: 3mm }
h2 { font-size: 16pt; color: var(--c); margin: 9mm 0 2mm; page-break-after: avoid }
h3 { font-size: 12.5pt; margin: 6mm 0 1.5mm; page-break-after: avoid }
p { margin: 0 0 3mm }
img { max-width: 100%; border-radius: 6px; box-shadow: 0 2px 12px rgba(0,0,0,.18); margin: 2mm 0 4mm;
  page-break-inside: avoid; border: 1px solid #ddd }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; margin: 2mm 0 5mm; page-break-inside: avoid }
th { background: var(--c); color: #fff; text-align: left; padding: 2mm 3mm }
td { padding: 2mm 3mm; border-bottom: 1px solid #e6e6e6; vertical-align: top } tr:nth-child(even) td { background: #fafafa }
code { background: #f1f3f5; padding: 0 1mm; border-radius: 3px; font-size: 9.5pt }
pre { background: #1e1e28; color: #f8f8f2; padding: 3mm 4mm; border-radius: 6px; font-size: 9pt; white-space: pre-wrap; page-break-inside: avoid }
pre code { background: none; color: inherit; padding: 0 }
.note { border-left: 5px solid var(--c); background: color-mix(in srgb, var(--c) 8%, white); padding: 3mm 4mm;
  border-radius: 0 6px 6px 0; margin: 3mm 0 5mm; page-break-inside: avoid }
.note b { display: block; color: var(--c); margin-bottom: 1mm }
ul, ol { padding-left: 6mm } li { margin-bottom: 1.2mm } strong { color: #111 }
.chapter > p:first-of-type { font-size: 12pt; color: #444 }
"""


def data_uri(path: Path) -> str:
    kind = "png" if path.suffix == ".png" else "jpeg"
    return f"data:image/{kind};base64," + base64.b64encode(path.read_bytes()).decode()


def admonitions(md: str) -> str:
    """MkDocs's ``!!! note "Title"`` blocks as plain boxes."""
    def box(match):
        title = match.group(2) or ""
        body = markdown(re.sub(r"^    ", "", match.group(3), flags=re.M))
        head = f"<b>{title}</b>" if title else ""
        return f'\n<div class="note">{head}{body}</div>\n'
    return re.sub(r'!!! (\w+)(?: "([^"]*)")?\n((?:    .*\n?)+)', box, md)


def chapter(site: Path, name: str, colour: str, kicker: str) -> str:
    md = admonitions((site / name).read_text(encoding="utf-8"))
    body = markdown(md, extensions=["tables", "fenced_code"])
    body = body.replace("\\|", "|")                                     # a pipe escaped inside a table cell
    body = re.sub(r'src="(?:\.\./)*screens/([^"]+)"', lambda m: f'src="{data_uri(SCREENS / m.group(1))}"', body)
    body = re.sub(r'<a href="[^"]*">([^<]*)</a>', r"\1", body)          # no live links on paper
    body = re.sub(r"<h1>(.*?)</h1>", rf'<div class="kicker">{kicker}</div><h1>\1</h1>', body, count=1)
    return f'<section class="chapter" style="--c:{colour}">{body}</section>'


def title_of(site: Path, name: str) -> str:
    return re.search(r"^# (.*)", (site / name).read_text(encoding="utf-8"), re.M).group(1)


def build_html(lang: str = "en") -> str:
    t = LANGUAGES[lang]
    site = t["site"]
    pages = t.get("pages", PAGES)
    chapters = [(t.get("home") if p == "index.md" else p, c, k) for (p, c), k in zip(pages, t["kickers"])]
    logo = data_uri(ROOT / "ninaivu" / "static" / "icons" / "icon-512.png")
    toc = "".join(f'<li style="--c:{c}"><span>{k}</span>{title_of(site, p)}</li>' for p, c, k in chapters[1:])
    parts = "".join(f"<div><b>{name}</b>{what}</div>" for name, what in t["parts"])
    cover = f"""<section class="cover"><img src="{logo}"><h1>{t["name"]}</h1><div class="tamil">{t["under"]}</div>
<p class="tag">{t["tag"]}</p><p class="sub">{t["sub"]}</p><div class="parts">{parts}</div></section>"""
    if toc:
        cover += f"""
<section class="toc"><h1>{t["contents"]}</h1><ol>{toc}</ol><p class="fine">{t["fine"]}</p></section>"""
    css = CSS.replace("__FOOTER__", t["footer"])
    return (f"<!doctype html><html lang={t.get('html_lang', lang)}><head><meta charset=utf-8><title>{t['title']}</title>"
            f"<style>{css}</style></head><body>{cover}{''.join(chapter(site, *c) for c in chapters)}</body></html>")


def main(out: Path, lang: str = "en") -> None:
    from playwright.sync_api import sync_playwright

    html_path = out.with_suffix(".html")
    html_path.write_text(build_html(lang), encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.resolve().as_uri())
        page.wait_for_timeout(500)
        page.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True)
        browser.close()
    html_path.unlink()
    print(out, out.stat().st_size, "bytes")


if __name__ == "__main__":
    args = sys.argv[1:]
    lang = "en"
    if args[:1] == ["--lang"]:
        lang, args = args[1], args[2:]
    default = {"en": "Ninaivu-guide.pdf", "admin": "Ninaivu-admin-guide.pdf"}.get(lang, f"Ninaivu-guide-{lang}.pdf")
    main(Path(args[0] if args else default), lang)
