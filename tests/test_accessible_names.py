"""Every field a person can type into or choose from has a name a screen reader says.

Found with axe-core against the running app (November 2026): the family app's
Sort menu had no name at all, the search box said it opened a list without
saying it was a combo box, the viewer's filmstrip could be scrolled only with
a mouse, and twenty fields in the console were named by their placeholder
alone, which goes silent as soon as anything is typed. The console also
stopped a phone from zooming. These checks read the templates, so a field
added without a name fails here rather than waiting for the next audit.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "ninaivu" / "templates"
NAMELESS_TYPES = {"hidden", "submit", "button", "reset", "image"}


class _Fields(HTMLParser):
    """Collects the visible form fields and what names them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: list[tuple[int, str, dict]] = []
        self.label_for: set[str] = set()
        self.ids: set[str] = set()
        self._in_label = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        a = {k: (v or "") for k, v in attrs}
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "label":
            self._in_label += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea"):
            if a.get("type", "").lower() in NAMELESS_TYPES or "hidden" in a:
                return
            a["_wrapped"] = "1" if self._in_label else ""
            self.fields.append((self.getpos()[0], tag, a))

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == "label" and self._in_label:
            self._in_label -= 1


def _parse(name: str) -> _Fields:
    parser = _Fields()
    parser.feed((TEMPLATES / name).read_text(encoding="utf-8"))
    return parser


@pytest.mark.parametrize("template", ["index.html", "admin.html", "share.html", "ask.html"])
def test_every_visible_field_is_named(template):
    page = _parse(template)
    nameless = []
    for line, tag, a in page.fields:
        named = (a.get("aria-label") or a.get("aria-labelledby") or a.get("title")
                 or a["_wrapped"] or a.get("id") in page.label_for)
        if not named:
            nameless.append(f"{template}:{line} <{tag} id={a.get('id')!r}>")
    assert not nameless, "fields with no name but a placeholder:\n" + "\n".join(nameless)


@pytest.mark.parametrize("template", ["index.html", "admin.html"])
def test_a_labelledby_points_at_something_on_the_page(template):
    page = _parse(template)
    for line, _tag, a in page.fields:
        for ref in a.get("aria-labelledby", "").split():
            assert ref in page.ids, f"{template}:{line} names itself after missing #{ref}"


@pytest.mark.parametrize("template", ["admin.html", "index.html", "share.html", "ask.html"])
def test_no_page_stops_a_phone_zooming(template):
    """The viewer does its own pinch on a stage that takes no browser gestures
    (touch-action: none), so the page does not need zoom taken away."""
    head = (TEMPLATES / template).read_text(encoding="utf-8").split("</head>", 1)[0]
    assert "user-scalable=no" not in head
    assert "maximum-scale=1" not in head


def test_the_family_app_has_a_top_level_heading():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    assert 'role="heading" aria-level="1"' in html


def test_a_console_field_on_a_phone_is_big_enough_that_safari_does_not_zoom():
    css = (TEMPLATES.parent / "static" / "css" / "admin.css").read_text(encoding="utf-8")
    coarse = css.split("@media (pointer: coarse) {", 1)[1].split("\n}", 1)[0]
    assert "font-size: 16px" in coarse


def test_search_says_it_is_a_combo_box_with_a_list():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    search = html.split('<input id="search"', 1)[1].split(">", 1)[0]
    assert 'role="combobox"' in search
    assert 'aria-controls="suggestions"' in search


def test_the_filmstrip_can_be_reached_from_the_keyboard():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    strip = html.split('id="filmstrip"', 1)[1].split(">", 1)[0]
    assert 'tabindex="0"' in strip and "aria-label=" in strip
