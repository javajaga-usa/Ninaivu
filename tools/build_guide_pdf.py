"""The application guide as a designed PDF, from the docs site's pages.

    python tools/build_guide_pdf.py [out.pdf]
    python tools/build_guide_pdf.py --lang ta [out.pdf]    # the Tamil guide
    python tools/build_guide_pdf.py --lang admin [out.pdf] # the technical administrator guide

Takes docs/site/*.md in reading order and prints them as an A4 book with
Chromium (Playwright): a full-bleed cover, a contents page with page numbers
and links, a coloured opener for each chapter, running heads and folios,
callouts by kind, screenshots in a window frame, cross-references that say
which page they point to, bookmarks, a tagged (accessible) structure, document
properties, and a back cover. ``--lang ta`` takes docs/site/ta/*.md, the same
pages in Tamil. ``--lang admin`` takes docs/admin-guide.md, the technical page
(English only) that the household guides leave out.

The look is in tools/guide_pdf.css. Needs ``markdown``, ``playwright`` with its
Chromium and ``pypdf``, and the Noto fonts (Debian/Ubuntu: fonts-noto-core),
which carry the Tamil. The pictures are docs/screens/*.jpg, the same ones the
site uses.

Page numbers in the contents and in the cross-references come from the PDF
itself: the book is printed, the page each anchor landed on is read back, and
it is printed again with those numbers until they stop moving (twice, usually).
Set NINAIVU_CHROMIUM to use a browser other than Playwright's own.
"""
from __future__ import annotations

import argparse
import base64
import html
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from markdown import Markdown
from markdown.extensions.toc import slugify_unicode

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"
SCREENS = ROOT / "docs" / "screens"
STYLE = Path(__file__).with_name("guide_pdf.css")
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "ninaivu" / "__init__.py").read_text(encoding="utf-8"))[1]

SITE_URL = "https://javajaga-usa.github.io/Ninaivu/"
REPO_URL = "https://github.com/javajaga-usa/Ninaivu"

PAGES = [
    ("index.md", "#e8590c"), ("install.md", "#0b7285"), ("first-day.md", "#2b8a3e"),
    ("family-and-roles.md", "#6741d9"), ("backup.md", "#c92a2a"), ("remote-access.md", "#1971c2"),
    ("ai.md", "#e67700"), ("troubleshooting.md", "#5c5f66"), ("guide-family.md", "#0ca678"),
    ("guide-console.md", "#7048e8"),
]

