"""How long the gallery waits before it can show a photograph.

The family app used to sit empty for most of half a minute after a restart on
a large library. Two things caused it, and both are pinned here.

The first was ``library_stats`` joining ``embeddings`` to ``assets``:
``asset_id`` is the rowid, so the join probed each row by rowid and read the
page that vector sits on — 358 MB of blob read to count 183,157 rows. The
second was the gallery awaiting ``/api/status`` — counts and all — before it
asked for a single picture.

So: the counts are their own endpoint, ``/api/status`` will leave them out on
request, and there is an index that lets the count walk something narrow.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ninaivu.storage import db

SCRIPTS = Path(__file__).resolve().parent.parent / "ninaivu" / "static" / "js"


# -- the counts, on their own -----------------------------------------------

def test_the_status_still_carries_the_counts_by_default(as_admin):
    """Anything already asking for /api/status keeps getting what it got."""
    body = as_admin.get("/api/status").get_json()
    assert body["stats"] is not None
    assert isinstance(body["stats"]["count"], int)


def test_the_counts_can_be_left_out(as_admin):
    """The one part of the status whose cost grows with the library."""
    body = as_admin.get("/api/status?stats=0").get_json()
    assert "stats" not in body
    # Everything the page needs to draw itself is still there.
    assert body["user"]
    assert "has_library" in body
    assert "ai" in body


def test_the_counts_have_an_endpoint_of_their_own(as_admin):
    plain = as_admin.get("/api/status").get_json()["stats"]
    alone = as_admin.get("/api/status/stats").get_json()["stats"]
    assert alone["count"] == plain["count"]
    assert alone["pictures"] == plain["pictures"]


def test_the_counts_are_not_cached(as_admin):
    """They climb while a scan runs; a cached answer would freeze the sidebar."""
    response = as_admin.get("/api/status/stats")
    assert response.headers.get("Cache-Control") == "no-store"


def test_the_counts_respect_what_a_viewer_may_see(as_family, as_admin):
    """Same rule as /api/status: splitting it out must not widen it."""
    theirs = as_family.get("/api/status/stats").get_json()["stats"]
    everything = as_admin.get("/api/status/stats").get_json()["stats"]
    if theirs is None:
        return
    assert theirs["count"] <= everything["count"]


def test_a_library_that_is_not_set_up_says_so_rather_than_failing(app, as_admin):
    app.config["MV_CONFIG"].roots = []
    app.config["MV_CONFIG"].active_root = None
    assert as_admin.get("/api/status/stats").get_json()["stats"] is None


# -- the index that makes the count cheap -----------------------------------

def test_the_join_behind_the_counts_walks_an_index_not_the_vectors(tmp_path):
    """The 23 seconds, in one query plan.

    `embeddings.asset_id` is the rowid, so without an index on it the join
    probes each row by rowid — which reads the page that row's vector sits
    on. Counting is then proportional to the *size* of the vectors rather
    than to how many there are.
    """
    conn = db.init_db(tmp_path / "index.db")
    plan = " ".join(
        row["detail"] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM embeddings e "
            "JOIN assets a ON a.id = e.asset_id WHERE a.root = ?",
            ("/library",)))
    assert "idx_embeddings_asset" in plan, plan
    assert "COVERING INDEX idx_embeddings_asset" in plan, plan


def test_the_index_is_made_for_a_library_that_predates_it(tmp_path):
    """It is in the schema, so opening an older index creates it."""
    conn = db.init_db(tmp_path / "index.db")
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_embeddings_asset'").fetchone()
    assert found


# -- the gallery does not wait ----------------------------------------------
#
# These read the script, the way tests/test_scan_says_where.py does. The
# behaviour they protect only shows up against a real library on a cold
# cache, which no unit test has — but the shape that causes it is plain in
# the source, and that is what must not come back.

def app_js() -> str:
    return (SCRIPTS / "app.js").read_text(encoding="utf-8")


def test_the_status_is_asked_for_without_the_counts():
    api = (SCRIPTS / "api.js").read_text(encoding="utf-8")
    assert "'/api/status?stats=0'" in api, "the gallery is asking for the counts again"
    assert "/api/status/stats" in api


def test_the_counts_are_not_awaited():
    """Awaiting them is the whole bug: nothing on screen needs them."""
    source = app_js()
    match = re.search(r"^\s*refreshCounts\(\);", source, re.M)
    assert match, "refreshCounts is not called during start-up"
    assert "await refreshCounts()" not in source


def test_the_photographs_are_asked_for_in_the_same_breath():
    assert re.search(r"await Promise\.all\(\[reload\(\)", app_js())


# -- the cover comes off ----------------------------------------------------
#
# An overlay that outlives its page is a worse bug than the empty page it
# replaced, because nothing behind it can be read or clicked.

def test_every_path_that_never_reaches_the_gallery_uncovers_it():
    source = app_js()
    for where in ("gate.show(auth, 'setup')", "gate.show(auth, 'picker')"):
        before = source[:source.index(where)]
        assert before.rstrip().endswith("bootDone();"), where


def test_the_offline_notice_is_not_left_underneath_it():
    source = app_js()
    after = source[source.index("function setServerOffline"):][:400]
    assert "bootDone();" in after


def test_there_is_a_failsafe():
    """For the path nobody thought of."""
    assert re.search(r"setTimeout\(\(\) => bootDone\(\), \d+\)", app_js())


def test_the_overlay_is_in_the_markup_not_built_by_script():
    """So it is on screen before app.js has parsed."""
    page = (Path(__file__).resolve().parent.parent / "ninaivu" / "templates"
            / "index.html").read_text(encoding="utf-8")
    assert 'id="boot"' in page
    assert 'id="boot-fill"' in page
    assert 'id="boot-percent"' in page


def test_the_steps_end_at_a_hundred_and_only_go_forward():
    source = app_js()
    steps = re.search(r"const BOOT_STEPS = \[(.*?)\];", source, re.S)
    assert steps, "BOOT_STEPS is gone"
    weights = [int(n) for n in re.findall(r",\s*(\d+),", steps.group(1))]
    assert weights == sorted(weights), weights
    assert weights[-1] == 100, weights


@pytest.mark.parametrize("element", ["#boot", "#boot-fill", "#boot-percent",
                                     "#boot-step"])
def test_the_script_and_the_markup_agree(element):
    assert element in app_js()
    page = (Path(__file__).resolve().parent.parent / "ninaivu" / "templates"
            / "index.html").read_text(encoding="utf-8")
    assert f'id="{element[1:]}"' in page
