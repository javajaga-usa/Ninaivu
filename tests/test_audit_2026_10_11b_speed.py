"""Sixth audit, speed and data safety at scale (11 October 2026).

Measured on a synthetic index of 208,000 items (14,184 Live pairs, 2,886
RAW+JPEG pairs, 41 albums one of 50,000 items, 400,000 faces, 9,000
favourites). These pin what was found there with plans, counts and states
rather than timings, which a shared runner cannot keep.
"""

from __future__ import annotations

import json
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