# Everything on the cover, the contents, the callouts and the page furniture, per language.
LANGUAGES = {
    "en": {
        "site": SITE,
        "home": "../index.md",   # the English home is the site's front page, docs/index.md
        "kickers": ["Ninaivu {v}", *(f"Chapter {n}" for n in range(1, 8)),
                    "The guide · Part one", "The guide · Part two"],
        "numerals": ["", *(f"{n:02d}" for n in range(1, 8)), "A", "B"],
        "title": "Ninaivu — the application guide",
        "footer": "Ninaivu · the application guide",
        "name": "Ninaivu", "under": "நினைவு · memory",
        "tag": "Your family's photographs, at home.",
        "sub": "The application guide · version {v}",
        "parts": [("Ninaivu", "the library"), ("Mugil", "backup"), ("Sudar", "the photo studio")],
        "contents": "Contents",
        "lede": "Everything in this guide, and the page it is on.",
        "page": "p.",
        "admonitions": {"note": "Note", "tip": "Tip", "warning": "Warning", "danger": "Danger"},
        "legend": "How to read this guide",
        "keys": [("note", "Note", "Worth knowing"), ("tip", "Tip", "A quicker way"), ("warning", "Warning", "Read before you go on")],
        "fine": "The pictures use a generated sample library, not anybody's photographs. Everything "
                "here runs on a computer you own; nothing leaves the house unless you choose where it goes.",
        "links": [("The guide, online", SITE_URL), ("Download", REPO_URL + "/releases"), ("Report a problem", REPO_URL + "/issues")],
        "ver": "Ninaivu {v} · MIT licence",
        "keywords": "Ninaivu, Mugil, Sudar, family photographs, self-hosted, guide",
    },
    "ta": {
        "site": SITE / "ta",
        "home": "index.md",
        "kickers": ["நினைவு {v}", *(f"அத்தியாயம் {n}" for n in range(1, 8)),
                    "வழிகாட்டி · பகுதி ஒன்று", "வழிகாட்டி · பகுதி இரண்டு"],
        "numerals": ["", *(f"{n:02d}" for n in range(1, 8)), "அ", "ஆ"],
        "title": "நினைவு — பயன்பாட்டு வழிகாட்டி",
        "footer": "நினைவு · பயன்பாட்டு வழிகாட்டி",
        "name": "நினைவு", "under": "Ninaivu · memory",
        "tag": "உங்கள் குடும்பப் புகைப்படங்கள், உங்கள் வீட்டிலேயே.",
        "sub": "பயன்பாட்டு வழிகாட்டி · பதிப்பு {v}",
        "parts": [("நினைவு", "நூலகம்"), ("முகில்", "காப்புப்பிரதி"), ("சுடர்", "புகைப்பட ஸ்டுடியோ")],
        "contents": "பொருளடக்கம்",
        "lede": "இந்த வழிகாட்டியில் உள்ளவை, அவை இருக்கும் பக்கத்துடன்.",
        "page": "ப.",
        "admonitions": {"note": "குறிப்பு", "tip": "உதவிக்குறிப்பு", "warning": "எச்சரிக்கை", "danger": "ஆபத்து"},
        "legend": "இந்த வழிகாட்டியை எப்படிப் படிப்பது",
        "keys": [("note", "குறிப்பு", "தெரிந்துகொள்ள வேண்டியது"), ("tip", "உதவிக்குறிப்பு", "நேரத்தை மிச்சப்படுத்தும் வழி"),
                 ("warning", "எச்சரிக்கை", "தொடர்வதற்கு முன் கவனிக்க")],
        "fine": "படங்கள் உருவாக்கப்பட்ட ஒரு மாதிரி நூலகத்தைப் பயன்படுத்துகின்றன; யாருடைய புகைப்படங்களும் "
                "அல்ல. இங்குள்ள அனைத்தும் நீங்கள் வைத்திருக்கும் ஒரு கணினியில் இயங்குகின்றன; நீங்கள் "
                "தேர்ந்தெடுக்காத இடத்துக்கு எதுவும் வீட்டை விட்டு வெளியே போவதில்லை. கட்டளைகளும் "
                "அமைப்புகளின் பெயர்களும் ஆங்கிலத்தில் உள்ளன — நீங்கள் தட்டச்சு செய்வது அவைதான்.",
        "links": [("இணையத்தில் வழிகாட்டி", SITE_URL + "ta/"), ("பதிவிறக்கம்", REPO_URL + "/releases"),
                  ("சிக்கலைத் தெரிவிக்க", REPO_URL + "/issues")],
        "ver": "நினைவு {v} · MIT உரிமம்",
        "keywords": "நினைவு, முகில், சுடர், குடும்பப் புகைப்படங்கள், வழிகாட்டி, Ninaivu",
    },
    "admin": {
        "site": ROOT / "docs",
        "pages": [("admin-guide.md", "#495057")],
        "kickers": ["Ninaivu {v}"],
        "numerals": [""],
        "title": "Ninaivu — technical administrator guide",
        "footer": "Ninaivu · technical administrator guide",
        "name": "Ninaivu", "under": "நினைவு · memory",
        "tag": "For whoever looks after the technical side.",
        "sub": "Technical administrator guide · version {v}",
        "parts": [("Docker", "on a NAS"), ("Source", "a checkout"), ("Command line", "flags and tools")],
        "contents": "Contents",
        "lede": "Everything in this guide, and the page it is on.",
        "page": "p.",
        "admonitions": {"note": "Note", "tip": "Tip", "warning": "Warning", "danger": "Danger"},
        "legend": "",
        "keys": [],
        "fine": "",
        "links": [("The guide, online", SITE_URL), ("Source", REPO_URL), ("Report a problem", REPO_URL + "/issues")],
        "ver": "Ninaivu {v} · MIT licence",
        "keywords": "Ninaivu, administrator, Docker, command line, guide",
        "html_lang": "en",
    },
}


