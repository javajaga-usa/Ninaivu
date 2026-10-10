"""A million-photo library must stay browsable.

Measured on a synthetic library of 1,000,000 rows, a gallery page took about
nine seconds: SQLite sorted the whole table for every page, because no index
matched the gallery's date order, and the total was counted through an index
that did not hold ``nsfw`` or ``kind``, so every row was read. These tests pin
the query plans rather than timings — a plan is deterministic, and a timing on
a shared CI runner is not.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from ninaivu import ai
from ninaivu.server import auth
from ninaivu.storage import db

ROOT = "D:/lib"
FAMILY_VIEW = dict(max_visibility=1)


def _open(path):
    conn = db.init_db(path)
    auth.init_auth_schema(conn)
    return conn


def _fill(conn, n, start=0):
    base = 1.2e9
    conn.executemany(
        "INSERT INTO assets(root, rel_path, filename, folder, kind, visibility, "
        "captured_at, mtime, date_key, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,0)",
        ((ROOT, f"f{i // 200}/img{i}.jpg", f"img{i}.jpg", f"f{i // 200}",
          "audio" if i % 50 == 0 else "picture", i % 3,
          None if i % 7 == 0 else base + i * 97 % 100_000, base + i,
          "2008-01-01") for i in range(start, start + n)))
    conn.commit()


@pytest.fixture(scope="module")
def big_file(tmp_path_factory):
    """Twenty thousand rows and their statistics, built once for the module.

    Built per test it cost over a second each, and every test here only reads
    it. Each test still opens its own connection (`big`): conftest closes them
    all after every test.
    """
    path = tmp_path_factory.mktemp("big") / "big.db"
    conn = _open(path)
    _fill(conn, 20_000)
    assert db.refresh_statistics(conn)
    return path


@pytest.fixture()
def big(big_file):
    return _open(big_file)


def _plans(conn, **kwargs):
    """Every statement query_assets runs, with SQLite's plan for each."""
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        db.query_assets(conn, [ROOT], **kwargs)
    finally:
        conn.set_trace_callback(None)
    out = {}
    for sql in statements:
        if sql.startswith("SELECT"):
            kind = "count" if sql.startswith("SELECT COUNT") else "page"
            out[kind] = (sql, " | ".join(
                r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)))
    return out


def test_a_gallery_page_is_read_in_date_order_without_sorting(big):
    plans = _plans(big, limit=100, offset=5_000, **FAMILY_VIEW)
    plan = plans["page"][1]
    assert "idx_assets_gallery" in plan, plan
    assert "TEMP B-TREE" not in plan, f"the page sorts the whole library: {plan}"


def test_oldest_first_uses_the_same_index_backwards(big):
    plan = _plans(big, limit=100, sort="date_asc", **FAMILY_VIEW)["page"][1]
    assert "TEMP B-TREE" not in plan, plan


def test_the_total_is_counted_from_an_index_alone(big):
    sql, plan = _plans(big, limit=100, **FAMILY_VIEW)["count"]
    assert "user_assets" not in sql, "the count joins a table that adds no rows"
    assert "COVERING INDEX" in plan, f"the count reads every row: {plan}"


def test_a_favourites_count_still_joins_the_viewer(big):
    sql, _ = _plans(big, limit=100, favorites=True, **FAMILY_VIEW)["count"]
    assert "user_assets" in sql


def test_counts_and_pages_agree_with_the_rows(big):
    rows, total = db.query_assets(big, [ROOT], limit=100_000, **FAMILY_VIEW)
    assert total == len(rows)
    assert all(r["kind"] != "audio" and r["visibility"] <= 1 for r in rows)
    keys = [(r["captured_at"] or r["mtime"], r["id"]) for r in rows]
    assert keys == sorted(keys, reverse=True)


