"""Where each photograph is (4.0, proposal 3): the ``locations`` table that
migration 2 adds beside ``assets``, the triggers that keep it in step, and
fingerprints taken as files are indexed rather than in a pass afterwards."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from PIL import Image

from ninaivu.media import scanner as scanner_mod
from ninaivu.media.scanner import Scanner
from ninaivu.server import auth
from ninaivu.storage import db, fingerprints, locations, migrations


# -- helpers -----------------------------------------------------------------

def _record(rel: str, size: int = 100, mtime: float = 1_700_000_000.0) -> dict:
    folder, _, name = rel.rpartition("/")
    return {"root": "/library", "rel_path": rel, "filename": name, "folder": folder,
            "ext": "jpg", "kind": "picture", "size": size, "mtime": mtime}


def _expected(conn) -> dict[int, tuple]:
    """What ``locations`` should say, worked out from the tables 3.7 has."""
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT a.id, a.root, a.rel_path, "
        "CASE WHEN f.asset_id IS NULL THEN NULL ELSE f.algo || ':' || f.digest END "
        "FROM assets a LEFT JOIN content_fingerprints f "
        "ON f.asset_id=a.id AND f.size=a.size AND f.mtime=a.mtime")}


def _locations(conn) -> dict[int, tuple]:
    rows = conn.execute("SELECT asset_id, root, rel_path, fingerprint FROM locations "
                        "WHERE place='library'").fetchall()
    found = {r[0]: (r[1], r[2], r[3]) for r in rows}
    assert len(found) == len(rows), "one place in the library per photograph"
    return found


def _in_step(conn) -> dict[int, tuple]:
    have = _locations(conn)
    assert have == _expected(conn)
    assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == len(have)
    return have


def _fingerprint(conn, asset_id: int, digest: str, size: int = 100,
                 mtime: float = 1_700_000_000.0) -> None:
    fingerprints.store(conn, [(asset_id, digest, size, mtime)])


def _schema(conn) -> dict[tuple[str, str], tuple]:
    return {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master")}


def _as_37(conn) -> None:
    """Make an index made by this version into one as 3.7 leaves it."""
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                "AND name LIKE 'locations_%'").fetchall():
        conn.execute(f"DROP TRIGGER {name}")
    conn.execute("DROP TABLE locations")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()


def _reopen(path) -> object:
    db.close(path)
    db._READY.discard(str(path))                          # noqa: SLF001
    return db.init_db(path)


def _open(cfg):
    """The index of the fixture library, as the server opens it."""
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    return conn


def _rescan(cfg) -> Scanner:
    scanner = Scanner(cfg)
    scanner._run(Path(cfg.active_root), full=False)       # noqa: SLF001
    assert scanner.progress.status == "done", scanner.progress.message
    return scanner


# -- the migration -----------------------------------------------------------

def test_a_37_index_migrates_with_locations_filled_and_ids_unchanged(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    ids = [db.upsert_asset(conn, _record(f"2024/0{n}/photo{n}.jpg")) for n in range(1, 5)]
    _fingerprint(conn, ids[0], "aa" * 32)
    _fingerprint(conn, ids[1], "bb" * 32)
    with db._write_lock:                                  # noqa: SLF001
        # Edited since its fingerprint was taken: that one describes nothing now.
        conn.execute("UPDATE assets SET size=101 WHERE id=?", (ids[1],))
        conn.commit()
    _as_37(conn)
    before = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, root, rel_path FROM assets")}

    conn = _reopen(path)
    try:
        assert migrations.recorded(conn) == migrations.latest(migrations.INDEX)
        after = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, root, rel_path FROM assets")}
        assert after == before
        have = _in_step(conn)
        assert set(have) == set(ids)
        assert have[ids[0]][2] == f"{fingerprints.ALGO}:{'aa' * 32}"
        assert have[ids[1]][2] is None and have[ids[2]][2] is None
        assert {r[0] for r in conn.execute("SELECT place FROM locations")} == {"library"}
    finally:
        db.close(path)


def test_the_migration_only_adds(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    db.upsert_asset(conn, _record("a/one.jpg"))
    _as_37(conn)
    before = _schema(conn)
    assert not [key for key in before if "locations" in key[1]]

    conn = _reopen(path)
    try:
        after = _schema(conn)
        assert {key: after.get(key) for key in before} == before
        added = {key: value for key, value in after.items() if key not in before}
        assert added and all(value[0] == "locations" or key[1].startswith("locations_")
                             for key, value in added.items())
        # The triggers watch the tables 3.7 has, and write only the new one.
        assert {value[0] for key, value in added.items() if key[0] == "trigger"} == {
            "assets", "content_fingerprints"}
    finally:
        db.close(path)


def test_the_triggers_outlive_every_start(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master "
                                        "WHERE name LIKE 'locations_%'")}
    assert len(names) == 6
    for _ in range(2):
        conn = _reopen(path)
    try:
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master "
                                           "WHERE name LIKE 'locations_%'")} == names
        db.upsert_asset(conn, _record("after/restart.jpg"))
        assert len(_in_step(conn)) == 1
    finally:
        db.close(path)


# -- the triggers ------------------------------------------------------------

def test_locations_follow_insert_rename_respell_and_delete(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    try:
        one = db.upsert_asset(conn, _record("2024/01/one.jpg"))
        two = db.upsert_asset(conn, _record("2024/01/two.jpg"))
        assert _in_step(conn)[one] == ("/library", "2024/01/one.jpg", None)
        seen = conn.execute("SELECT seen_at FROM locations WHERE asset_id=?",
                            (one,)).fetchone()[0]
        assert seen > 0

        # A fingerprint arriving, and a rescan writing the same values again.
        _fingerprint(conn, one, "11" * 32)
        db.bulk_upsert(conn, [_record("2024/01/one.jpg")])
        assert _in_step(conn)[one][2] == f"{fingerprints.ALGO}:{'11' * 32}"

        # Renamed in place.
        with db._write_lock:                              # noqa: SLF001
            conn.execute("UPDATE assets SET rel_path='2024/02/one.jpg' WHERE id=?", (one,))
            conn.commit()
        have = _in_step(conn)
        assert have[one] == ("/library", "2024/02/one.jpg", f"{fingerprints.ALGO}:{'11' * 32}")

        # Respelled onto a path a first scan had already indexed again: the
        # duplicate's row and place go, the older row takes the path.
        dup = db.upsert_asset(conn, _record("2024/03/Café.jpg"))
        db.upsert_asset(conn, _record("2024/03/Café.jpg"))
        db.respell_asset(conn, "/library", "2024/02/one.jpg", "2024/03/Café.jpg")
        have = _in_step(conn)
        assert dup not in have
        assert have[one][1] == "2024/03/Café.jpg" and have[one][2] is not None

        # The file changed: its fingerprint no longer describes it.
        db.bulk_upsert(conn, [_record("2024/03/Café.jpg", size=200)])
        assert _in_step(conn)[one][2] is None
        _fingerprint(conn, one, "22" * 32, size=200)
        assert _in_step(conn)[one][2] == f"{fingerprints.ALGO}:{'22' * 32}"
        with db._write_lock:                              # noqa: SLF001
            conn.execute("DELETE FROM content_fingerprints WHERE asset_id=?", (one,))
            conn.commit()
        assert _in_step(conn)[one][2] is None

        # Deleted, by hand and by a scan that found it gone.
        with db._write_lock:                              # noqa: SLF001
            conn.execute("DELETE FROM assets WHERE id=?", (two,))
            conn.commit()
        assert two not in _in_step(conn)
        db.delete_missing(conn, "/library", set(), complete=True, force=True)
        assert _in_step(conn) == {}
    finally:
        db.close(path)


def test_a_writer_that_goes_round_the_triggers_is_set_right_by_the_next_write(tmp_path):
    """A place left behind (a tool with the triggers dropped, a repair) never
    stops a photograph being indexed at that path."""
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    # The foreign key would refuse a place for a photograph that is not
    # there; a tool's connection without it would not.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO locations(asset_id, root, rel_path) "
                 "VALUES (999, '/library', 'stale.jpg')")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        new = db.upsert_asset(conn, _record("stale.jpg"))
        assert _in_step(conn) == {new: ("/library", "stale.jpg", None)}
    finally:
        db.close(path)


# -- the scanner -------------------------------------------------------------

def test_fingerprints_are_taken_as_files_are_indexed(cfg, monkeypatch):
    cfg.content_fingerprints = True
    conn = _open(cfg)
    # The last pass does nothing here: whatever has a fingerprint got it
    # while it was being indexed.
    monkeypatch.setattr(Scanner, "_fingerprint", lambda self, conn, root: None)
    _rescan(cfg)
    files = conn.execute("SELECT COUNT(*) FROM assets WHERE trashed=0").fetchone()[0]
    assert files > 0
    assert scanner_mod.owed(conn, cfg.active_root, "fingerprints").startswith("0:")
    have = _in_step(conn)
    assert all(fp and fp.startswith(f"{fingerprints.ALGO}:") for _, _, fp in have.values())
    # The two byte-identical copies in the fixture library are one fingerprint.
    shots = {rel: fp for _, rel, fp in have.values() if rel.startswith("2023/05/12/shot0")}
    assert len(shots) == 2 and len(set(shots.values())) == 1
    digest = fingerprints.fingerprint_file(Path(cfg.active_root) / "2023/05/12/shot0.jpg")[0]
    assert set(shots.values()) == {f"{fingerprints.ALGO}:{digest}"}


def test_a_file_too_big_to_take_at_index_time_is_left_to_the_last_pass(cfg, monkeypatch):
    cfg.content_fingerprints = True
    conn = _open(cfg)
    monkeypatch.setattr(fingerprints, "AT_INDEX_UP_TO", 1000)
    monkeypatch.setattr(Scanner, "_fingerprint", lambda self, conn, root: None)
    _rescan(cfg)
    sizes = dict(conn.execute("SELECT rel_path, size FROM assets"))
    have = {rel: fp for _, rel, fp in _in_step(conn).values()}
    assert any(size > 1000 for size in sizes.values())
    assert all((fp is None) == (sizes[rel] > 1000) for rel, fp in have.items())

    monkeypatch.undo()
    fingerprints.fill(conn, cfg.active_root)
    assert all(fp is not None for _, _, fp in _in_step(conn).values())


def test_with_fingerprints_off_none_are_taken_at_index_time(cfg):
    conn = _open(cfg)
    assert not cfg.content_fingerprints
    _rescan(cfg)
    assert conn.execute("SELECT COUNT(*) FROM content_fingerprints").fetchone()[0] == 0
    assert all(fp is None for _, _, fp in _in_step(conn).values())


def test_a_3x_index_is_backfilled_by_the_fingerprint_pass(cfg):
    """An index made before 4.0: migrated with its places and no fingerprints;
    the last pass of the next scan takes them, and the triggers carry them."""
    conn = _open(cfg)
    _rescan(cfg)                                          # fingerprints off, as many 3.x were
    _as_37(conn)
    ids = {r[0]: r[1] for r in conn.execute("SELECT id, rel_path FROM assets")}
    conn = _reopen(cfg.db_path)
    assert migrations.recorded(conn) == migrations.latest(migrations.INDEX)
    assert all(fp is None for _, _, fp in _in_step(conn).values())

    cfg.content_fingerprints = True
    read: list[str] = []
    real = fingerprints.at_index
    fingerprints.at_index = lambda path, st: read.append(str(path)) or real(path, st)
    try:
        _rescan(cfg)
    finally:
        fingerprints.at_index = real
    assert read == []                                     # nothing new to index
    assert {r[0]: r[1] for r in conn.execute("SELECT id, rel_path FROM assets")} == ids
    have = _in_step(conn)
    assert set(have) == set(ids)
    assert all(fp and fp.startswith(f"{fingerprints.ALGO}:") for _, _, fp in have.values())
    assert scanner_mod.owed(conn, cfg.active_root, "fingerprints").startswith("0:")


def test_a_moved_photograph_keeps_its_id_and_its_place_follows(cfg):
    cfg.content_fingerprints = True
    conn = _open(cfg)
    _rescan(cfg)
    old = conn.execute("SELECT id FROM assets WHERE rel_path='shared/holiday/beach1.jpg'"
                       ).fetchone()[0]
    fp = _in_step(conn)[old][2]
    assert fp is not None

    root = Path(cfg.active_root)
    (root / "trips/2024").mkdir(parents=True)
    shutil.move(root / "shared/holiday/beach1.jpg", root / "trips/2024/renamed.jpg")
    _rescan(cfg)

    have = _in_step(conn)
    assert have[old] == (str(root), "trips/2024/renamed.jpg", fp)
    assert conn.execute("SELECT COUNT(*) FROM locations WHERE rel_path="
                        "'shared/holiday/beach1.jpg'").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT asset_id FROM locations WHERE fingerprint=?",
                                       (fp,))] == [old]


def test_locations_answer_what_the_move_finder_asks(cfg):
    """``fingerprints.fingerprinted`` (what _follow_moves pairs by) and
    ``locations`` agree on every file, before and after one is edited."""
    cfg.content_fingerprints = True
    conn = _open(cfg)
    _rescan(cfg)
    root = Path(cfg.active_root)
    path = root / "misc/plain.png"
    Image.new("RGB", (210, 200), (200, 10, 10)).save(path)
    later = time.time() + 5
    os.utime(path, (later, later))
    db.bulk_upsert(conn, [scanner_mod.build_record(root, "misc/plain.png", path.stat(), cfg)])

    rels = [r[0] for r in conn.execute("SELECT rel_path FROM assets")]
    asked = fingerprints.fingerprinted(conn, str(root), rels)
    assert "misc/plain.png" not in asked
    from_locations = {r[0]: (r[1].partition(":")[2], r[2]) for r in conn.execute(
        "SELECT l.rel_path, l.fingerprint, a.size FROM locations l "
        "JOIN assets a ON a.id=l.asset_id WHERE l.root=? AND l.fingerprint LIKE ?",
        (str(root), f"{fingerprints.ALGO}:%"))}
    assert from_locations == asked


# -- rebuilding and checking (second slice) ----------------------------------

def _many(conn, n: int) -> list[int]:
    db.bulk_upsert(conn, [_record(f"2024/{i // 100:02d}/p{i:05d}.jpg") for i in range(n)])
    ids = [r[0] for r in conn.execute("SELECT id FROM assets ORDER BY id")]
    fingerprints.store(conn, [(i, f"{i:064x}", 100, 1_700_000_000.0) for i in ids[::2]])
    return ids


def test_check_finds_each_difference_and_rebuild_sets_them_right(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    try:
        ids = _many(conn, 40)
        assert locations.check(conn)["agrees"]
        first_seen = dict(conn.execute("SELECT asset_id, seen_at FROM locations"))
        with db._write_lock:                              # noqa: SLF001
            conn.execute("PRAGMA foreign_keys=OFF")
            # What a repair can leave: rows of only one of the tables.
            conn.execute("DELETE FROM locations WHERE asset_id IN (?, ?)", (ids[1], ids[2]))
            conn.execute("UPDATE locations SET fingerprint='blake3:00' WHERE asset_id=?", (ids[3],))
            conn.execute("UPDATE locations SET fingerprint=NULL WHERE asset_id=?", (ids[4],))
            conn.execute("UPDATE locations SET rel_path='elsewhere.jpg' WHERE asset_id=?", (ids[5],))
            conn.execute("INSERT INTO locations(asset_id, root, rel_path) VALUES (99999, '/x', 'y')")
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
        before = conn.execute("SELECT COUNT(*), SUM(seen_at) FROM locations").fetchone()
        found = locations.check(conn)
        assert not found["agrees"]
        assert (found["missing"], found["extra"], found["wrong_fingerprint"]) == (3, 2, 2)
        assert set(found["examples"]["missing"]) == {ids[1], ids[2], ids[5]}
        assert found["examples"]["wrong_fingerprint"] == [ids[3], ids[4]]
        # Only read.
        assert conn.execute("SELECT COUNT(*), SUM(seen_at) FROM locations").fetchone() == before

        assert locations.rebuild(conn) == len(ids)
        assert locations.check(conn)["agrees"]
        _in_step(conn)
        # A place that was right keeps when it was first seen there.
        now_seen = dict(conn.execute("SELECT asset_id, seen_at FROM locations"))
        assert now_seen[ids[0]] == first_seen[ids[0]]
    finally:
        db.close(path)


def test_rebuild_makes_lost_triggers_again_and_waits_for_the_migration(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    try:
        db.upsert_asset(conn, _record("a/one.jpg"))
        conn.execute("DROP TRIGGER locations_follow_insert")
        conn.commit()
        locations.rebuild(conn)
        db.upsert_asset(conn, _record("a/two.jpg"))
        assert len(_in_step(conn)) == 2
        _as_37(conn)
        assert locations.rebuild(conn) == 0
        assert locations.check(conn) == {"present": False, "agrees": False, "missing": 0,
                                         "extra": 0, "wrong_fingerprint": 0, "examples": {}}
    finally:
        db.close(path)


def _dbstat() -> bool:
    import sqlite3
    try:
        sqlite3.connect(":memory:").execute("SELECT * FROM dbstat LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def test_a_partly_salvaged_index_has_its_locations_right_afterwards(tmp_path):
    import pytest
    if not _dbstat():
        pytest.skip("SQLite without dbstat")
    import sqlite3
    from contextlib import closing
    from ninaivu.storage import index_check
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    _many(conn, 3000)
    db.close(path)
    db._READY.discard(str(path))                          # noqa: SLF001
    # Pages of assets overwritten, as a failing disk leaves them: the rebuild
    # salvages what it can of each table on its own, and locations keeps the
    # places of photographs whose rows were lost.
    with closing(sqlite3.connect(path)) as raw:
        leaves = [r[0] for r in raw.execute(
            "SELECT pageno FROM dbstat WHERE name='assets' AND pagetype='leaf'")]
        size = raw.execute("PRAGMA page_size").fetchone()[0]
    with path.open("r+b") as stream:
        for page in leaves[len(leaves) // 3: len(leaves) // 3 + 3]:
            stream.seek((page - 1) * size + 100)
            stream.write(os.urandom(size // 4))
    index_check.ask(tmp_path, "error")
    result = index_check.run(tmp_path)
    assert result["ok"] and result["repaired"] == ["index.db"], result
    assert result["locations"]["rebuilt"] > 0
    with closing(sqlite3.connect(path)) as raw:
        assert 0 < raw.execute("SELECT COUNT(*) FROM assets").fetchone()[0] < 3000
        assert locations.check(raw)["agrees"]
        assert _locations(raw) == _expected(raw)


def test_a_check_after_damage_was_seen_rebuilds_only_what_differs(tmp_path):
    import sqlite3
    from contextlib import closing
    from ninaivu.storage import index_check
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    _many(conn, 10)
    db.close(path)
    db._READY.discard(str(path))                          # noqa: SLF001

    index_check.ask(tmp_path, "error")
    assert index_check.run(tmp_path)["locations"] == {"agrees": True}

    with closing(sqlite3.connect(path)) as raw:
        raw.execute("DELETE FROM locations WHERE asset_id = (SELECT MIN(id) FROM assets)")
        raw.commit()
    # A move changed no row: not looked at.
    index_check.ask(tmp_path, "moved")
    assert "locations" not in index_check.run(tmp_path)
    index_check.ask(tmp_path, "error")
    done = index_check.run(tmp_path)["locations"]
    assert done["rebuilt"] == 10 and done["found"] == {"missing": 1, "extra": 0,
                                                        "wrong_fingerprint": 0}
    with closing(sqlite3.connect(path)) as raw:
        assert locations.check(raw)["agrees"]


# -- readers of locations ----------------------------------------------------

from test_copy_contract import recorded  # noqa: E402,F401  (fixture)


def test_the_ledger_maps_paths_to_fingerprints_through_locations_as_it_did_before(
        recorded, monkeypatch):                           # noqa: F811
    """The ledger's join on ``locations`` gives what its join on
    ``content_fingerprints`` at the file's size and time gave: the same rows
    filled from every record, and the same headline, with a file edited since
    its fingerprint and one never fingerprinted among them."""
    from ninaivu.copies import ledger
    conn, roots = recorded.conn, [str(r) for r in recorded.cfg.roots]
    with db._write_lock:                                  # noqa: SLF001
        # Taken of an earlier version of a file the copies still record as it is.
        conn.execute("UPDATE content_fingerprints SET mtime = mtime - 7 WHERE asset_id=?",
                     (recorded.b["id"],))
        conn.execute("DELETE FROM content_fingerprints WHERE asset_id=?", (recorded.c["id"],))
        conn.commit()
    _in_step(conn)
    sources = ["mirror", "offsite", "drive"]

    def filled() -> tuple:
        with db._write_lock:                              # noqa: SLF001
            conn.execute("DELETE FROM copies")
            kept = ledger.fill(conn, only=sources)
            rows = sorted(conn.execute("SELECT fingerprint, destination, state, size FROM copies"))
            conn.rollback()
        return kept, rows, ledger.headline(conn, roots)

    now = filled()
    monkeypatch.setattr(ledger, "_FINGERPRINTED", "JOIN content_fingerprints f ON "
                        "f.asset_id = a.id AND f.size = a.size AND f.mtime = a.mtime")
    monkeypatch.setattr(ledger, "_FP", "(f.algo || ':' || f.digest)")
    before = filled()
    assert now[:2] == before[:2]
    assert now[1] and now[2]["unfingerprinted"] >= 1
    assert recorded.b["fp"] not in {r[0] for r in now[1]}


def _let_go(conn, rel: str, digest: str) -> None:
    conn.execute("INSERT INTO mugil_let_go(root, rel_path, size, mtime, algo, digest, let_go_at) "
                 "VALUES ('/library', ?, 100, 1700000000.0, ?, ?, 1.0)",
                 (rel, fingerprints.ALGO, digest))
    conn.commit()


def test_a_let_go_original_is_known_by_its_path_and_after_a_move_by_its_fingerprint(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    try:
        kept, moved, copy, plain = (db.upsert_asset(conn, _record(f"a/{n}.jpg"))
                                    for n in ("kept", "moved", "copy", "plain"))
        _fingerprint(conn, kept, "11" * 32)
        _fingerprint(conn, copy, "11" * 32)               # the same bytes as kept
        _fingerprint(conn, moved, "22" * 32)
        _let_go(conn, "a/kept.jpg", "11" * 32)
        _let_go(conn, "a/moved.jpg", "22" * 32)
        # As 3.x read it: by path.
        assert locations.let_go_paths(conn, "/library") == {"a/kept.jpg", "a/moved.jpg"}
        assert locations.let_go_paths(conn, "/elsewhere") == set()

        # The row moved by a writer that left the let-go record where it was.
        with db._write_lock:                              # noqa: SLF001
            conn.execute("UPDATE assets SET rel_path='b/moved.jpg' WHERE id=?", (moved,))
            conn.commit()
        assert locations.let_go_paths(conn, "/library") == {
            "a/kept.jpg", "a/moved.jpg", "b/moved.jpg"}

        # None of the four files is on the disk: the let-go ones stay, and the
        # copy with the let-go bytes is not taken for the original, whose row
        # is where its record says.
        db.delete_missing(conn, "/library", set(), complete=True, force=True)
        assert {r[0] for r in conn.execute("SELECT id FROM assets")} == {kept, moved}
        assert len(_in_step(conn)) == 2
    finally:
        db.close(path)


def test_a_let_go_record_two_rows_share_bytes_with_names_neither(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    try:
        one, two = (db.upsert_asset(conn, _record(f"b/{n}.jpg")) for n in ("one", "two"))
        _fingerprint(conn, one, "33" * 32)
        _fingerprint(conn, two, "33" * 32)
        _let_go(conn, "a/was.jpg", "33" * 32)             # no row is at its path
        assert locations.let_go_paths(conn, "/library") == {"a/was.jpg"}
    finally:
        db.close(path)


# -- the console's reading -----------------------------------------------------

def test_the_console_reads_the_health_of_locations(scanned):
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        anonymous = create_admin_app(services).test_client()
        assert anonymous.get("/api/admin/locations/health").status_code in (401, 403)
        client = login(create_admin_app(services).test_client(), *ADMIN)
        files = conn.execute("SELECT COUNT(*) FROM assets WHERE trashed=0").fetchone()[0]
        got = client.get("/api/admin/locations/health").get_json()
        assert got["photographs"] == files > 0
        assert got["fingerprinted"] == 0 and got["waiting"] == files
        assert got["locations"]["agrees"] is True

        first = conn.execute("SELECT id FROM assets WHERE trashed=0 ORDER BY id").fetchone()[0]
        _fingerprint(conn, first, "44" * 32, *conn.execute(
            "SELECT size, mtime FROM assets WHERE id=?", (first,)).fetchone())
        with db._write_lock:                              # noqa: SLF001
            conn.execute("DELETE FROM locations WHERE asset_id=?", (first,))
            conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        got = client.get("/api/admin/locations/health").get_json()
        assert (got["fingerprinted"], got["waiting"]) == (1, files - 1)
        assert got["locations"]["agrees"] is False and got["locations"]["missing"] == 1
        # Only read.
        assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == before
    finally:
        services.stop(timeout=5.0)
