"""The stability pass of October 2026: hand-offs between jobs, hidden items
during a long pass, connections left in a transaction, and the backup's queue.

Each test is one of the races found by running scans, imports, the second
copy, the backup and a dozen browsing family members at once on the Peak
profile.
"""

import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from ninaivu.media.scanner import Scanner
from ninaivu.server import auth
from ninaivu.storage import db, embeddings


def open_db(cfg):
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    return conn


def picture_ids(conn):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' ORDER BY id")]


# -- handing over from one scan to the next ---------------------------------------

class _Blocking:
    """A scanner whose scan sits in one phase until it is let go."""

    def __init__(self, cfg, status):
        self.scanner = Scanner(cfg)
        self.release = threading.Event()
        self.runs = []

        def run_each(roots, full):
            self.runs.append((list(roots), full))
            self.scanner.progress.update(status=status, running=True)
            while not self.release.is_set() and not self.scanner._stop.is_set():
                time.sleep(0.01)
            self.scanner.progress.update(status="done", running=False)

        self.scanner._run_each = run_each
        self.scanner.watch = lambda roots=(): None


def _wait_for(predicate, timeout=5.0):
    ends = time.monotonic() + timeout
    while time.monotonic() < ends:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_asking_for_a_scan_during_indexing_answers_at_once_and_follows_it(cfg):
    fake = _Blocking(cfg, "indexing")
    fake.scanner.start(full=True)
    assert _wait_for(lambda: fake.runs)

    began = time.monotonic()
    fake.scanner.start([Path(cfg.active_root)])
    assert time.monotonic() - began < 1.0          # it used to wait ten seconds
    assert len(fake.runs) == 1                     # the full scan was not thrown away
    assert not fake.scanner._stop.is_set()

    fake.release.set()
    assert _wait_for(lambda: len(fake.runs) == 2)
    roots, full = fake.runs[1]
    assert [str(r) for r in roots] == [str(Path(cfg.active_root))]
    fake.scanner.stop(join=True)


def test_asking_for_a_scan_during_analysis_takes_over_without_waiting(cfg):
    fake = _Blocking(cfg, "tagging")
    fake.scanner.start()
    assert _wait_for(lambda: fake.runs)

    began = time.monotonic()
    fake.scanner.start([Path(cfg.active_root)], full=False)
    assert time.monotonic() - began < 1.0
    # The analysis stood down, and the new scan started as soon as it had.
    assert _wait_for(lambda: len(fake.runs) == 2)
    fake.release.set()
    fake.scanner.stop(join=True)


def test_requests_during_a_scan_are_merged_into_one_that_follows(cfg, tmp_path):
    fake = _Blocking(cfg, "walking")
    fake.scanner.start()
    assert _wait_for(lambda: fake.runs)
    other = tmp_path / "other"
    other.mkdir()
    fake.scanner.start([Path(cfg.active_root)])
    fake.scanner.start([other], full=True)
    fake.release.set()
    assert _wait_for(lambda: len(fake.runs) == 2)
    time.sleep(0.1)
    assert len(fake.runs) == 2
    roots, full = fake.runs[1]
    assert {str(r) for r in roots} == {str(Path(cfg.active_root)), str(other)}
    assert full is True
    fake.scanner.stop(join=True)


def test_files_seen_while_analysis_waits_for_the_night_are_indexed_now(cfg):
    fake = _Blocking(cfg, "tagging")                    # held for its turn
    fake.scanner.start()
    assert _wait_for(lambda: fake.runs)
    fake.scanner._rescan_quiet(Path(cfg.active_root))
    # It used to ask again every thirty seconds until the analysis ended.
    assert _wait_for(lambda: len(fake.runs) == 2)
    fake.release.set()
    fake.scanner.stop(join=True)


def test_files_seen_during_indexing_are_queued_behind_it(cfg):
    fake = _Blocking(cfg, "indexing")
    fake.scanner.start()
    assert _wait_for(lambda: fake.runs)
    fake.scanner._rescan_quiet(Path(cfg.active_root))
    assert not fake.scanner._stop.is_set()
    fake.release.set()
    assert _wait_for(lambda: len(fake.runs) == 2)
    fake.scanner.stop(join=True)


def test_a_scan_that_cannot_open_the_index_does_not_stay_running(cfg, monkeypatch):
    scanner = Scanner(cfg)

    def busy(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "ready_connection", busy)
    scanner._run(Path(cfg.active_root), full=False)
    assert scanner.progress.snapshot()["status"] == "error"


# -- an item hidden while a pass is working on it ----------------------------------

