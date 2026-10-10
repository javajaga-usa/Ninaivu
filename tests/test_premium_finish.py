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
        assert rule.startswith(":where("), rule


def test_tamil_is_not_tightened():
    text = (CSS / "premium.css").read_text(encoding="utf-8")
    assert ":lang(ta) .btn { letter-spacing: 0; }" in text
