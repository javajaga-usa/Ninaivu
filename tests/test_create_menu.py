"""Where making something lives: one Create menu on the top bar, one Add
photos menu, and Creative Studio wherever the photographs are chosen (a
selection, an album, the viewer), not only behind Sudar's Magic tools.

These are checks of the page as written; tests/creative_studio_ui.mjs opens
the studio from the gallery in a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "ninaivu" / "static"
PAGE = (Path(__file__).resolve().parents[1] / "ninaivu" / "templates" / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "js" / "app.js").read_text(encoding="utf-8")


def _menu(menu_id: str) -> str:
    """The markup of the menu list *menu_id*, up to the end of its wrapper."""
    start = PAGE.index(f'id="{menu_id}"')
    return PAGE[start:PAGE.index('\n      </div>\n', start)]


def test_making_and_looking_back_are_one_menu():
    menu = _menu("create-menu")
    for button in ("studio-btn", "create-books-btn", "year-btn", "then-btn",
                   "create-capsules-btn", "frame-btn"):
        assert f'id="{button}"' in menu, button
    # Making first, then looking back, each under its heading.
    assert menu.index('"Make something"') < menu.index('id="studio-btn"') < menu.index('"Look back"') < menu.index('id="year-btn"')


def test_bringing_photos_in_is_one_menu():
    menu = _menu("add-menu")
    for button in ("upload-btn", "backup-btn", "prints-btn"):
        assert f'id="{button}"' in menu, button


def test_the_top_bar_keeps_only_menus_and_a_few_buttons():
    bar = PAGE[PAGE.index('id="topbar-more"'):PAGE.index('<div class="view-menu">')]
    outside = re.sub(r'<div class="bar-menu-list".*?\n        </div>', "", bar, flags=re.S)
    loose = re.findall(r'<button class="icon-btn" id="([\w-]+)"', outside)
    assert loose == ["ask-btn", "map-btn"], loose


def test_both_menus_open_like_the_others():
    assert "wireMenu($('#add-btn'), $('#add-menu'))" in APP
    assert "wireMenu($('#create-btn'), $('#create-menu'))" in APP


def test_creative_studio_is_reached_where_the_photos_are():
    assert 'id="sel-create"' in PAGE, "the selection bar"
    assert 'id="v-create"' in PAGE, "the viewer"
    assert "i18n.t('Make a collage')" in APP, "an album, a trip or a person"
    assert "openStudioFor([...grid.selection]" in APP
    viewer = (STATIC / "js" / "viewer.js").read_text(encoding="utf-8")
    assert "openCreativeStudio({ids: [item.id]" in viewer


def test_guests_are_not_offered_creative_studio():
    assert "$('#studio-btn').hidden = !state.user?.can?.download;" in APP
    assert "$('#sel-create').hidden = !can.download;" in APP


def test_the_studio_does_not_offer_a_second_then_and_now():
    studio = (STATIC / "js" / "creative-studio.js").read_text(encoding="utf-8")
    assert "i18n.key('Then & now')" not in studio
    assert "i18n.key('Side by side')" in studio
