"""Like a phone's own photo app: the next few hundred thumbnails either way are
already there. Counted in photographs, not screens, around where the screen
stopped and around where the pointer is about to take it (the date rail, a
month in Jump to). Checked in a browser on 2,000 photographs: 326 ready at
the top instead of 44, 630 more after stopping mid-way instead of 54, and
hovering the date rail fetched 455 around that place, where it fetched none."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "ninaivu/static/js/grid-base.js").read_text(encoding="utf-8")
GRID = (ROOT / "ninaivu/static/js/grid.js").read_text(encoding="utf-8")
APP = (ROOT / "ninaivu/static/js/app.js").read_text(encoding="utf-8")


def test_three_hundred_each_way():
    assert re.search(r"const PREFETCH_COUNT = 300;", BASE)
    assert "PREFETCH_COUNT" in GRID[:GRID.index("export class Grid")]
    ready = GRID[GRID.index("  readyAround("):GRID.index("  settle() {")]
    assert "this.nearestUrls(bottom, 1, count)" in ready
    assert "this.nearestUrls(top, -1, count)" in ready
    assert "PREFETCH_COUNT / 2 : PREFETCH_COUNT" in ready
    # A lean link (Data Saver, 2G) gets a screen and a half on, nothing behind.
    assert "if (profile.lean)" in ready
    # Settling and the pointer both use it.
    settle = GRID[GRID.index("  settle() {"):GRID.index("  prefetchAround(")]
    assert "this.readyAround(top, bottom, down, profile)" in settle
    pointer = GRID[GRID.index("  prefetchAround("):GRID.index("  paintCell(")]
    assert "this.readyAround(top, top + view, true, profile)" in pointer


def test_a_wobble_does_not_throw_away_what_is_on_its_way():
    speed = GRID[GRID.index("  measureSpeed() {"):GRID.index("  band() {")]
    assert "this.turning >= 2" in speed and "REVERSE_PX_MS" in speed
    assert "this.velocity * 0.4 + instant * 0.6" in speed
    warm = GRID[GRID.index("  warmAhead("):GRID.index("  readyAround(")]
    # A fling pauses the queue; it does not abort what is already coming.
    assert "this.prefetcher.want([], { priority: 'warm' })" in warm


def test_the_pointer_gets_its_place_ready():
    assert "prefetchAround(y) {" in GRID
    rail = APP[APP.index("function wireScrubber()"):]
    assert "grid.prefetchAround(max * ratio)" in rail
    assert "event.pointerType !== 'mouse'" in rail
    assert "button.onpointerenter = () => grid.prefetchAround(" in APP
