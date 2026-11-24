"""The console's About box: the version, where this runs, and what it holds."""
from __future__ import annotations

from ninaivu import __version__


def test_the_administrator_reads_what_this_copy_is(as_admin):
    body = as_admin.get("/api/admin/about").get_json()
    assert body["version"] == __version__
    assert body["python"] and body["system"]
    assert body["state_dir"]
    assert isinstance(body["libraries"], list)
    assert isinstance(body["models"], list)
    assert body["notices"].endswith("THIRD_PARTY_NOTICES.md")


def test_nobody_else_reads_it(as_family, anon):
    assert as_family.get("/api/admin/about").status_code in (401, 403, 404)
    assert anon.get("/api/admin/about").status_code in (401, 403, 404)


def test_the_console_page_carries_the_box(as_admin):
    page = as_admin.get("/").get_data(as_text=True)
    assert 'id="about-btn"' in page and 'id="about-modal"' in page
    assert __version__ in page