@dataclass
class Chapter:
    key: str
    file: str                 # the page's file name, which other pages' links name
    colour: str
    kicker: str
    numeral: str
    title: str
    body: str                 # the page's HTML without its h1, ids already made unique across the book
    blurb: str                # the page's first sentence, for the contents
    anchors: dict[str, str]   # the page's own heading anchors, as other pages write them -> id in the book
    sections: list[tuple[str, str]] = field(default_factory=list)   # (id, title) of each h2
    headings: list[str] = field(default_factory=list)               # the text of every heading, for the bookmarks

    @property
    def id(self) -> str:
        return f"c-{self.key}"


def data_uri(path: Path) -> str:
    kind = "png" if path.suffix == ".png" else "jpeg"
    return f"data:image/{kind};base64," + base64.b64encode(path.read_bytes()).decode()


def plain(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", markup))).strip()


def shell_colours(code: str) -> str:
    """Comments and prompts in a command block, so what to type stands out from what is only said."""
    lines = []
    for line in code.split("\n"):
        if re.match(r"\s*#(\s|$)", line):
            line = f'<span class="c">{line}</span>'
        else:
            line = re.sub(r"(\s)(#\s.*)$", r'\1<span class="c">\2</span>', line)
            line = re.sub(r"^(\s*)(\$|PS [^&>]*&gt;|&gt;) ", r'\1<span class="p">\2</span> ', line)
        lines.append(line)
    return "\n".join(lines)


def render_chapter(site: Path, name: str, colour: str, kicker: str, numeral: str, t: dict) -> Chapter:
    key = Path(name).stem
    md = (site / name).read_text(encoding="utf-8")
    # "!!! note" with no title gets the language's word for it, not the English one
    md = re.sub(r"^!!! (\w+)[ \t]*$", lambda m: f'!!! {m[1]} "{t["admonitions"].get(m[1], m[1].title())}"', md, flags=re.M)
    body = Markdown(extensions=["tables", "fenced_code", "admonition", "attr_list", "toc"],
                    extension_configs={"toc": {"slugify": slugify_unicode}}).convert(md)
    body = body.replace("\\|", "|")                                     # a pipe escaped inside a table cell

    # Every heading gets an id that is ASCII and unique in the whole book; the page's own anchors
    # ({#home-and-the-internet}) are kept as the way other pages link to it.
    anchors: dict[str, str] = {}
    sections: list[tuple[str, str]] = []
    headings: list[str] = []

    def heading(m):
        level, old, inner = m[1], m[2], m[3]
        new = f"{key}-{len(anchors) + 1}"
        anchors[old] = new
        headings.append(plain(inner))
        if level == "2":
            sections.append((new, plain(inner)))
        return f'<h{level} id="{new}">{inner}</h{level}>'
    body = re.sub(r'<h([1-4]) id="([^"]+)">(.*?)</h\1>', heading, body, flags=re.S)

    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    title = plain(h1[1]) if h1 else key
    body = body.replace(h1[0], "", 1) if h1 else body

    body = re.sub(r'src="(?:\.\./)*screens/([^"]+)"', lambda m: f'src="{data_uri(SCREENS / m[1])}"', body)
    body = re.sub(r'<p><img alt="([^"]*)" src="([^"]*)" ?/?></p>',
                  lambda m: ('<figure class="shot"><div class="bar"><i></i><i></i><i></i></div>'
                             f'<img alt="{m[1]}" src="{m[2]}"><figcaption>{m[1]}</figcaption></figure>'), body)
    body = re.sub(r'<pre><code( class="language-(?:bash|sh|shell|console|powershell)")?>(.*?)</code></pre>',
                  lambda m: f'<pre><code{m[1] or ""}>{shell_colours(m[2])}</code></pre>', body, flags=re.S)

    body = re.sub(r"<p>((?:(?!</p>).)*:)</p>", r'<p class="intro">\1</p>', body, flags=re.S)   # "Like this:" stays with what follows
    body = re.sub(r"<tr>(\s*)<td>((?:(?!</td>).)*)</td>",                                   # a short first cell is a row label
                  lambda m: f'<tr>{m[1]}<td class="k">{m[2]}</td>' if len(plain(m[2])) <= 32 else m[0], body, flags=re.S)

    first = re.search(r"<p>(.*?)</p>", body, re.S)
    blurb = ""
    if first:
        blurb = re.split(r"(?<=[.!?])\s", plain(first[1]), maxsplit=1)[0]
        if len(blurb) > 150:
            blurb = blurb[:147].rsplit(" ", 1)[0] + "…"
    body = re.sub(r'^\s*<p( class="intro")?>', lambda m: f'<p class="lead{" intro" if m[1] else ""}">', body, count=1)  # the opening paragraph is the lead

    return Chapter(key, Path(name).name, colour, kicker.format(v=VERSION), numeral, title, body, blurb, anchors, sections, headings)


def load_chapters(lang: str) -> list[Chapter]:
    t = LANGUAGES[lang]
    pages = t.get("pages", PAGES)
    return [render_chapter(t["site"], t["home"] if p == "index.md" and "home" in t else p, c, k, n, t)
            for (p, c), k, n in zip(pages, t["kickers"], t["numerals"])]


def link_rewriter(ch: Chapter, chapters: list[Chapter], pages: dict[str, int], t: dict):
    """Links between the book's own pages become jumps with the page number beside them; links to
    pages that are not in this book lose their link, and web links print their address, since
    paper cannot be clicked."""
    by_file = {c.file: c for c in chapters}

    def jump(target: str, inner: str) -> str:
        return (f'<a class="xr" href="#{target}">{inner}'
                f'<span class="xref">{t["page"]} {pages.get(target, "00")}</span></a>')

    def sub(m):
        href, inner = html.unescape(m[1]), m[2]
        if href.startswith("#"):
            target = ch.anchors.get(href[1:])
            return jump(target, inner) if target else inner
        if re.match(r"[a-z][a-z0-9+.-]*:", href):
            shown = re.sub(r"^https?://(www\.)?|/$", "", href)
            echo = "" if plain(inner) in (href, shown) else f'<span class="url">{html.escape(shown)}</span>'
            return f'<a href="{html.escape(href)}">{inner}</a>{echo}'
        path, _, frag = href.partition("#")
        other = by_file.get(Path(path).name)
        if not other:
            return inner
        return jump(other.anchors.get(frag) or other.id, inner)
    return lambda body: re.sub(r'<a href="([^"]*)">(.*?)</a>', sub, body, flags=re.S)


def css_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def page_furniture(name: str, title: str, colour: str, t: dict) -> str:
    """The foot of one chapter's pages: its title, the guide's name, and the folio. There is no
    running head: the first page of a chapter would have to go without one (``:first`` means the
    first page of the book, not of a named page), and its opener already says the same thing."""
    face = 'font-family: "Noto Sans", "Noto Sans Tamil", sans-serif; font-size: 8pt; vertical-align: top; padding-top: 4mm;'
    return f"""
@page {name} {{
  @bottom-left {{ content: {css_string(title)}; {face} font-weight: 700; color: {colour}; border-top: .4pt solid #dcd8e4 }}
  @bottom-center {{ content: {css_string(t["footer"])}; {face} color: #8a8798; border-top: .4pt solid #dcd8e4 }}
  @bottom-right {{ content: counter(page); {face} font-size: 9pt; font-weight: 700; color: {colour}; border-top: .4pt solid #dcd8e4 }}
}}"""


def toc_rows(chapters: list[Chapter], pages: dict[str, int]) -> str:
    rows = []
    for ch in chapters:
        subs = "".join(f'<li><a href="#{i}"><span>{html.escape(s)}</span><i>{pages.get(i, "00")}</i></a></li>'
                       for i, s in ch.sections)
        chip = f'<span class="chip">{ch.numeral}</span>' if ch.numeral else '<span class="chip mark"></span>'
        rows.append(
            f'<li class="ch" style="--c:{ch.colour}"><a class="row" href="#{ch.id}">{chip}'
            f'<span class="t">{html.escape(ch.title)}</span><span class="dots"></span>'
            f'<span class="pg">{pages.get(ch.id, "00")}</span></a>'
            f'<p class="desc">{html.escape(ch.blurb)}</p>' + (f"<ul>{subs}</ul>" if subs else "") + "</li>")
    return "".join(rows)


def build_html(lang: str, chapters: list[Chapter], pages: dict[str, int]) -> str:
    t = LANGUAGES[lang]
    v = {"v": VERSION}
    logo = data_uri(ROOT / "ninaivu" / "static" / "icons" / "icon-512.png")
    parts = "".join(f"<div><b>{n}</b>{what}</div>" for n, what in t["parts"])
    cover = (f'<section class="cover"><div class="rings"></div><div class="mark"><img src="{logo}" alt=""></div>'
             f'<h1>{t["name"]}</h1><div class="under">{t["under"]}</div><div class="rule"></div>'
             f'<p class="tag">{t["tag"]}</p><div class="pill">{t["sub"].format(**v)}</div>'
             f'<div class="parts">{parts}</div></section>')

    keys = "".join(f'<div class="key {k}"><b>{label}</b>{what}</div>' for k, label, what in t["keys"])
    legend = ""
    if t["legend"]:
        legend = f'<div class="legend"><h2>{t["legend"]}</h2><div class="keys">{keys}</div><p class="fine">{t["fine"]}</p></div>'
    contents = (f'<section class="toc"><h1>{t["contents"]}</h1><p class="sub">{t["lede"]}</p>'
                f'<ol>{toc_rows(chapters, pages)}</ol>{legend}</section>')

    body = []
    for ch in chapters:
        rewrite = link_rewriter(ch, chapters, pages, t)
        mark = ch.numeral or f'<img src="{logo}" alt="">'
        body.append(
            f'<section class="chapter" id="{ch.id}" style="--c:{ch.colour}; page: ch-{ch.key}">'
            f'<header class="opener"><div class="num">{mark}</div><div class="kicker">{ch.kicker}</div>'
            f'<h1>{html.escape(ch.title)}</h1></header>{rewrite(ch.body)}</section>')

    links = "".join(f'<div><span>{label}</span><a href="{url}">{re.sub("^https://", "", url)}</a></div>'
                    for label, url in t["links"])
    back = (f'<section class="back"><img src="{logo}" alt=""><h2>{t["name"]}</h2><p class="tag">{t["tag"]}</p>'
            f'<div class="links">{links}</div><div class="ver">{t["ver"].format(**v)}</div></section>')

    furniture = page_furniture("ch-toc", t["contents"], "#e8590c", t) + "".join(
        page_furniture(f"ch-{c.key}", c.title, c.colour, t) for c in chapters)
    return (f"<!doctype html><html lang={t.get('html_lang', lang)}><head><meta charset=utf-8>"
            f"<title>{html.escape(t['title'])}</title><style>{STYLE.read_text(encoding='utf-8')}{furniture}</style></head>"
            f"<body>{cover}{contents}{''.join(body)}{back}</body></html>")


def print_pdf(html_text: str, path: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("NINAIVU_CHROMIUM") or None)
        page = browser.new_page()
        page.set_content(html_text, wait_until="load")
        page.evaluate("document.fonts.ready")
        page.pdf(path=str(path), format="A4", print_background=True, prefer_css_page_size=True,
                 outline=True, tagged=True)
        browser.close()


def quiet_pypdf() -> None:
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)     # "Object count exceeds defined trailer size": Chromium's, harmless


