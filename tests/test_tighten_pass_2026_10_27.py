"""Console pages that read the whole library again on every open, made to
remember or to read less, without answering differently (measured with
tools/bench_app.py on a library of 210,000 items).

Both folder pickers share one remembered count of each folder; suggested
locations are kept until a place or a time changes; the duplicates index is
partial, so finding a tenth of the library reads a tenth; the largest-files
count reads the size index; the Space screen's growth is one read, not three.
"""
import time

from ninaivu.server import auth
from ninaivu.storage import db, space

ROOT = "/lib"
DAY = 86400.0


def _add(conn, rel, *, size=1000, dup=None, added=None, when=None):
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, kind, size, dup_group, "
        "added_at, captured_at, date_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ROOT, rel, rel.rsplit("/", 1)[-1], rel.rsplit("/", 1)[0] if "/" in rel else "",
         "picture", size, dup, added, when, "2024-01-01"))
    conn.commit()
    return conn.execute("SELECT id FROM assets WHERE rel_path=?", (rel,)).fetchone()[0]


# -- folder pickers -----------------------------------------------------------

def test_both_folder_pickers_count_the_same_and_follow_the_library(as_admin, scanned):
    _, conn, _ = scanned
    people = as_admin.get("/api/people/folders").get_json()["folders"]
    assignable = [f for f in as_admin.get("/api/admin/assignable").get_json()["folders"]
                  if not f["is_root"]]
    assert people and {f["path"].split("/")[-1]: f["count"] for f in people} == \
        {f["label"]: f["count"] for f in assignable}
    total = sum(f["count"] for f in people if f["depth"] == 0)

    # Into the bin: both are told at once, not the count from before.
    asset = conn.execute("SELECT id FROM assets WHERE trashed=0 AND folder != '' "
                         "LIMIT 1").fetchone()[0]
    conn.execute("UPDATE assets SET trashed=1 WHERE id=?", (asset,))
    conn.commit()
    again = as_admin.get("/api/people/folders").get_json()["folders"]
    assert sum(f["count"] for f in again if f["depth"] == 0) == total - 1


def test_the_kept_counts_are_handed_over_as_they_are(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    _add(conn, "2024/01/a.jpg")
    _add(conn, "2024/01/b.jpg")
    _add(conn, "top.jpg")
    total, rows = db.folder_counts(conn, ROOT)
    assert total == 3 and rows == [{"folder": "2024/01", "n": 2}]
    # Remembered: the same object, not read again.
    assert db.folder_counts(conn, ROOT)[1] is rows


# -- suggested locations ------------------------------------------------------

def test_suggested_locations_are_kept_until_a_place_or_a_time_moves(as_admin, scanned):
    _, conn, _ = scanned
    first, second, third = [r[0] for r in conn.execute(
        "SELECT id FROM assets ORDER BY id LIMIT 3")]
    conn.execute("UPDATE assets SET gps_lat = NULL, gps_lon = NULL, captured_at = NULL")
    conn.execute("UPDATE assets SET gps_lat = 11.41, gps_lon = 76.69, city = 'Ooty', "
                 "country = 'IN', captured_at = 1563700000, date_source = 'exif' WHERE id = ?",
                 (first,))
    conn.execute("UPDATE assets SET captured_at = 1563701000, date_source = 'exif' "
                 "WHERE id = ?", (second,))
    conn.commit()
    found = as_admin.get("/api/admin/locations?hours=1").get_json()
    assert found["total"] == 1 and found["groups"][0]["ids"] == [second]

    # A time moved: the third is now within the hour, and suggested.
    conn.execute("UPDATE assets SET captured_at = 1563702000, date_source = 'exif' "
                 "WHERE id = ?", (third,))
    conn.commit()
    found = as_admin.get("/api/admin/locations?hours=1").get_json()
    assert sorted(found["groups"][0]["ids"]) == sorted([second, third])

    # Filled in: nothing is left to suggest, and the count says so at once.
    assert as_admin.post("/api/admin/locations/apply",
                         json={"hours": 1, "all": True}).get_json()["applied"] == 2
    found = as_admin.get("/api/admin/locations?hours=1").get_json()
    assert found["total"] == 0 and found["inferred"] == 2
    assert as_admin.post("/api/admin/locations/undo").get_json()["undone"] == 2
    assert as_admin.get("/api/admin/locations?hours=1").get_json()["total"] == 2


# -- the indexes the plans read -----------------------------------------------

def _plan(conn, sql, params=()):
    return " ".join(str(r[-1]) for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))


