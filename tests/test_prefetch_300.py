"""Like a phone's own photo app: the next few hundred thumbnails either way are
already there. Counted in photographs, not screens, around where the screen
stopped and around where the pointer is about to take it (the date rail, a
month in Jump to). Checked in a browser on 2,000 photographs: 326 ready at
the top instead of 44, 630 more after stopping mid-way instead of 54, and
hovering the date rail fetched 455 around that place, where it fetched none."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRID = (ROOT / "ninaivu/static/js/grid.js").read_text(encoding="utf-8")
APP = (ROOT / "ninaivu/static/js/app.js").read_text(encoding="utf-8")


def test_three_hundred_each_way():
    assert re.search(r"const PREFETCH_COUNT = 300;", GRID)
    assert "nearestUrls(bottom, 1, PREFETCH_COUNT)" in GRID
    assert "nearestUrls(top, -1, PREFETCH_COUNT)" in GRID
    # Settling uses it, and a lean link (Data Saver, 2G) still does not.
    settle = GRID[GRID.index("  settle() {"):GRID.index("  readyAround(")]
    assert "this.readyAround(top, bottom, down)" in settle and "leanLink()" in settle


def test_the_pointer_gets_its_place_ready():
    assert "prefetchAround(y) {" in GRID
    rail = APP[APP.index("function wireScrubber()"):]
    assert "grid.prefetchAround(max * ratio)" in rail
    assert "event.pointerType !== 'mouse'" in rail
    assert "button.onpointerenter = () => grid.prefetchAround(" in APP
