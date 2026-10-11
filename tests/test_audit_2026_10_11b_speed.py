"""Sixth audit, speed and data safety at scale (11 October 2026).

Measured on a synthetic index of 208,000 items (14,184 Live pairs, 2,886
RAW+JPEG pairs, 41 albums one of 50,000 items, 400,000 faces, 9,000
favourites). These pin what was found there with plans, counts and states
rather than timings, which a shared runner cannot keep.
"""

from __future__ import annotations

import json
import math
import threading

import pytest

from ninaivu.api import admin_api
from ninaivu.media import video_compress as vc
from ninaivu.server import auth
from ninaivu.storage import db
from test_large_library import ROOT, _fill, _open, _plans


def _on_a_thread(work):
    """Run *work* on a fresh thread, as start-up runs on its own, and hand back
    what it returned or raised."""
    out: dict = {}

    def run():
        try:
            out["value"] = work()
        except BaseException as exc:                     # noqa: BLE001
            out["error"] = exc
        finally:
            db.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(30)
    if "error" in out:
        raise out["error"]
    return out.get("value")


# --- P-01: start-up ran on a closed connection ------------------------------

def test_carrying_on_compressions_leaves_the_start_up_threads_connection_open(cfg):
    """resume_compressions closed the thread's own connection, the one every
    later step of start-up was handed: no interrupted job was carried on, the
    screenshot rule and the warm-up never ran, and every start walked the
    whole library."""
    db.init_db(cfg.db_path)
    vc._reset_for_tests()

    def boot():
        db.connect(cfg.db_path).execute("SELECT 1")
        admin_api.resume_compressions(cfg)
        return db.connect(cfg.db_path).execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    try:
        assert _on_a_thread(boot) == 0
    finally:
        vc._reset_for_tests()


def test_a_connection_somebody_closed_is_opened_again(tmp_path):
    path = tmp_path / "index.db"
    db.init_db(path).close()
    again = db.connect(path)
    assert again.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert db.connect(path) is again


# --- P-02: the favourites walked the whole library ---------------------------

@pytest.fixture(scope="module")
def library_with_favourites(tmp_path_factory):
    """Twenty thousand rows, three hundred of them one viewer's favourites,
    measured: the shape of the 208,000-item index at a test's size."""
    path = tmp_path_factory.mktemp("favs") / "index.db"
    conn = _open(path)
    _fill(conn, 20_000)
    viewer = auth.create_user(conn, "maya", "summerdays24", role=auth.ROLE_FAMILY)
    conn.executemany(
        "INSERT INTO user_assets(user_id, asset_id, favorite, rating) VALUES (?,?,1,?)",
        [(viewer.id, i, i % 4) for i in range(1, 20_000, 67)])
    conn.commit()
    assert db.refresh_statistics(conn, force=True)
    return path, viewer.id


@pytest.mark.parametrize("asked", [dict(favorites=True), dict(min_rating=2)])
def test_a_viewers_favourites_are_read_from_their_own_rows(library_with_favourites, asked):
    """Written through COALESCE, the filter kept the join an outer one: SQLite
    walked every photograph in date order and probed for a favourite at each
    (252 ms for 8,600 of 208,000). Said plainly, it starts from the viewer's
    own rows and sorts those (77 ms)."""
    path, viewer = library_with_favourites
    conn = _open(path)
    plans = _plans(conn, viewer_id=viewer, max_visibility=1, limit=25_000, **asked)
    for kind in ("count", "page"):
        first = plans[kind][1].split(" | ")[0]
        assert first.split()[:2] in (["SEARCH", "ua"], ["SCAN", "ua"]), plans[kind]
    rows, total = db.query_assets(conn, [ROOT], viewer_id=viewer, max_visibility=1,
                                  limit=25_000, **asked)
    expected = conn.execute(
        "SELECT COUNT(*) FROM assets a JOIN user_assets ua ON ua.asset_id = a.id "
        "AND ua.user_id = ? WHERE a.visibility <= 1 AND a.kind <> 'audio' AND "
        + ("ua.favorite = 1" if "favorites" in asked else "ua.rating >= 2"),
        (viewer,)).fetchone()[0]
    assert total == len(rows) == expected > 0