def test_a_pass_does_not_write_onto_an_item_hidden_since_it_started(scanned):
    cfg, conn, _ = scanned
    asset = picture_ids(conn)[0]
    db.set_visibility(conn, [asset], 2, source="item", user_id=None, record_undo=False)

    # What the tagger, the text reader and the face finder would write next.
    db.store_ai_fields(conn, asset, tags=["passport"], caption="passport",
                       ai_version=99, ocr_text="K1234567")
    db.store_embedding(conn, asset, "fake", 2, np.array([1, 0], np.float32).tobytes())
    stored = db.replace_asset_faces(conn, asset, [{
        "bbox": [0, 0, 1, 1], "embedding": b"x"}], "fake")

    row = conn.execute("SELECT tags, caption, ocr_text, ai_version FROM assets WHERE id=?",
                       (asset,)).fetchone()
    assert row["ocr_text"] is None and row["ai_version"] != 99
    assert "passport" not in (row["tags"] or "")
    assert conn.execute("SELECT 1 FROM embeddings WHERE asset_id=?", (asset,)).fetchone() is None
    assert stored == 0
    assert conn.execute("SELECT 1 FROM faces WHERE asset_id=?", (asset,)).fetchone() is None


def test_a_folder_hidden_during_indexing_takes_the_files_indexed_after(scanned):
    cfg, conn, _ = scanned
    root = str(cfg.active_root)
    rules_before = db.folder_rules(conn, root)
    # The scan has read the rules; the admin hides "private" now; the scan
    # goes on writing files there at the default level.
    conn.execute("INSERT INTO folder_rules(root, folder, visibility, created_at) "
                 "VALUES(?,?,?,?)", (root, "private", 2, time.time()))
    conn.execute("UPDATE assets SET visibility=1, vis_source='default' "
                 "WHERE root=? AND folder='private'", (root,))
    conn.commit()

    Scanner._rules_now(conn, Path(root), rules_before)
    rows = conn.execute("SELECT visibility, vis_source FROM assets "
                        "WHERE root=? AND folder='private'", (root,)).fetchall()
    assert rows and all(r["visibility"] == 2 and r["vis_source"] == "folder" for r in rows)
    # Elsewhere nothing moved.
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE root=? AND folder!='private' "
                        "AND vis_source='folder'", (root,)).fetchone()[0] == 0


# -- connections left inside a transaction -----------------------------------------

def test_a_failed_write_does_not_leave_its_thread_on_an_old_snapshot(tmp_path):
    path = tmp_path / "index.db"
    db.init_db(path)
    outcome = {}

    def request_thread():
        conn = db.connect(path)
        conn.execute("INSERT INTO meta(key, value) VALUES('a', '1')")
        assert conn.in_transaction
        outcome["ended"] = db.end_open_transactions(failed=True)
        outcome["after"] = conn.in_transaction
        db.close_all()

    worker = threading.Thread(target=request_thread)
    worker.start()
    worker.join()
    assert outcome == {"ended": 1, "after": False}
    db.close_all()


def test_a_request_that_wrote_without_committing_has_it_committed(tmp_path):
    path = tmp_path / "index.db"
    db.init_db(path)

    def request_thread():
        conn = db.connect(path)
        conn.execute("INSERT INTO meta(key, value) VALUES('kept', '1')")
        db.end_open_transactions(failed=False)
        db.close_all()

    worker = threading.Thread(target=request_thread)
    worker.start()
    worker.join()
    check = sqlite3.connect(path)
    assert check.execute("SELECT value FROM meta WHERE key='kept'").fetchone() == ("1",)
    check.close()
    db.close_all()


def test_the_web_app_ends_a_request_s_open_transaction(app):
    @app.get("/api/_test_leaves_a_transaction")
    def leaves():                                       # noqa: ANN202
        conn = db.connect(app.config["MV_CONFIG"].db_path)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('left', '1')")
        return "ok"

    client = app.test_client()
    client.get("/api/_test_leaves_a_transaction")
    for conn in db._local.conns.values():
        assert not conn.in_transaction


# -- the backup's queue -------------------------------------------------------------

def test_queueing_the_library_survives_another_writer_committing_meanwhile(scanned, monkeypatch):
    from ninaivu.cloud import service as service_mod

    cfg, conn, _ = scanned
    monkeypatch.setattr(service_mod, "QUEUE_PAGE", 3)
    other = sqlite3.connect(cfg.db_path, timeout=5)
    seen = []

    def on_queued(total):
        seen.append(total)
        # The indexer, committing between two pages.
        other.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('tick', ?)",
                      (str(total),))
        other.commit()

    rows = list(service_mod._in_pages(conn, "SELECT id, root, rel_path FROM assets "
                                      "WHERE trashed=0", []))
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert len(rows) == conn.execute("SELECT COUNT(*) FROM assets WHERE trashed=0").fetchone()[0]
    from ninaivu.cloud import store
    store.init_schema(conn)
    full = conn.execute("SELECT root, rel_path, filename, size, mtime, kind, id FROM assets "
                        "WHERE trashed=0").fetchall()
    queued = store.queue_missing(
        conn, service_mod._in_pages(conn, "SELECT id, root, rel_path, filename, size, mtime, "
                                    "kind FROM assets WHERE trashed=0", []),
        on_queued=on_queued)
    assert queued == len(full)
    other.close()


