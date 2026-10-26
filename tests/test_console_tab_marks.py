"""The console's tab counts and the phone's section marks tell one story.

On a phone the console shows its sections as a row of pills, and a section
with something waiting wears a dot. Each dot must be traceable to a number on
a tab inside that section; and the Health tab, its pill and Needs you must
count the same problems (the browser side is console_tab_marks_ui.mjs).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ninaivu.utils import logs

ADMIN_PAGE = Path(__file__).resolve().parents[1] / "ninaivu" / "templates" / "admin.html"


def test_every_page_needs_you_points_at_has_a_tab_that_can_show_its_count(as_admin):
    html = ADMIN_PAGE.read_text(encoding="utf-8")
    items = as_admin.get("/api/admin/attention").get_json()["items"]
    assert items
    for item in items:
        tab = re.search(rf'<button role="tab" data-tab="{item["page"]}"[^>]*>.*?</button>', html, re.S)
        assert tab, item["page"]
        assert 'class="tab-count"' in tab.group(0), f'the {item["page"]} tab has nowhere to show {item["key"]}'


def test_the_health_pill_and_needs_you_count_the_same_problems(as_admin):
    logs.forget()
    try:
        for n in range(20):
            logs._problems.handle(logging.makeLogRecord(
                {"name": "ninaivu.test", "levelno": logging.WARNING, "levelname": "WARNING",
                 "msg": f"warning {n}"}))
        listed = as_admin.get("/api/admin/problems?limit=12").get_json()
        items = as_admin.get("/api/admin/attention").get_json()["items"]
        counted = next(i for i in items if i["key"] == "problems")["count"]
        assert len(listed["problems"]) == 12
        assert listed["total"] == counted == 20
    finally:
        logs.forget()