def test_a_rating_floor_of_nothing_still_lists_everything(library_with_favourites):
    path, viewer = library_with_favourites
    conn = _open(path)
    _, everything = db.query_assets(conn, [ROOT], viewer_id=viewer, max_visibility=1)
    _, floor = db.query_assets(conn, [ROOT], viewer_id=viewer, max_visibility=1,
                               min_rating=-1)
    assert floor == everything


# --- P-03: the search box read every row's tags a second time ----------------

def test_the_sidebar_and_the_search_box_share_one_reading_of_the_tags(tmp_path, monkeypatch):
    conn = _open(tmp_path / "index.db")
    conn.executemany(
        "INSERT INTO assets(root, rel_path, filename, folder, kind, tags, camera, date_key) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(ROOT, f"f{i % 300}/img{i}.jpg", f"img{i}.jpg", f"f{i % 300}", "picture",
          json.dumps([f"tag{i % 250}", f"tag{i % 7}"]), f"cam{i % 60}", f"20{10 + i % 9}-01-01")
         for i in range(3000)])
    conn.commit()
    reads = []
    real = db._facets
    monkeypatch.setattr(db, "_facets", lambda *a: reads.append(a[-1]) or real(*a))

    sidebar = db.facets(conn, [ROOT])
    search_box = db.facets(conn, [ROOT], limit=200)
    assert len(reads) == 1
    for kind in ("tags", "folders", "cameras"):
        assert sidebar[kind] == search_box[kind][:40]
        assert len(sidebar[kind]) == 40 and len(search_box[kind]) > 40
    assert sidebar["years"] == search_box["years"]
    # Cut from the longer list, the sidebar is what it always was.
    assert sidebar == real(conn, *_facet_guard(), 40)


def _facet_guard():
    guard = ("root IN (?) AND trashed=0 AND live_clip=0 AND raw_twin=0 AND "
             + db.visibility_clause("", 1) + " AND nsfw=0")
    return guard, [ROOT, 1]


# --- P-04: the album list read every album's items on every visit ------------

def _album_passes(conn, call):
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        out = call()
    finally:
        conn.set_trace_callback(None)
    return out, sum("GROUP BY ai.album_id" in sql for sql in seen)


def test_the_album_list_is_remembered_until_an_album_or_the_library_changes(tmp_path):
    """Two grouped passes over every album's items, on every visit to Albums:
    0.45 s of the 0.7 s it took on 208,000 photographs with 100,000 in albums.
    Remembered, a second visit reads none of them (1 ms)."""
    conn = _open(tmp_path / "index.db")
    _fill(conn, 600)
    big = db.create_album(conn, "Everything")
    small = db.create_album(conn, "Holiday")
    db.album_add(conn, big, list(range(1, 501)))
    db.album_add(conn, small, [2, 5, 8])     # (id - 1) % 3 < 2: the family sees them
    family = dict(max_visibility=1)

    def listed():
        albums, passes = _album_passes(
            conn, lambda: db.list_albums(conn, [ROOT], **family))
        return {a["name"]: a["n"] for a in albums}, passes

    first, passes = listed()
    assert passes == 2
    again, passes = listed()
    assert (again, passes) == (first, 0)

    # A photograph joins an album: worked out again, and right.
    db.album_add(conn, small, [11])
    after_add, passes = listed()
    assert passes == 2 and after_add["Holiday"] == first["Holiday"] + 1

    # One of them is hidden from the family: the library's counter moves.
    with db._write_lock:
        conn.execute("UPDATE assets SET visibility=2 WHERE id=11")
        conn.commit()
    after_hide, passes = listed()
    assert passes == 2 and after_hide["Holiday"] == first["Holiday"]

    db.album_remove(conn, small, [2])
    after_remove, _ = listed()
    assert after_remove["Holiday"] == first["Holiday"] - 1
    assert after_remove == _fresh_albums(conn, family)


