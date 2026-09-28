"""Queueing the library must not read the disk, and must not block the start.

Pressing Start on a 156,000-file library uploaded nothing for hours: queueing
stat'ed every file — 113 ms each on a USB archive — on the request's own
thread, and wrote the queue in one transaction at the end, so nothing could be
sent until the whole index had been walked.
"""

import threading
import time

import pytest

from ninaivu.cloud import service as cloud_service
from ninaivu.cloud import store
from ninaivu.storage import db


@pytest.fixture()
def backup(cfg, scanned, monkeypatch):
    _, conn, _ = scanned
    monkeypatch.setattr("ninaivu.ai.get_engine", lambda: None)
    cfg.cloud_enabled = True
    store.init_schema(conn)
    return cloud_service.CloudService(cfg, lambda: db.connect(cfg.db_path)), conn


def test_queueing_never_touches_the_disk(backup, monkeypatch):
    """The size comes from the index; the upload reads the file's stamp itself."""
    _, conn = backup
    monkeypatch.setattr(store, "source_version",
                        lambda path: pytest.fail(f"queueing stat'ed {path}"))
    rows = [{"root": "R", "rel_path": f"a/{n}.jpg", "filename": f"{n}.jpg", "size": 10}
            for n in range(5)]
    assert store.queue_missing(conn, rows) == 5
    assert conn.execute("SELECT COUNT(*) FROM cloud_uploads WHERE state='pending'"
                        ).fetchone()[0] == 5


def test_the_queue_is_written_as_it_goes(backup, monkeypatch):
    """So the first files can go up while the rest are still being queued."""
    _, conn = backup
    monkeypatch.setattr(store, "QUEUE_BATCH", 2)
    seen = []

    def rows():
        for n in range(6):
            # What the queue holds by the time each asset is offered.
            seen.append(conn.execute("SELECT COUNT(*) FROM cloud_uploads").fetchone()[0])
            yield {"root": "R", "rel_path": f"b/{n}.jpg", "filename": f"{n}.jpg", "size": 10}

    totals = []
    assert store.queue_missing(conn, rows(), on_queued=totals.append) == 6
    assert totals == [2, 4, 6], totals
    assert max(seen) >= 2, "nothing was written until the walk had finished"


def test_an_unchanged_file_is_left_alone_and_a_changed_one_goes_again(backup):
    _, conn = backup
    rows = [{"root": "R", "rel_path": "c/1.jpg", "filename": "1.jpg", "size": 10}]
    store.queue_missing(conn, rows)
    conn.execute("UPDATE cloud_uploads SET state='done' WHERE rel_path='c/1.jpg'")
    conn.commit()
    assert store.queue_missing(conn, rows) == 0

    changed = [{"root": "R", "rel_path": "c/1.jpg", "filename": "1.jpg", "size": 999}]
    assert store.queue_missing(conn, changed) == 1
    assert store.state_of(conn, "R", "c/1.jpg") == "pending"


def test_start_returns_before_the_library_is_queued(backup, monkeypatch):
    backup_service, _ = backup
    monkeypatch.setattr(backup_service.creds, "refresh_token", "r", raising=False)
    queued = threading.Event()
    slow = threading.Event()

    def queue_library(on_queued=None):
        queued.set()
        slow.wait(5)          # as a real walk of a large library would
        return 0

    monkeypatch.setattr(backup_service, "queue_library", queue_library)
    monkeypatch.setattr(backup_service, "_queue_for_the_engine", queue_library)
    monkeypatch.setattr(cloud_service.SyncEngine, "_connect", lambda self: object(),
                        raising=False)

    began = time.time()
    result = backup_service.start()
    assert time.time() - began < 2, "Start waited for the library to be queued"
    assert result["started"] is True
    assert "queued" not in result
    assert queued.wait(5), "the engine never asked for the queue"
    slow.set()


def test_the_console_is_told_while_it_is_queueing(backup, monkeypatch):
    backup_service, _ = backup
    engine = backup_service.engine()
    monkeypatch.setattr(backup_service, "queue_library",
                        lambda on_queued=None: (on_queued(7) if on_queued else None) or 7)
    assert backup_service._queue_for_the_engine() == 7
    assert engine.state.snapshot()["queued_so_far"] == 7


def test_a_file_edited_in_place_goes_again_without_reading_it(backup, monkeypatch):
    """Same size, new timestamp. The index knows; the disk is not consulted."""
    _, conn = backup
    monkeypatch.setattr(store, "source_version",
                        lambda path: pytest.fail(f"queueing read {path}"))
    first = {"root": "R", "rel_path": "d/1.jpg", "filename": "1.jpg",
             "size": 10, "mtime": 100.0}
    store.queue_missing(conn, [first])
    conn.execute("UPDATE cloud_uploads SET state='done' WHERE rel_path='d/1.jpg'")
    conn.commit()

    assert store.queue_missing(conn, [dict(first)]) == 0, "an unchanged file went again"
    assert store.queue_missing(conn, [{**first, "mtime": 200.0}]) == 1
    assert store.state_of(conn, "R", "d/1.jpg") == "pending"


def test_the_queue_carries_the_timestamp_the_index_had(backup):
    backup_service, conn = backup
    backup_service.queue_library()
    rows = conn.execute("SELECT u.source_mtime, a.mtime FROM cloud_uploads u "
                        "JOIN assets a ON a.root=u.root AND a.rel_path=u.rel_path").fetchall()
    assert rows, "nothing was queued"
    assert all(abs(row["source_mtime"] - row["mtime"]) < 1e-6 for row in rows)