# -- the search matrix while tagging runs newest first ------------------------------

def test_vectors_filed_out_of_order_are_appended_not_rebuilt(tmp_path, monkeypatch):
    conn = db.init_db(tmp_path / "index.db")
    for asset_id in range(1, 11):
        conn.execute("INSERT INTO assets(id, root, rel_path, filename, folder, kind, size, "
                     "mtime, indexed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (asset_id, "/r", f"{asset_id}.jpg", f"{asset_id}.jpg", "", "picture",
                      1, 0, 0))
    conn.commit()

    def vector(i):
        return np.array([i, 1], np.float32).tobytes()

    for asset_id in (10, 9):                            # newest first
        db.store_embedding(conn, asset_id, "m", 2, vector(asset_id))
    embeddings.forget_embeddings()
    db.embedding_store(conn)

    rebuilt = []
    real = embeddings._rebuild_embeddings
    monkeypatch.setattr(embeddings, "_rebuild_embeddings",
                        lambda *a: (rebuilt.append(1), real(*a)))
    for asset_id in (8, 7, 6):
        db.store_embedding(conn, asset_id, "m", 2, vector(asset_id))
        ids, buffer, dim, index = db.embedding_store(conn)
    assert rebuilt == []
    matrix = np.frombuffer(buffer, np.float32).reshape(-1, dim)
    for asset_id in (10, 9, 8, 7, 6):
        assert matrix[index[asset_id]][0] == asset_id
    db.close_all()


def test_replaced_vectors_are_not_kept_when_no_matrix_is_held(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    conn.execute("INSERT INTO assets(id, root, rel_path, filename, folder, kind, size, mtime, "
                 "indexed_at) VALUES(1,'/r','1.jpg','1.jpg','','picture',1,0,0)")
    conn.commit()
    embeddings.forget_embeddings()
    embeddings._EMBED_REPLACED.clear()
    for i in range(5):
        db.store_embedding(conn, 1, "m", 2, np.array([i, 1], np.float32).tobytes())
    assert embeddings._EMBED_REPLACED == {}
    db.close_all()


# -- the off-site folder ------------------------------------------------------------

def test_the_off_site_copy_never_recreates_a_folder_whose_disk_went_away(tmp_path):
    from ninaivu.cloud import offsite

    mount = tmp_path / "mount"
    mount.mkdir()
    target = offsite.FolderTarget(mount)
    target.put_bytes(offsite.MARKER, b"{}")               # a new destination: allowed
    source = tmp_path / "file.bin"
    source.write_bytes(b"x" * 1000)
    target.put_file("files/ab/one.ninaivu", source)

    # The disk drops out: the mount point is left, empty.
    import shutil
    shutil.rmtree(mount / "ninaivu-offsite")
    with pytest.raises(OSError):
        target.put_file("files/ab/two.ninaivu", source)
    assert not (mount / "ninaivu-offsite").exists()
    assert target.gone()


def test_stopping_the_off_site_copy_stops_inside_a_large_file(tmp_path):
    from ninaivu.cloud import offsite

    target = offsite.FolderTarget(tmp_path)
    target.put_bytes(offsite.MARKER, b"{}")
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * (9 * 1024 * 1024))
    with pytest.raises(OSError):
        target.put_file("files/ab/big.ninaivu", source, stop=lambda: True)
    assert not list((tmp_path / "ninaivu-offsite").rglob("big.ninaivu*"))


# -- the console's streams ----------------------------------------------------------

def test_status_streams_leave_half_the_web_threads_free():
    from types import SimpleNamespace

    from ninaivu.api import _streams

    cfg = SimpleNamespace(server_threads=6)
    taken = 0
    while _streams.take(cfg):
        taken += 1
        assert taken < 50
    try:
        assert taken == 3
    finally:
        for _ in range(taken):
            _streams.give()
    assert _streams.held() == 0


# -- the cloud upload starts once ---------------------------------------------------

def test_two_starts_at_once_start_one_upload_loop():
    from ninaivu.cloud import engine as engine_mod

    started = []
    release = threading.Event()

    class Engine(engine_mod.SyncEngine):
        def __init__(self):                             # noqa: D107 - only what start needs
            self._start_lock = threading.Lock()
            self._thread = None
            self._stop = threading.Event()
            self._folders = {}
            self.state = engine_mod.SyncState()

        def _run(self):
            started.append(1)
            release.wait(5)

    eng = Engine()
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        eng.start()

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    release.set()
    assert len(started) == 1