def test_the_album_counter_moves_only_for_album_items(tmp_path):
    conn = _open(tmp_path / "index.db")
    _fill(conn, 10)
    album = db.create_album(conn, "A")
    before = db.meta_counter(conn, db.ALBUMS_GENERATION_KEY)
    db.update_album(conn, album, name="B")
    assert db.meta_counter(conn, db.ALBUMS_GENERATION_KEY) == before
    db.album_add(conn, album, [1, 2])
    moved = db.meta_counter(conn, db.ALBUMS_GENERATION_KEY)
    assert moved != before
    db.delete_album(conn, album)                 # its items go by cascade
    assert db.meta_counter(conn, db.ALBUMS_GENERATION_KEY) != moved


def _fresh_albums(conn, limits):
    db._AGGREGATES.clear()
    return {a["name"]: a["n"] for a in db.list_albums(conn, [ROOT], **limits)}


# --- P-05: adding to an album read the whole album back ----------------------

def test_adding_to_an_album_counts_it_in_one_query(app, people, scanned, monkeypatch):
    """The answer's count read every id in the album and narrowed them 500 at
    a time: a hundred queries, 240 ms, for each addition to a 50,000-photograph
    album. Pieces of two here, so the old way shows at the fixture's size."""
    from conftest import FAMILY, login
    from ninaivu.api import api as api_module

    monkeypatch.setattr(api_module, "_IN_CHUNK", 2)
    _, conn, _ = scanned
    client = login(app.test_client(), *FAMILY)
    ids = [r["id"] for r in client.get("/api/assets?limit=200").get_json()["items"]]
    album = client.post("/api/albums", json={"name": "Counted", "ids": ids[:2]}).get_json()["id"]

    statements = []
    db.connect(app.config["MV_SERVICES"].cfg.db_path).set_trace_callback(statements.append)
    try:
        answer = client.post(f"/api/albums/{album}/items",
                             json={"ids": ids[2:], "remove": False}).get_json()
    finally:
        db.connect(app.config["MV_SERVICES"].cfg.db_path).set_trace_callback(None)
    narrowed = [s for s in statements if s.startswith("SELECT") and "a.id IN" in s]
    assert answer["count"] == len(ids)
    # Only the ids being added are narrowed (in pieces of two); the count is
    # one more query, not the whole album again.
    assert len(narrowed) == -(-len(ids[2:]) // 2)
    removed = client.post(f"/api/albums/{album}/items",
                          json={"ids": ids[:1], "remove": True}).get_json()
    assert removed["count"] == len(ids) - 1


# --- P-06: the whole layout of a large library went uncompressed -------------

def test_a_layout_over_eight_megabytes_is_still_compressed():
    """An administrator's whole gallery at 208,000 items is 8.6 MB of JSON
    (1.5 MB gzipped) and went as it was: the 8 MB ceiling, there to keep a
    large file from being read into memory, was applied to answers already
    built in memory too. A file is still held to it."""
    import gzip

    from flask import Flask

    import ninaivu

    app = Flask(__name__)
    items = [[i, 133, 1, 2, 0, "1700000000r0", "abc"] for i in range(400_000)]
    body = json.dumps({"segments": [{"key": "2024-01-01", "items": items}]})
    assert len(body) > ninaivu._GZIP_MAX_BYTES
    with app.test_request_context("/api/segments", headers={"Accept-Encoding": "gzip"}):
        response = app.response_class(body, mimetype="application/json")
        ninaivu._gzip_text(response)
        assert response.headers.get("Content-Encoding") == "gzip"
        assert json.loads(gzip.decompress(response.get_data())) == json.loads(body)


# --- P-07: the write-ahead log never shrank ----------------------------------

def test_the_write_ahead_log_is_cut_back_once_it_starts_over(tmp_path, monkeypatch):
    """A burst written while a reader holds an old snapshot grows the log (1.1
    GB for 40,000 tag writes beside three readers at 208,000 items). Reused
    rather than shrunk, it stayed that size on disk for good."""
    import os
    import sqlite3

    monkeypatch.setattr(db, "WAL_SIZE_LIMIT", 256 * 1024, raising=False)
    path = tmp_path / "index.db"
    log = f"{path}-wal"
    conn = db.connect(path)
    conn.execute("CREATE TABLE burst(x)")
    conn.commit()
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM burst").fetchone()
    for _ in range(150):                            # past the 1,000-page checkpoint
        conn.execute("INSERT INTO burst VALUES (?)", (os.urandom(50_000),))
        conn.commit()
    assert os.path.getsize(log) > 4 * 1024 * 1024
    reader.rollback()
    reader.close()
    for _ in range(2):                              # checkpointed, then started over
        conn.execute("INSERT INTO burst VALUES (1)")
        conn.commit()
    assert os.path.getsize(log) <= 256 * 1024


# --- P-08: the gallery's rows were fetched one GIL hand-over at a time -------

_LEAN = ("id", "width", "kind", "gps_lat", "duration", "color", "date_key")


def _lean_page(conn, viewer, **asked):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        rows, _ = db.query_assets(conn, [ROOT], viewer_id=viewer, max_visibility=1,
                                  limit=25_000, columns=_LEAN, with_total=False, **asked)
    finally:
        conn.set_trace_callback(None)
    return rows, statements


def test_the_gallerys_rows_come_back_in_one_step_and_unchanged(library_with_favourites):
    """sqlite3 hands the GIL back at every row it steps. With the scanner busy
    in Python in the same process, the gallery's first page (25,000 rows,
    0.35 s idle) took 4 to 10 s, and the bare fetch 155 s against one spinning
    thread. Read as one JSON value it is one step: 99 ms busy."""
    path, viewer = library_with_favourites
    conn = _open(path)
    a, b = (r["id"] for r in _lean_page(conn, viewer)[0][:2])
    conn.execute("UPDATE assets SET gps_lat = ?, duration = 1e-300 WHERE id = ?",
                 (0.1 + 0.2, a))
    conn.execute("UPDATE assets SET color = 'say \"cheese\" \\ ☺' WHERE id = ?", (b,))
    conn.commit()
    try:
        rows, statements = _lean_page(conn, viewer)
        assert any("json_group_array" in s for s in statements), statements
        full, _ = db.query_assets(conn, [ROOT], viewer_id=viewer, max_visibility=1,
                                  limit=25_000, with_total=False)
        assert rows == [{k: r[k] for k in (*_LEAN, "favorite", "rating")} for r in full]
        assert [type(r["gps_lat"]) for r in rows] == [type(r["gps_lat"]) for r in full]
        first = next(r for r in rows if r["id"] == a)
        assert first["gps_lat"] == 0.1 + 0.2
        # Near the bottom of the range some SQLite builds (macOS's) print the
        # last of the 17 digits a step off; anything a photograph holds is exact.
        assert math.isclose(first["duration"], 1e-300, rel_tol=1e-15)
        assert next(r for r in rows if r["id"] == b)["color"] == 'say "cheese" \\ ☺'
        assert any(r["favorite"] is True and r["rating"] for r in rows)
        empty, _ = _lean_page(conn, viewer, text="nothing-at-all-called-this")
        assert empty == []
    finally:
        conn.execute("UPDATE assets SET gps_lat = NULL, duration = NULL WHERE id = ?", (a,))
        conn.execute("UPDATE assets SET color = NULL WHERE id = ?", (b,))
        conn.commit()


def test_a_column_holding_bytes_is_still_read(library_with_favourites):
    path, viewer = library_with_favourites
    conn = _open(path)
    c = _lean_page(conn, viewer)[0][2]["id"]
    conn.execute("UPDATE assets SET color = x'00ff' WHERE id = ?", (c,))
    conn.commit()
    try:
        rows, _ = _lean_page(conn, viewer)
        assert next(r for r in rows if r["id"] == c)["color"] == b"\x00\xff"
    finally:
        conn.execute("UPDATE assets SET color = NULL WHERE id = ?", (c,))
        conn.commit()
