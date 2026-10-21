"""The console pass of 2026-10-21: what was fixed stays fixed.

The page guide is one row, a greyed select draws one arrow, a field in a
setting's column keeps its height, an empty list is not a warning, and the
storage check has a Stop button and a progress line.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ninaivu" / "static"
ADMIN = (ROOT / "ninaivu" / "templates" / "admin.html").read_text(encoding="utf-8")


def _css(name: str) -> str:
    return re.sub(r"/\*.*?\*/", "", (STATIC / "css" / name).read_text(encoding="utf-8"),
                  flags=re.DOTALL)


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


def test_every_page_guide_is_one_row():
    guides = ADMIN.count('class="admin-guide"')
    assert guides >= 20
    # The poster's decorations are gone from every page, not hidden.
    for part in ("guide-halo", "guide-sheet-back", "guide-caption", "guide-dot", "guide-spark"):
        assert part not in ADMIN, part
    assert ADMIN.count('<svg class="admin-guide-art" viewBox="51 17 86 82"') == guides
    rule = _rule(_css("admin.css"), ".admin-guide-art")
    assert "width: 44px" in rule and "height: 42px" in rule


def test_a_greyed_select_draws_its_arrow_once():
    # style.css greys it with the `background` shorthand, which tiles the image.
    assert ".select:disabled { background: var(--surface-2); }" in _css("style.css")
    rule = _rule(_css("premium.css"), "select.select:disabled")
    assert "background-repeat: no-repeat" in rule


def test_a_field_in_a_setting_column_keeps_its_height():
    # .input grows along a row (flex: 1); in a column that squashed it flat.
    assert "flex: 1" in _rule(_css("style.css"), ".input")
    assert "flex: none" in _rule(_css("admin.css"), ".setting-field > .input, .setting-field > .select")
    assert '<div class="setting-field">\n        <label for="notify-webhook"' in ADMIN


def test_an_empty_list_is_not_a_warning():
    for name in ("fold-empty", "bin-empty", "lf-empty", "ar-sources-empty"):
        assert f'class="empty-note calm" id="{name}"' in ADMIN, name
    assert 'class="empty-note good" id="problems-empty"' in ADMIN


def test_the_storage_check_can_be_stopped_and_shows_progress():
    assert 'id="scrubber-stop-btn"' in ADMIN
    assert 'id="scrub-progress-bar"' in ADMIN
    assert "Bitrot" not in re.sub(r"<!--.*?-->", "", ADMIN, flags=re.DOTALL)
    js = (STATIC / "js" / "admin.js").read_text(encoding="utf-8")
    assert "'/api/admin/scrubber/stop'" in js
    assert "showScrubberProgress(data?.progress)" in js