def test_the_retired_index_is_dropped_on_upgrade(tmp_path):
    path = tmp_path / "old.db"
    conn = db.init_db(path)
    conn.execute("CREATE INDEX idx_assets_sort ON assets(root, trashed, visibility, captured_at DESC)")
    conn.commit()
    db.close_all()
    conn = db.init_db(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_assets_sort" not in names
    assert {"idx_assets_filter", "idx_assets_gallery"} <= names


def test_statistics_are_rebuilt_only_when_the_library_changes_size(tmp_path):
    conn = _open(tmp_path / "s.db")
    assert not db.refresh_statistics(conn), "a fresh library was analysed at start-up"
    _fill(conn, 500)
    assert not db.refresh_statistics(conn), "a few hundred photos are not worth an ANALYZE"
    _fill(conn, 3_000, start=500)
    assert db.refresh_statistics(conn), "growing from nothing to thousands must re-analyse"
    assert not db.refresh_statistics(conn)
    assert db.refresh_statistics(conn, force=True)


def test_lean_rows_carry_only_what_was_asked_for(big):
    rows, total = db.query_assets(big, [ROOT], limit=10, columns=("id", "kind"),
                                  **FAMILY_VIEW)
    assert total > 10 and len(rows) == 10
    assert set(rows[0]) == {"id", "kind", "favorite", "rating"}
    with pytest.raises(ValueError):
        db.query_assets(big, [ROOT], columns=("id; DROP TABLE assets",))


def test_top_k_matches_a_full_sort():
    scores = np.random.default_rng(3).random(50_000).astype("float32")
    full = np.argsort(-scores)[:500]
    assert np.array_equal(scores[ai._top(scores, 500)], scores[full])
    assert ai._top(scores[:3], 10).tolist() == np.argsort(-scores[:3]).tolist()
    assert len(ai._top(scores, 0)) == 0


def test_indexing_hands_files_to_the_pool_a_few_at_a_time(cfg, monkeypatch):
    """A first scan must not queue every file in the library before starting."""
    from PIL import Image
    from ninaivu.media import scanner as scanner_mod
    from ninaivu.media.scanner import Scanner

    root = Path(cfg.active_root)
    for i in range(60):
        Image.new("RGB", (32, 24), (i * 4, 80, 40)).save(root / f"bulk{i}.jpg")
    cfg.workers = 1
    outstanding = {"now": 0, "most": 0}
    lock = threading.Lock()

    class Counting(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            with lock:
                outstanding["now"] += 1
                outstanding["most"] = max(outstanding["most"], outstanding["now"])
            future = super().submit(*args, **kwargs)

            def finished(_):
                with lock:
                    outstanding["now"] -= 1
            future.add_done_callback(finished)
            return future

    monkeypatch.setattr(scanner_mod, "ThreadPoolExecutor", Counting)
    conn = _open(cfg.db_path)
    scanner = Scanner(cfg)
    scanner._run(root, full=True)

    indexed = conn.execute("SELECT COUNT(*) FROM assets WHERE filename LIKE 'bulk%'").fetchone()[0]
    assert indexed == 60, scanner.progress
    assert outstanding["most"] <= 4, f"{outstanding['most']} files were queued at once"


def test_the_search_matrix_is_one_read_only_block(tmp_path):
    conn = _open(tmp_path / "e.db")
    _fill(conn, 4)
    ids = [r[0] for r in conn.execute("SELECT id FROM assets ORDER BY id")]
    # Not hidden: an admins-only item is given no search vector at all.
    conn.execute("UPDATE assets SET visibility=1")
    conn.commit()
    for n, asset_id in enumerate(ids[:3]):
        db.store_embedding(conn, asset_id, "m", 4,
                           np.array([n, 1, 2, 3], "float32").tobytes())
    # A vector from a different model: it must not shift the rows after it.
    db.store_embedding(conn, ids[3], "other", 2, np.array([9, 9], "float32").tobytes())

    got, buffer, dim, index = db.embedding_store(conn)
    assert dim == 4 and got == ids[:3] and index == {a: i for i, a in enumerate(ids[:3])}
    matrix = np.frombuffer(buffer, dtype="float32").reshape(len(got), dim)
    assert matrix[:, 0].tolist() == [0, 1, 2]
    with pytest.raises(TypeError):
        buffer[0] = 1


def test_the_gallery_says_when_it_shows_only_part_of_a_view(as_admin):
    data = as_admin.get("/api/segments?limit=1").get_json()
    assert data["total"] > 1 and data["returned"] == 1 and data["truncated"] is True

    root = Path(__file__).resolve().parents[1] / "ninaivu"
    assert 'id="truncated-notice"' in (root / "templates/index.html").read_text(encoding="utf-8")
    assert "showTruncation(data)" in (root / "static/js/app.js").read_text(encoding="utf-8")


def _pages(client, query, size):
    """Walk /api/segments piece by piece, the way the grid does."""
    pieces, offset = [], 0
    while offset is not None:
        data = client.get(f"/api/segments?{query}&limit={size}&offset={offset}").get_json()
        pieces.append(data)
        offset = data["next_offset"]
        assert len(pieces) < 100, "paging never ended"
    return pieces


def _ids(pieces):
    return [item[0] for piece in pieces for seg in piece["segments"] for item in seg["items"]]


def test_the_gallery_arrives_in_pieces_that_add_up_to_the_whole(as_admin):
    whole = as_admin.get("/api/segments").get_json()
    assert whole["next_offset"] is None and not whole["truncated"]
    everything = _ids([whole])
    assert len(everything) > 3, "the fixture library is too small to page"

    pieces = _pages(as_admin, "sort=date_desc", 2)
    assert _ids(pieces) == everything, "pieces lost, repeated or reordered items"
    assert [p["offset"] for p in pieces] == list(range(0, 2 * len(pieces), 2))
    assert pieces[-1]["next_offset"] is None and not pieces[-1]["truncated"]
    assert all(p["truncated"] for p in pieces[:-1])


def test_a_random_order_is_never_paged(as_admin):
    """Every query redraws a random order, so a second piece would repeat and
    skip photos; it comes back whole, at the size it always had."""
    data = as_admin.get("/api/segments?sort=random&limit=2&offset=2").get_json()
    assert data["offset"] == 0 and data["next_offset"] is None
    assert data["returned"] == data["total"]


def test_an_index_whose_definition_changed_is_rebuilt(tmp_path):
    """IF NOT EXISTS looks only at the name, so an index with new columns must
    be recognised as stale and rebuilt rather than kept in its old shape."""
    path = tmp_path / "shape.db"
    conn = db.init_db(path)
    conn.execute("DROP INDEX idx_assets_gallery")
    conn.execute("CREATE INDEX idx_assets_gallery ON assets(root, COALESCE(captured_at, mtime) DESC, "
                 "id DESC, trashed, nsfw, visibility, kind)")
    conn.commit()
    db.close_all()

    conn = db.init_db(path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_assets_gallery'").fetchone()[0]
    assert "date_key" in sql, f"the old index shape survived the upgrade: {sql}"
    assert db._drop_changed_indexes(conn, db._INDEX_SCHEMA) == [], "a current index was dropped"


@pytest.mark.parametrize("side", ["before", "after"])
def test_the_family_date_policy_stays_on_the_index(big, monkeypatch, side):
    """The date policy is part of every viewer's visibility clause on the family
    app. Pages and counts must still be answered from the index, not by reading
    a row for every photo on the far side of the cutoff."""
    from ninaivu.server import date_policy
    monkeypatch.setattr(date_policy, "current", lambda: ("2008-01-01", side, "family"))
    plans = _plans(big, limit=100, offset=5_000, **FAMILY_VIEW)
    page_sql, page_plan = plans["page"]
    assert "date_key" in page_sql, "the policy did not reach the query"
    # Either the date-ordered walk, or — on SQLite with stat4, which knows every
    # photograph here is dated 2008-01-01 — a search bounded by the cutoff that
    # finds the visible side empty and so sorts nothing. Never a sort of the
    # whole library.
    walked = "idx_assets_gallery" in page_plan and "TEMP B-TREE" not in page_plan
    bounded = "idx_assets_date (root=? AND date_key<?)" in page_plan
    assert walked or bounded, page_plan
    assert "COVERING INDEX" in plans["count"][1], plans["count"][1]


def test_a_folder_view_reads_only_that_folder(big):
    """One folder of a large library is found through the folder index, not by
    walking the whole library and testing each row against a LIKE pattern."""
    plans = _plans(big, limit=100, folder="f10", **FAMILY_VIEW)
    for kind in ("page", "count"):
        sql, plan = plans[kind]
        assert "LIKE" not in sql
        assert "idx_assets_folder" in plan, f"{kind}: {plan}"
    rows, total = db.query_assets(big, [ROOT], limit=1000, folder="f10", **FAMILY_VIEW)
    assert total == len(rows) > 0 and {r["folder"] for r in rows} == {"f10"}


@pytest.mark.parametrize("sort", ["name_asc", "name_desc", "size_desc", "size_asc"])
def test_name_and_size_sorts_read_in_order(big, sort):
    plan = _plans(big, limit=100, offset=5_000, sort=sort, **FAMILY_VIEW)["page"][1]
    assert "TEMP B-TREE" not in plan, f"{sort} sorts the whole library: {plan}"


def test_a_scan_keeps_only_what_indexing_reads_about_each_file(tmp_path):
    """A first scan holds one entry per new file until indexing reaches it."""
    import os
    import sys
    from ninaivu.media import media
    from ninaivu.media.scanner import FileSignature

    photo = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(photo)
    st = os.stat(photo)
    signature = FileSignature(st)
    assert (signature.st_size, signature.st_mtime) == (st.st_size, st.st_mtime)
    assert media.created_at(signature) == media.created_at(st), "the capture-date fallback changed"
    assert not hasattr(signature, "__dict__")
    assert sys.getsizeof(signature) < sys.getsizeof(st)


def test_a_gallery_piece_of_25000_is_read_in_date_order_too(tmp_path):
    """The app fetches the layout 25,000 tiles at a time. With the sampled
    statistics, SQLite took a library folder of 300,000 rows for one of a
    thousand and sorted the whole library for each piece (1.3 s instead of
    0.2 s); the photographs' table is analysed in full."""
    conn = _open(tmp_path / "pieces.db")
    _fill(conn, 60_000)
    assert db.refresh_statistics(conn)
    stat = conn.execute("SELECT stat FROM sqlite_stat1 "
                        "WHERE idx='idx_assets_gallery'").fetchone()[0].split()
    # Rows per library folder: all of them, as there is one folder here.
    assert int(stat[1]) == 60_000, stat
    plans = _plans(conn, limit=25_000, offset=25_000, with_total=False, **FAMILY_VIEW)
    sql, plan = plans["page"]
    assert "idx_assets_gallery" in plan and "TEMP B-TREE" not in plan, plan