def test_duplicates_are_found_through_their_own_small_index(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    auth.init_auth_schema(conn)
    for n in range(200):
        _add(conn, f"2024/01/{n}.jpg", dup=(n // 2 if n < 20 else None))
    index = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_assets_dupgroup'"
                         ).fetchone()[0]
    assert "WHERE dup_group IS NOT NULL" in index
    groups = db.duplicate_groups(conn, [ROOT], max_visibility=2)
    assert len(groups) == 10 and all(len(g["items"]) == 2 for g in groups)
    conn.execute("PRAGMA analysis_limit=0")
    conn.execute("ANALYZE")
    assert "idx_assets_dupgroup" in _plan(
        conn, "SELECT a.id FROM assets a WHERE a.root=? AND a.trashed=0 AND a.nsfw=0 "
              "AND a.visibility <= 2 AND a.dup_group IS NOT NULL", (ROOT,))


def test_an_index_from_before_is_made_partial_on_the_next_start(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    conn.execute("DROP INDEX idx_assets_dupgroup")
    conn.execute("CREATE INDEX idx_assets_dupgroup ON assets(dup_group)")
    conn.commit()
    db.close_all()
    db._READY.discard(str(path))                         # noqa: SLF001
    conn = db.init_db(path)
    index = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_assets_dupgroup'"
                         ).fetchone()[0]
    assert "WHERE dup_group IS NOT NULL" in index


def test_the_largest_files_count_reads_the_size_index(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    auth.init_auth_schema(conn)
    for n in range(300):
        _add(conn, f"2024/01/{n}.jpg", size=(900 << 20) if n < 3 else 1000)
    conn.execute("PRAGMA analysis_limit=0")
    conn.execute("ANALYZE")
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    rows, total = db.query_assets(conn, [ROOT], max_visibility=2, min_size=500 << 20,
                                  exclude_reviewed_large=True, sort="size_desc", limit=200)
    conn.set_trace_callback(None)
    assert total == 3 and [r["size"] for r in rows] == [900 << 20] * 3
    count = next(s for s in seen if s.startswith("SELECT COUNT(*)"))
    assert "idx_assets_size" in _plan(conn, count)


# -- growth -------------------------------------------------------------------

def test_growth_is_read_in_one_pass_and_says_what_it_said(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    now = time.time()
    # Taken as they arrived: the household's own.
    _add(conn, "new/a.jpg", size=100, added=now - 10 * DAY, when=now - 11 * DAY)
    _add(conn, "new/b.jpg", size=200, added=now - 200 * DAY, when=now - 200 * DAY)
    # An old archive imported this year, and one from before the year.
    _add(conn, "old/c.jpg", size=5000, added=now - 30 * DAY, when=now - 3000 * DAY)
    _add(conn, "old/d.jpg", size=7000, added=now - 400 * DAY, when=now - 400 * DAY)
    # No time taken, so its file's time (none: 0), as before; and one in the
    # bin, which never counts.
    _add(conn, "none/e.jpg", size=9, added=now - 5 * DAY)
    gone = _add(conn, "new/f.jpg", size=50, added=now - 5 * DAY, when=now - 5 * DAY)
    conn.execute("UPDATE assets SET trashed=1 WHERE id=?", (gone,))
    conn.commit()
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    out = space.growth(conn, [ROOT], now=now)
    conn.set_trace_callback(None)
    assert len([s for s in seen if s.lstrip().upper().startswith("SELECT")]) == 1
    assert out["new_files"] == 2 and out["new_bytes"] == 300
    assert out["imported_files"] == 2 and out["imported_bytes"] == 5009
    assert out["history_days"] == 365.0
    assert out["bytes_per_month"] == round(300 / 365.0 * space.MONTH_DAYS)


def test_growth_of_an_empty_library_is_nothing(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    out = space.growth(conn, [ROOT])
    assert out["history_days"] == 0.0 and out["new_files"] == 0
    assert out["imported_bytes"] == 0 and out["bytes_per_month"] is None
