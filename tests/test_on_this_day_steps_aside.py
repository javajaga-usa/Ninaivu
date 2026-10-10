"""On This Day steps aside while the family is looking for something.

The strip of photographs from this day in past years sits above the plain
gallery. Once a search is typed, a sidebar filter picked (a year, a folder, a
person, an album…) or a narrower view chosen (Favourites, Videos…), the strip
is noise above results that were asked for, so it hides; clearing everything
brings it back. The rule lives in two small functions in app.js, run here in
Node against a stand-in for the page state.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "ninaivu" / "static" / "js" / "app.js"


def _function(source, name):
    match = re.search(rf"^function {name}\(\) {{\n.*?^}}\n", source, re.S | re.M)
    assert match, f"app.js no longer has {name}()"
    return match.group(0)


PLAIN = {"q": "", "kinds": [], "favorites": False, "duplicates": False,
         "tag": "", "folder": "", "camera": "", "from": "", "to": "",
         "person": 0, "occasion": 0, "album": 0, "near": 0, "sort": "date_desc"}

CASES = [
    ("all", {}, True),
    ("all", {"sort": "date_asc"}, True),
    ("all", {"q": "beach"}, False),
    ("all", {"tag": "birthday"}, False),
    ("all", {"folder": "2019"}, False),
    ("all", {"camera": "iPhone"}, False),
    ("all", {"from": "2019-01-01", "to": "2019-12-31"}, False),
    ("all", {"person": 4}, False),
    ("all", {"occasion": 2}, False),
    ("all", {"album": 7}, False),
    ("all", {"near": 1}, False),
    ("all", {"favorites": True}, False),
    ("all", {"kinds": ["video"]}, False),
    ("favorites", {}, False),
    ("videos", {}, False),
    ("pictures", {}, False),
    ("live", {}, False),
    ("hidden", {}, False),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_strip_shows_only_on_the_plain_gallery():
    source = APP.read_text(encoding="utf-8")
    script = "\n".join([
        "const cases = " + json.dumps(
            [[view, {**PLAIN, **change}] for view, change, _ in CASES]) + ";",
        "let state;",
        _function(source, "anyFilter"),
        _function(source, "memoriesBannerWanted"),
        "console.log(JSON.stringify(cases.map(([view, filters]) => {",
        "  state = { view, filters };",
        "  return memoriesBannerWanted();",
        "})));",
    ])
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, check=True).stdout
    got = json.loads(out)
    for (view, change, want), shown in zip(CASES, got):
        assert shown is want, f"view={view} {change}: shown={shown}"


def test_every_reload_decides_the_strip_again():
    """Clearing a search or a filter reloads; the strip must follow it back."""
    source = APP.read_text(encoding="utf-8")
    reload = re.search(r"^async function reload\(.*?^}\n", source, re.S | re.M)
    assert reload and "showMemoriesBanner()" in reload.group(0)
    load = re.search(r"^async function loadMemories\(\) {\n.*?^}\n", source, re.S | re.M)
    assert load and "banner.hidden = false" not in load.group(0)
    assert "showMemoriesBanner()" in load.group(0)
