"""The premium finish (static/css/premium.css) is one sheet for both apps.

It is loaded last by the family app and the console so its rules win over
the older refinement blocks, and it only restyles: it never hides, moves or
resizes anything the pages' own sheets lay out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "ninaivu" / "static" / "css"
TEMPLATES = ROOT / "ninaivu" / "templates"


def _sheets(template: str) -> list[str]:
    html = (TEMPLATES / template).read_text(encoding="utf-8")
    return re.findall(r"""<link rel="stylesheet" href="\{\{ asset\('/static/css/([\w-]+\.css)'\) \}\}">""", html)


@pytest.mark.parametrize("template", ["index.html", "admin.html"])
def test_both_apps_load_the_finish_last(template):
    sheets = _sheets(template)
    assert "style.css" in sheets
    assert sheets[-1] == "premium.css", sheets


def test_the_finish_never_hides_or_places_anything():
    text = re.sub(r"/\*.*?\*/", "", (CSS / "premium.css").read_text(encoding="utf-8"), flags=re.DOTALL)
    for banned in ("position: fixed", "visibility: hidden", "order", "grid-template"):
        assert not re.search(rf"(?<![\w-]){banned}\s*:", text), banned
    # The progress bar's sheen is the one thing switched off, for reduced motion.
    assert text.count("display: none") == 1
    assert "scan-bar i::after { animation: none; display: none; }" in text


def test_check_boxes_are_drawn_at_no_weight():
    """The drawn check boxes sit inside :where(), so any box a page has already
    sized (photo-book picks, the switch rows) keeps its own size."""
    text = (CSS / "premium.css").read_text(encoding="utf-8")
    for rule in re.findall(r"^([^{}\n]*input\[type=(?:checkbox|radio)\][^{}\n]*)\{", text, flags=re.M):
        if "focus-visible" in rule:
            continue
        assert rule.strip().startswith(":where("), rule


def test_tamil_is_not_tightened():
    text = (CSS / "premium.css").read_text(encoding="utf-8")
    assert ":lang(ta) .btn { letter-spacing: 0; }" in text


def _layers(value: str) -> list[str]:
    """A box-shadow's layers: its commas outside brackets."""
    layers, depth, start = [], 0, 0
    for i, ch in enumerate(value):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            layers.append(value[start:i])
            start = i + 1
    return layers + [value[start:]]


def test_buttons_sit_flat():
    """Buttons have no drop shadow, glow or lit edge, in any state: the family
    found the raised, glowing buttons heavy. A focus ring, or a hairline drawn
    inside, is all a button's box-shadow may be."""
    sheets = [CSS / name for name in ("premium.css", "style.css", "creative-studio.css", "ai-playground.css")]
    sheets += [TEMPLATES / "share.html", TEMPLATES / "ask.html"]
    allowed = re.compile(r"none|var\(--ring\)|inset .*|0 0 0 [\d.]+px .*")
    for sheet in sheets:
        text = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.DOTALL)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if not re.search(r"\.btn\b|\.chip\b|\.send\b|(?<![\w-])button(?![\w-])", selector):
                continue
            if re.search(r"\.jump-btn|::after|-thumb", selector):
                continue  # a button floating over photographs needs its shadow
            for value in re.findall(r"(?<![\w-])box-shadow:\s*([^;]+)", body):
                for part in _layers(value):
                    assert allowed.fullmatch(part.strip()), f"{sheet.name}: {selector.strip()} → {value}"
            assert "text-shadow" not in body or "text-shadow: none" in body, f"{sheet.name}: {selector.strip()}"
