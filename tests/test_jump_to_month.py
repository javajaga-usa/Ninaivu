"""The gallery's "Jump to" button: a month and a year this view has.

Faint over the photographs until pointed at, so it never hides one; whole on
hover, focus or while open. Its list comes from the view's own sections, so
it offers only the years and months there are (app.js, jumpIndex).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_page_carries_the_button_and_its_panel():
    page = (ROOT / "ninaivu/templates/index.html").read_text(encoding="utf-8")
    for needle in ('id="jump" hidden', 'id="jump-btn"', 'id="jump-pop"',
                   'id="jump-years"', 'id="jump-months"', 'aria-haspopup="dialog"'):
        assert needle in page


def test_it_is_faint_at_rest_and_whole_when_pointed_at():
    css = (ROOT / "ninaivu/static/css/style.css").read_text(encoding="utf-8")
    rest = re.search(r"\.jump-btn \{[^}]*opacity:\s*([\d.]+)", css)
    assert rest and 0 < float(rest.group(1)) < 0.6
    assert re.search(r"\.jump-btn:hover[^{]*\{[^}]*opacity:\s*1", css)
    # Only the pill and its panel take clicks, never the space around them.
    assert re.search(r"\.jump \{[^}]*pointer-events:\s*none", css)


def test_only_months_the_view_has_are_offered():
    script = (ROOT / "ninaivu/static/js/app.js").read_text(encoding="utf-8")
    assert "function jumpIndex()" in script and "button.disabled = !header" in script
    # And only in date order: names and sizes have no months to jump between.
    assert "['date_desc', 'date_asc']" in script
