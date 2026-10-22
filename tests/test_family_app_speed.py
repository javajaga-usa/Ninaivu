"""The family app opening faster on a large library (2.1).

Measured on a library of 200,000 photographs: the first tiles came after
2.3 s and the page went quiet after 8 s; now 1.0 s and 5.2 s, and 0.4 s and
1.6 s on a second device or a new tab.

* The sidebar's count of "Only to me" items read the whole library on every
  open: its index held a NULL for every photograph, so ANALYZE said a lookup
  matched all of them and SQLite walked the table instead (120 ms a call).
* The gallery's layout of a whole library, about two seconds to read and
  shape, is kept by its tag, so a second device, a new tab or a cleared cache
  is answered from memory.
* An administrator's page asked for the counts and every sidebar list a
  second time on its first activity tick.
"""

from pathlib import Path

from ninaivu.api import api
from ninaivu.storage import db


def test_the_only_to_me_count_uses_its_index_after_analyze(scanned):
    _, conn, _ = scanned
    conn.execute("ANALYZE")
    plan = " ".join(str(row[3]) for row in conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM assets a WHERE a.root = ? "
        "AND a.private_to = ? AND a.trashed = 0 AND a.live_clip = 0 AND a.raw_twin = 0",
        ("x", 1)))
    assert "idx_assets_private" in plan, plan


def test_an_old_whole_index_is_made_partial(scanned):
    cfg, conn, _ = scanned
    conn.execute("DROP INDEX idx_assets_private")
    conn.execute("CREATE INDEX idx_assets_private ON assets(private_to)")
    conn.commit()
    db.init_db(cfg.db_path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_assets_private'").fetchone()[0]
    assert "WHERE private_to IS NOT NULL" in sql


def test_a_layout_asked_for_again_is_answered_from_memory(as_family, monkeypatch):
    api._SEGMENT_CACHE.clear()
    first = as_family.get("/api/segments?sort=date_desc")
    assert first.status_code == 200 and first.headers.get("ETag")
    asked = []
    real = db.query_assets
    monkeypatch.setattr(db, "query_assets", lambda *a, **k: asked.append(1) or real(*a, **k))
    again = as_family.get("/api/segments?sort=date_desc")      # a new tab: no If-None-Match
    assert again.status_code == 200 and not asked
    assert again.get_json() == first.get_json()
    assert again.headers["ETag"] == first.headers["ETag"]


def test_a_change_to_the_library_is_never_answered_from_memory(as_admin, as_family, people):
    api._SEGMENT_CACHE.clear()
    before = {i[0] for s in as_family.get("/api/segments").get_json()["segments"]
              for i in s["items"]}
    target = sorted(before)[0]
    as_admin.post("/api/visibility", json={"ids": [target], "visibility": "hidden"})
    after = {i[0] for s in as_family.get("/api/segments").get_json()["segments"]
             for i in s["items"]}
    assert target in before and target not in after


def test_the_kept_layouts_stay_within_their_bound(monkeypatch):
    api._SEGMENT_CACHE.clear()
    monkeypatch.setattr(api, "_SEGMENT_CACHE_BYTES", 10)
    api._segments_keep("a", b"12345678")
    api._segments_keep("b", b"12345678")
    assert list(api._SEGMENT_CACHE) == ["b"]
    api._SEGMENT_CACHE.clear()


def test_the_first_activity_tick_does_not_load_the_page_again():
    app_js = (Path(__file__).resolve().parents[1] / "ninaivu" / "static" / "js"
              / "app.js").read_text(encoding="utf-8")
    assert "if (lastPhase === null && (scan.status === 'done' || scan.status === 'error'))" \
        in app_js
