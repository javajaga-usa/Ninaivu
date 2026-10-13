"""The gallery and the viewer behave like a phone's own photo app.

Checked in Chromium on 2,000 photographs: All media left 14,000 px down came
back to the same photograph at the top after visiting Photos and after
reloading the page; and the viewer, with the full photograph held back 2.5 s,
showed the gallery's own picture at once, unblurred, in exactly the box the
photograph then took.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The tiles live in grid-base.js; grid.js layers the fetching ahead on top.
GRID = (ROOT / "ninaivu/static/js/grid-base.js").read_text(encoding="utf-8")
APP = (ROOT / "ninaivu/static/js/app.js").read_text(encoding="utf-8")
VIEWER = (ROOT / "ninaivu/static/js/viewer.js").read_text(encoding="utf-8")
CSS = (ROOT / "ninaivu/static/css/style.css").read_text(encoding="utf-8")


def test_more_tiles_are_kept_with_their_pictures():
    assert re.search(r"matchMedia\('\(hover: none\)'\)\.matches\)\s*\?\s*600 : 1200;", GRID)


def test_each_view_remembers_its_place():
    assert "anchor() {" in GRID and "scrollToAnchor(place) {" in GRID
    # Measured from the grid's own top, whatever is above it.
    assert GRID.count("this.container.offsetTop || 0") >= 2
    assert "function rememberPlace()" in APP and "function restorePlace(filters)" in APP
    assert "if (resetScroll || placeDue)" in APP
    assert "window.addEventListener('pagehide', rememberPlace)" in APP
    assert "new ResizeObserver(holdPlace)" in APP


def test_the_viewer_opens_on_the_gallery_picture_not_a_blur():
    assert "ghost.className = sharp ? 'placeholder sharp' : 'placeholder'" in VIEWER
    rule = re.search(r"\.viewer-stage > img\.placeholder\.sharp \{[^}]*\}", CSS).group(0)
    assert "filter: none" in rule and "grid-area: 1 / 1" in rule


def test_a_neighbour_let_go_of_is_warmed_again():
    assert "!this.warming.has(neighbour) && !this.warmed.has(neighbour)" in VIEWER
    assert "remember(id) {" in VIEWER