def landing_pages(path: Path) -> dict[str, int]:
    """The page (from 1) each anchor of the book landed on, read back from the printed PDF."""
    from pypdf import PdfReader

    quiet_pypdf()
    reader = PdfReader(str(path))
    return {name.lstrip("/"): reader.get_destination_page_number(dest) + 1
            for name, dest in reader.named_destinations.items()}


def repair_bookmarks(writer, headings: list[str]) -> None:
    """Chromium writes a bookmark's title twice when its heading was pushed to a new page, and
    drops the space where a balanced heading wraps. The headings are known, so put them back."""
    from pypdf.generic import NameObject, TextStringObject

    known = {re.sub(r"\s+", "", h): h for h in headings}

    def fix(title: str) -> str:
        squeezed = re.sub(r"\s+", "", title)
        half = len(squeezed) // 2
        if len(squeezed) % 2 == 0 and squeezed[:half] == squeezed[half:]:
            squeezed = squeezed[:half]
        return known.get(squeezed, title)

    def walk(item) -> None:
        while item is not None:
            item = item.get_object()
            if "/Title" in item:
                item[NameObject("/Title")] = TextStringObject(fix(str(item["/Title"])))
            if "/First" in item:
                walk(item["/First"])
            item = item.get("/Next")

    outlines = writer._root_object.get("/Outlines")
    if outlines is not None and "/First" in outlines.get_object():
        walk(outlines.get_object()["/First"])


