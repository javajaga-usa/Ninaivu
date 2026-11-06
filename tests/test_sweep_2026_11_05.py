"""Whole-app sweep of 5 November 2026: searches that count less, a gallery
layout built without a dict for every row, and a tray that does not wait on
the program it opens a file with."""

import subprocess
import sys
import time

import pytest

from ninaivu.storage import db, descriptions


def _counting(conn):
    said = []
    conn.set_trace_callback(lambda sql: said.append(sql) if sql.lstrip().upper().startswith("SELECT COUNT(*)") else None)
    return said


def test_a_search_that_fits_in_one_page_is_not_counted_again(scanned):
    cfg, conn, _ = scanned
    roots = list(cfg.roots)
    said = _counting(conn)
    try:
        rows, total = db.query_assets(conn, roots, text="beach", limit=10_000, max_visibility=2)
    finally:
        conn.set_trace_callback(None)
    assert rows and total == len(rows)
    assert said == []                         # the page said how many there were


def test_the_last_short_page_says_the_total_and_a_full_one_is_counted(scanned):
    cfg, conn, _ = scanned
    roots = list(cfg.roots)
    everything, whole = db.query_assets(conn, roots, limit=10_000, max_visibility=2)
    assert whole == len(everything) > 3
    said = _counting(conn)
    try:
        first, total_first = db.query_assets(conn, roots, limit=2, max_visibility=2)
        last, total_last = db.query_assets(conn, roots, limit=2, offset=whole - 1, max_visibility=2)
        past, total_past = db.query_assets(conn, roots, limit=2, offset=whole + 5, max_visibility=2)
    finally:
        conn.set_trace_callback(None)
    assert (len(first), total_first) == (2, whole)       # full: counted
    assert (len(last), total_last) == (1, whole)         # short: offset + what came back
    assert (past, total_past) == ([], whole)             # past the end: counted, not guessed
    assert len(said) == 2


def test_a_word_search_is_counted_once_while_it_is_scrolled(scanned, monkeypatch):
    cfg, conn, _ = scanned
    roots = list(cfg.roots)
    with db._aggregates_lock:
        db._AGGREGATES.clear()
    said = _counting(conn)
    try:
        for offset in (0, 1):
            db.query_assets(conn, roots, text="beach", limit=1, offset=offset,
                            remember_total=True, max_visibility=2)
    finally:
        conn.set_trace_callback(None)
    assert len(said) == 1
    # Kept briefly: once that has passed, counted afresh.
    monkeypatch.setattr(db, "WORDS_TOTAL_SECONDS", 0.0)
    said = _counting(conn)
    try:
        db.query_assets(conn, roots, text="beach", limit=1, remember_total=True, max_visibility=2)
    finally:
        conn.set_trace_callback(None)
    assert len(said) == 1


def test_a_description_just_written_is_counted_at_once(scanned):
    """A total kept for a while must not hide what was just described: a
    search that fits one page is never answered from what was kept."""
    cfg, conn, _ = scanned
    roots = list(cfg.roots)
    assert db.query_assets(conn, roots, text="kuthuvilakku", remember_total=True,
                           max_visibility=2)[1] == 0
    asset_id = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
    descriptions.store(conn, asset_id, "Grandmother lighting a kuthuvilakku",
                       model="captions", version=1)
    rows, total = db.query_assets(conn, roots, text="kuthuvilakku", remember_total=True,
                                  max_visibility=2)
    assert [r["id"] for r in rows] == [asset_id] and total == 1


def test_word_search_reads_descriptions_and_the_index_as_one_list(scanned):
    cfg, conn, _ = scanned
    roots = list(cfg.roots)
    asset_id = conn.execute("SELECT id FROM assets WHERE filename NOT LIKE '%beach%' LIMIT 1").fetchone()[0]
    descriptions.store(conn, asset_id, "a beach at dusk", model="captions", version=1)
    plans = []
    conn.set_trace_callback(plans.append)
    try:
        rows, total = db.query_assets(conn, roots, text="beach", limit=10_000, max_visibility=2)
    finally:
        conn.set_trace_callback(None)
    assert asset_id in {r["id"] for r in rows} and total == len(rows)
    page = next(sql for sql in plans if "ORDER BY" in sql and "assets_fts" in sql)
    assert " UNION SELECT rowid FROM descriptions_fts" in page and " OR a.id IN" not in page


def test_the_gallery_layout_is_the_same_by_position(as_family):
    """The layout as it was built from named rows, item for item."""
    from ninaivu.api import api as api_mod
    from ninaivu.media import media
    answer = as_family.get("/api/segments").get_json()
    items = [item for seg in answer["segments"] for item in seg["items"]]
    assert items and answer["total"] == len(items)
    conn = db.connect(as_family.application.config["MV_CONFIG"].db_path)
    recipe = items[0][5].partition("t")[2]          # the thumbnail settings' token
    for item in items:
        row = dict(conn.execute("SELECT * FROM assets WHERE id=?", (item[0],)).fetchone())
        assert item[5] == media.thumb_version(row, recipe)
        assert item[6] == (api_mod._swatch(row.get("color")) if row.get("thumb") else "")


@pytest.mark.skipif(sys.platform == "win32", reason="opened with os.startfile there")
def test_the_tray_opens_a_file_without_waiting_for_it(monkeypatch, tmp_path):
    from ninaivu.desktop import tray
    started = []

    class Started:
        def __init__(self, command, **kwargs):
            started.append((command, kwargs))
    monkeypatch.setattr(tray.subprocess, "Popen", Started)
    monkeypatch.setattr(tray.subprocess, "run", lambda *a, **k: pytest.fail("waited on the opener"))
    began = time.monotonic()
    tray.open_file(tmp_path / "server.log")
    assert time.monotonic() - began < 1
    command, kwargs = started[0]
    assert command[0] in ("open", "xdg-open") and kwargs["stdin"] is subprocess.DEVNULL
