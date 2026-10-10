"""Every bar at the top of a page is solid, so iOS 26 Safari does not blur it.

Safari on iOS 26 looks for a solid bar at the top of the page and carries its
colour up under the status bar. Over a see-through bar it draws its own soft
blur across the top of the page instead, smearing the logo, buttons and avatar.
The console's and the family app's bars were made solid in 1.10.0; the shared
album page, "Ask the family", the photo editor and AI Studio followed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ninaivu" / "static" / "css"
TEMPLATES = ROOT / "ninaivu" / "templates"

# (file, selector of the bar at the top of the page)
BARS = [
    (STATIC / "style.css", ".topbar"),
    (STATIC / "admin.css", ".admin-bar"),
    (STATIC / "premium.css", ".topbar"),
    (STATIC / "premium.css", ".admin-bar"),
    (STATIC / "editor.css", ".photo-editor header"),
    (STATIC / "ai-playground.css", ".ap-header"),
    (TEMPLATES / "share.html", "header"),
    (TEMPLATES / "ask.html", "header"),
]


def _rules(text: str, selector: str) -> list[str]:
    """The declaration blocks of every rule whose selector list names `selector`."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    found = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        names = [name.strip() for name in match.group(1).split(",")]
        if selector in names:
            found.append(match.group(2))
    assert found, f"no rule for {selector!r}"
    return found


@pytest.mark.parametrize("path, selector", BARS, ids=lambda v: getattr(v, "name", v))
def test_a_top_bar_is_not_see_through(path, selector):
    for declarations in _rules(path.read_text(encoding="utf-8"), selector):
        for line in declarations.split(";"):
            name, _, value = line.partition(":")
            name, value = name.strip(), value.strip()
            if name == "background":
                assert "color-mix" not in value and "transparent" not in value, line
            if name in ("backdrop-filter", "-webkit-backdrop-filter"):
                assert value == "none", line
