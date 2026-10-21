"""The dark look (static/css/style.css) reads well and reads as one.

Warm charcoal, three stepped surfaces, no pure white, and text that meets
WCAG AA on every surface it sits on. The same colours are carried by the
share and Ask pages, Sudar, the photo editor, the offline page, the web app
manifest and the Control Panel, so the family sees one product.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "ninaivu" / "static" / "css"
TEMPLATES = ROOT / "ninaivu" / "templates"


def _tokens(text: str, selector: str) -> dict[str, str]:
    """The custom properties set in the first rule for *selector*."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    start = text.index(selector + " {")
    body = text[start:text.index("}", start)]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))


def _luminance(colour: str) -> float:
    r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.fixture(scope="module")
def dark() -> dict[str, str]:
    return _tokens((CSS / "style.css").read_text(encoding="utf-8"), ':root[data-theme="dark"]')


def test_the_explicit_and_the_system_dark_are_the_same():
    text = re.sub(r"/\*.*?\*/", "", (CSS / "style.css").read_text(encoding="utf-8"), flags=re.DOTALL)
    explicit = _tokens(text, ':root[data-theme="dark"]')
    media = text[text.index("@media (prefers-color-scheme: dark)"):]
    system = _tokens(media, ':root:not([data-theme="light"])')
    assert explicit == system


SURFACES = ("--bg", "--bg-elev", "--surface", "--surface-2")


@pytest.mark.parametrize("words", ["--text", "--text-2", "--text-3", "--accent", "--accent-2", "--danger", "--gold"])
@pytest.mark.parametrize("ground", SURFACES)
def test_text_meets_aa_on_every_surface(dark, words, ground):
    assert contrast(dark[words], dark[ground]) >= 4.5, (words, ground)


def test_a_label_on_the_accent_meets_aa(dark):
    assert contrast(dark["--accent-fg"], dark["--accent"]) >= 4.5


def test_nothing_is_pure_white_and_the_surfaces_step_up(dark):
    assert dark["--text"].lower() != "#ffffff"
    levels = [_luminance(dark[s]) for s in SURFACES]
    assert levels == sorted(levels) and len(set(levels)) == len(levels)
    assert _luminance(dark["--bg-sunken"]) < levels[0]


def test_the_charcoal_is_warm(dark):
    """A touch of brown, not grey: red is at least blue in every surface."""
    for name in SURFACES:
        r, _g, b = (int(dark[name][i:i + 2], 16) for i in (1, 3, 5))
        assert r >= b, name


def test_the_other_pages_carry_the_same_charcoal(dark):
    bg, text = dark["--bg"], dark["--text"]
    for page in ("share.html", "ask.html"):
        html = (TEMPLATES / page).read_text(encoding="utf-8")
        assert f"--ground: {bg};" in html and f"--ink: {text};" in html, page
    for sheet, prefix in (("editor.css", "pe"), ("ai-playground.css", "ap")):
        tokens = _tokens((CSS / sheet).read_text(encoding="utf-8"), ':root[data-theme="dark"]')
        assert tokens[f"--{prefix}-bg"] == bg and tokens[f"--{prefix}-text"] == text, sheet
    assert f"background:{bg};" in (ROOT / "ninaivu" / "static" / "sw.js").read_text(encoding="utf-8")
    assert f'"background_color": "{bg}"' in (ROOT / "ninaivu" / "api" / "api.py").read_text(encoding="utf-8")
    from ninaivu.desktop import theme
    assert theme.BASE["dark"]["bg"] == bg and theme.BASE["dark"]["text"] == text
