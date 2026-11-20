"""An empty view from the sidebar says what is missing and offers the way back.

Opening Videos on a library with none showed "Nothing to show / This filter
has no items yet" and a Clear filters button. There was no filter: the button
cleared nothing and reloaded the same empty view. The weekly health card also
stayed above every search and view, half a phone screen above the results.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "ninaivu" / "static"
APP = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
PAGE = (Path(__file__).resolve().parents[1] / "ninaivu" / "templates" / "index.html").read_text(encoding="utf-8")


def _empty_views() -> dict[str, str]:
    block = APP[APP.index("const EMPTY_VIEW = {"):]
    block = block[:block.index("};")]
    return dict(re.findall(r"(\w+): i18n\.key\('([^']+)'\)", block))


def test_every_sidebar_view_that_can_be_empty_names_what_is_missing():
    views = set(re.findall(r'data-view="([a-z-]+)"', PAGE))
    # All is the gallery itself; Memories opens the viewer; Duplicates and the
    # private folder have screens of their own for "nothing here".
    expected = views - {"all", "memories", "duplicates", "private"}
    assert expected <= set(_empty_views()), expected - set(_empty_views())


def test_the_empty_view_offers_every_photograph_not_a_filter_to_clear():
    assert "showEmpty(i18n.t(EMPTY_VIEW[state.view]), i18n.t('Nothing here yet.'), { scene: 'filter', showAll: true })" in APP
    assert "action.textContent = i18n.t('Show everything');\n    action.onclick = showEverything;" in APP
    # Only when nothing of the person's own narrows it: a search in Videos
    # that finds nothing still says "No matches" and offers Clear filters.
    assert "!data.total && !anyFilter() && EMPTY_VIEW[state.view]" in APP


def test_the_weeks_card_steps_aside_while_anything_narrows_the_gallery():
    assert "classList.toggle('aside', anyFilter() || state.view !== 'all')" in APP
    css = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    assert ".week-card.aside { display: none; }" in css
