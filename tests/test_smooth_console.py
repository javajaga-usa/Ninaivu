"""The console's polled status endpoints stay cheap, and never wait on a writer.

The Overview, the activity strip, the Cloud page, the Storage check page and
the Server page each ask the server something every few seconds from every
open tab. On a Raspberry Pi with a few hundred thousand photographs each ask
cost hundreds of milliseconds, and the Cloud one took the write lock, so it
stalled for as long as a scan's commit did. These check that the answers are
kept for a few seconds, that they are taken again when they should be, and
that the faster queries give the same answers as the ones they replaced.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.api import admin_api, archive_api, server_api
from ninaivu.archive import database as adb
from ninaivu.cloud import service as cloud_service
from ninaivu.cloud import store
from ninaivu.server import auth
from ninaivu.server import workload as wl
from ninaivu.storage import db
from ninaivu.storage.repair import Repairer


@pytest.fixture()
def console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    yield login(app.test_client(), *ADMIN), cfg, conn
    services.stop(timeout=5.0)


@pytest.fixture()
def backup(cfg, scanned, monkeypatch):
    _, conn, _ = scanned
    monkeypatch.setattr("ninaivu.ai.get_engine", lambda: None)
    store.init_schema(conn)
    return cloud_service.CloudService(cfg, lambda: db.connect(cfg.db_path)), conn


def _in_a_thread(work, wait: float = 5.0):
    """Run *work* elsewhere; the time it took, or None if it was still waiting."""
    took: list[float] = []

    def run():
        started = time.monotonic()
        work()
        took.append(time.monotonic() - started)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(wait)
    return took[0] if took else None


# --- the cloud queue's tables are made once --------------------------------

def test_the_cloud_status_does_not_wait_for_a_scan_to_commit(backup, cfg):
    """A second ask only reads: with another connection holding the write
    lock, the status still comes back at once rather than after the busy
    timeout."""
    service, conn = backup
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, filename, state, kind) "
                 "VALUES('R', 'a.jpg', 'a.jpg', 'pending', '')")
    conn.commit()
    service.status()
    assert service.queue_progress() == (0, 1)

    writer = sqlite3.connect(str(cfg.db_path), timeout=1.0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE meta SET value=value WHERE key='schema_version'")
        service._counts = None
        service._queue_seen = None
        took = _in_a_thread(lambda: (service.status(), service.queue_progress()))
        assert took is not None and took < 2.0, "the status waited on the writer"
        took = _in_a_thread(lambda: store.init_schema(db.connect(cfg.db_path)))
        assert took is not None and took < 2.0
    finally:
        writer.rollback()
        writer.close()


def test_a_fresh_database_still_gets_its_tables(tmp_path):
    """Once per file, not once per process: a new file at a path set up
    before, as a restore from a backup makes, is set up again."""
    path = tmp_path / "queue.db"
    first = sqlite3.connect(path)
    store.init_schema(first)
    first.execute("INSERT INTO cloud_uploads(root, rel_path, filename) VALUES('R', 'a', 'a')")
    first.commit()
    first.close()
    path.unlink()

    again = sqlite3.connect(path)
    store.init_schema(again)
    assert again.execute("SELECT COUNT(*) FROM cloud_uploads").fetchone()[0] == 0
    assert again.execute("SELECT COUNT(*) FROM cloud_approvals").fetchone()[0] == 0
    columns = {row[1] for row in again.execute("PRAGMA table_info(cloud_uploads)")}
    assert {"kind", "md5", "resume_size", "source_mtime"} <= columns
    again.close()


def test_rows_from_before_the_kind_column_are_still_given_one(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript(store.SCHEMA)
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, filename) "
                 "VALUES('R', 'a/b.jpg', 'b.jpg')")
    conn.commit()
    store.init_schema(conn)
    assert conn.execute("SELECT kind FROM cloud_uploads").fetchone()[0] == store.PICTURE


# --- the Cloud page's counts are kept a few seconds ---------------------------

def test_the_cloud_counts_are_taken_again_once_they_are_old(backup, monkeypatch):
    service, conn = backup
    service.STATUS_FRESH_FOR = 0.3
    counted = []
    real = store.summary
    monkeypatch.setattr(store, "summary", lambda c: counted.append(1) or real(c))
    assert service.status()["queue"]["total"] == 0
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, filename, state, done_at) "
                 "VALUES('R', 'a.jpg', 'a.jpg', 'done', 5)")
    conn.commit()
    assert service.status()["queue"]["total"] == 0, "kept for a few seconds"
    assert len(counted) == 1
    time.sleep(0.35)
    later = service.status()
    assert later["queue"]["total"] == 1 and later["recent"][0]["rel_path"] == "a.jpg"
    assert len(counted) == 2
    service.retry_failures()
    service.status()
    assert len(counted) == 3, "a change made here is counted at once"


def test_recent_lists_what_the_old_query_listed(backup):
    """Read through the index now, newest first by the same expression."""
    _, conn = backup
    rows = []
    for n in range(60):
        state = store.STATES[n % len(store.STATES)]
        done_at = 1000.0 + n * 7 if state == store.DONE or n % 4 == 0 else 0
        rows.append(("R", f"f{n}.jpg", f"f{n}.jpg", state, 2000.0 - n * 3, done_at))
    conn.executemany("INSERT INTO cloud_uploads(root, rel_path, filename, state, queued_at, "
                     "done_at) VALUES(?,?,?,?,?,?)", rows)
    conn.commit()

    def old(state=None, limit=12):
        where = "WHERE state=? " if state else ""
        return [r["rel_path"] for r in conn.execute(
            f"SELECT * FROM cloud_uploads {where}"
            "ORDER BY COALESCE(NULLIF(done_at,0), queued_at) DESC LIMIT ?",
            ((state, limit) if state else (limit,)))]

    for state in (None, *store.STATES):
        assert [r["rel_path"] for r in store.recent(conn, 12, state)] == old(state), state
    plan = " ".join(str(r[3]) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM cloud_uploads WHERE state=? "
        "ORDER BY COALESCE(NULLIF(done_at,0), queued_at) DESC LIMIT 12", ("done",)))
    assert "idx_cloud_state_recent" in plan and "TEMP B-TREE" not in plan


# --- the storage check ---------------------------------------------------------

def _old_bitrot_counts(conn) -> dict[str, int]:
    return {str(r[0]): int(r[1]) for r in conn.execute(
        "SELECT b.status, COUNT(*) FROM bitrot_records b "
        "WHERE b.id = (SELECT MAX(b2.id) FROM bitrot_records b2 "
        "              WHERE b2.asset_id = b.asset_id) GROUP BY b.status")}


def _record(conn, asset_id, status, at):
    conn.execute("INSERT INTO bitrot_records(asset_id, root, rel_path, actual_hash, status, "
                 "checked_at) VALUES(?, 'R', ?, 'h', ?, ?)", (asset_id, f"{asset_id}", status, at))


def test_the_bitrot_summary_counts_what_the_old_query_counted(scanned):
    _, conn, _ = scanned
    ids = [r[0] for r in conn.execute("SELECT id FROM assets ORDER BY id")]
    assert len(ids) >= 3
    passes = [("baseline",) * len(ids),
              tuple("verified" if n % 3 else "corrupt" for n in range(len(ids))),
              tuple(("verified", "missing", "repaired", "unreadable")[n % 4]
                    for n in range(len(ids)))]
    for number, statuses in enumerate(passes):
        for asset_id, status in zip(ids if number != 2 else ids[:-1], statuses):
            _record(conn, asset_id, status, 100.0 * number + asset_id)
    # A row with no asset is nobody's latest, before and after.
    conn.execute("INSERT INTO bitrot_records(asset_id, root, rel_path, status, checked_at) "
                 "VALUES(NULL, 'R', 'gone', 'corrupt', 999)")
    conn.commit()

    summary = db.get_bitrot_summary(conn)
    old = _old_bitrot_counts(conn)
    assert {s: summary[s] for s in db.BITROT_STATES if summary[s]} == old
    assert sum(summary[s] for s in db.BITROT_STATES) == len(ids)


def test_the_check_status_is_kept_while_a_check_runs(console, monkeypatch):
    client, _, conn = console
    asset_id = conn.execute("SELECT MIN(id) FROM assets").fetchone()[0]
    _record(conn, asset_id, "baseline", 1.0)
    conn.commit()
    asked = []
    real = db.get_bitrot_summary
    monkeypatch.setattr(db, "get_bitrot_summary", lambda c: asked.append(1) or real(c))
    progress = dict(admin_api._SCRUBBER_PROGRESS, running=True, corrupt=0, missing=0,
                    unreadable=0)
    monkeypatch.setattr(admin_api, "_SCRUBBER_PROGRESS", progress)

    first = client.get("/api/admin/scrubber/status").get_json()
    _record(conn, asset_id, "verified", 2.0)
    conn.commit()
    second = client.get("/api/admin/scrubber/status").get_json()
    assert asked == [1] and second["baseline"] == first["baseline"] == 1

    progress["missing"] = 1                    # something wrong: shown at once
    third = client.get("/api/admin/scrubber/status").get_json()
    assert len(asked) == 2 and third["verified"] == 1

    progress["running"] = False                # finished: always the real totals
    client.get("/api/admin/scrubber/status")
    client.get("/api/admin/scrubber/status")
    assert len(asked) == 4


# --- what is waiting for a person -----------------------------------------------

def test_the_unnamed_groups_are_counted_once_a_minute(console, monkeypatch):
    client, _, conn = console
    counted = []
    monkeypatch.setattr(db, "count_unnamed_clusters",
                        lambda *a, **k: counted.append(1) or 4)
    for _ in range(3):
        faces = [i for i in client.get("/api/admin/attention").get_json()["items"]
                 if i["key"] == "faces"]
        assert faces[0]["count"] == 4
    assert counted == [1]
    db.set_face_person(conn, 999_999, None)    # naming a face counts again at once
    client.get("/api/admin/attention")
    assert counted == [1, 1]
    monkeypatch.setattr(admin_api, "UNNAMED_FRESH_FOR", 0.0)
    client.get("/api/admin/attention")
    assert len(counted) == 3


# --- the Server page --------------------------------------------------------------

def test_the_server_page_looks_up_addresses_once_a_minute(console, monkeypatch):
    client, cfg, _ = console
    from ninaivu.server import remote
    from ninaivu.utils import tls
    monkeypatch.setattr(server_api, "_lookups", {})
    looked, resolved = [], []
    monkeypatch.setattr(tls, "lan_addresses", lambda: looked.append(1) or ["192.168.1.9"])
    real = remote.resolve
    monkeypatch.setattr(remote, "resolve", lambda c, **k: resolved.append(1) or real(
        c, tailscale_present=lambda: False, tailnet_name=lambda: ""))
    cfg.host = "0.0.0.0"
    cfg.remote_access = "tunnel"
    cfg.remote_hostname = "photos.example.org"

    first = client.get("/api/admin/server").get_json()
    second = client.get("/api/admin/server").get_json()
    assert looked == [1] and resolved == [1]
    assert second["network"]["addresses"] == ["192.168.1.9"]
    assert first["remote_access"] == "tunnel"
    assert first["remote_hostname"] == "photos.example.org"
    assert first["remote_networks"] == []
    assert first["endpoints"]["remote_access"]["name"] == "tunnel"

    cfg.remote_access = "none"                 # a changed setting is looked up again
    third = client.get("/api/admin/server").get_json()
    assert resolved == [1, 1] and third["endpoints"]["remote_access"]["name"] == "none"


# --- the Archive tab's stream -----------------------------------------------------

class _Enough(Exception):
    pass


def _stream_ticks(client, monkeypatch, ticks: int) -> None:
    slept = []

    def sleep(_seconds):
        slept.append(1)
        if len(slept) >= ticks:
            raise _Enough

    monkeypatch.setattr(archive_api.time, "sleep", sleep)
    response = client.get("/api/archive/stream")
    with pytest.raises(_Enough):
        for _chunk in response.response:
            pass


def test_an_idle_archive_is_not_counted_every_second(console, monkeypatch):
    client, _, _ = console
    counted = []
    real = adb.get_stats
    monkeypatch.setattr(adb, "get_stats", lambda: counted.append(1) or real())
    monkeypatch.setattr(archive_api, "is_scanning", lambda: False)
    _stream_ticks(client, monkeypatch, 6)
    assert counted == [1], "counted again while nothing was running"

    counted.clear()
    monkeypatch.setattr(archive_api, "is_scanning", lambda: True)
    _stream_ticks(client, monkeypatch, 6)
    assert len(counted) == 6, "a running job is counted every second"


# --- the Overview -----------------------------------------------------------------

def test_the_overview_folders_follow_the_library(console):
    client, cfg, conn = console
    first = client.get("/api/admin/overview").get_json()["library"]["folders"][0]
    assert first["count"] > 0
    assert any(key[1:2] == ("library_summary",) for key in db._AGGREGATES)
    conn.execute("UPDATE assets SET trashed=1 WHERE id=(SELECT MIN(id) FROM assets)")
    conn.commit()
    after = client.get("/api/admin/overview").get_json()["library"]["folders"][0]
    assert after["count"] == first["count"] - 1


def test_an_aggregate_with_an_age_limit_is_worked_out_again(scanned):
    _, conn, _ = scanned
    worked = []
    for _ in range(2):
        db.cached_aggregate(conn, ("aged",), lambda: worked.append(1) or 1, max_age=0.2)
    assert worked == [1]
    time.sleep(0.25)
    db.cached_aggregate(conn, ("aged",), lambda: worked.append(1) or 1, max_age=0.2)
    assert worked == [1, 1]


# --- the scheduled check keeps the household's night ------------------------------

def _at(hour, minute=0):
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))


@pytest.mark.parametrize("night,hour,expected", [
    (("00:00", "07:00"), 6.5, True),          # after the fixed hours, inside theirs
    (("00:00", "07:00"), 12, False),
    (("23:00", "06:00"), 23.5, True),
    (("02:00", "04:00"), 1.5, False),         # inside the fixed hours, outside theirs
    (("", ""), 3, True),                      # no night of their own: one to six
])
def test_the_scheduled_check_starts_in_the_households_night(cfg, night, hour, expected):
    stamp = _at(int(hour), int(hour % 1 * 60))
    work = wl.Workload(SimpleNamespace(workload_mode="balanced",
                                       workload_night_start=night[0] or "x",
                                       workload_night_end=night[1] or "x"))
    repairer = Repairer(cfg, lambda: db.connect(cfg.db_path), clock=lambda: stamp,
                        workload=work)
    assert repairer.is_night() is expected


def test_without_a_workload_the_fixed_hours_still_hold(cfg):
    at = {hour: Repairer(cfg, lambda: db.connect(cfg.db_path), clock=lambda h=hour: _at(h))
          for hour in (0, 1, 5, 6, 12)}
    assert [at[h].is_night() for h in (0, 1, 5, 6, 12)] == [False, True, True, False, False]