def finish(src: Path, out: Path, lang: str, headings: list[str]) -> None:
    """Document properties, tidy bookmarks, and a viewer that opens with the bookmarks and the title showing."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import BooleanObject, DictionaryObject, NameObject, TextStringObject

    quiet_pypdf()
    t = LANGUAGES[lang]
    writer = PdfWriter(clone_from=PdfReader(str(src)))
    writer.add_metadata({"/Title": t["title"], "/Author": "Ninaivu", "/Subject": t["tag"],
                         "/Keywords": t["keywords"], "/Creator": "Ninaivu tools/build_guide_pdf.py"})
    root = writer._root_object
    root[NameObject("/PageMode")] = NameObject("/UseOutlines")
    root[NameObject("/Lang")] = TextStringObject(t.get("html_lang", lang))
    root[NameObject("/ViewerPreferences")] = DictionaryObject({NameObject("/DisplayDocTitle"): BooleanObject(True)})
    repair_bookmarks(writer, headings)
    writer.compress_identical_objects()
    with out.open("wb") as f:
        writer.write(f)


def main(out: Path, lang: str = "en", keep_html: bool = False) -> None:
    chapters = load_chapters(lang)
    scratch = out.with_suffix(".tmp.pdf")
    pages: dict[str, int] = {}
    try:
        for _ in range(4):
            text = build_html(lang, chapters, pages)
            print_pdf(text, scratch)
            found = landing_pages(scratch)
            if found == pages:
                break
            pages = found
        else:
            raise SystemExit("the page numbers did not settle after four prints")
        if keep_html:
            out.with_suffix(".html").write_text(text, encoding="utf-8")
        finish(scratch, out, lang, [LANGUAGES[lang]["name"], LANGUAGES[lang]["contents"], LANGUAGES[lang]["legend"],
                                    *(h for ch in chapters for h in (ch.title, *ch.headings))])
    finally:
        scratch.unlink(missing_ok=True)
    print(out, out.stat().st_size, "bytes")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", type=Path)
    ap.add_argument("--lang", choices=sorted(LANGUAGES), default="en")
    ap.add_argument("--keep-html", action="store_true", help="leave the HTML that was printed next to the PDF")
    a = ap.parse_args()
    default = {"en": "Ninaivu-guide.pdf", "admin": "Ninaivu-admin-guide.pdf"}.get(a.lang, f"Ninaivu-guide-{a.lang}.pdf")
    main(a.out or Path(default), a.lang, a.keep_html)
