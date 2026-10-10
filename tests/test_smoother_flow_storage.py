"""Smoother flow: hand-offs between jobs that dropped what they were given.

A storage check carried on after a restart that never handed over to repair
and forgot what it had found; a backup that missed a change arriving as it
finished; compressed videos the cloud never heard about; half-written copies
a restart left for good; an off-site copy put off a whole day by one refusal;
archive workers and watchers letting go of each other's claims; a failing
backup bundle rebuilt every quarter-hour; locks kept for every photograph
ever downloaded; and a copy that waited ten minutes for a check that had
already stepped aside for it.
"""

from __future__ import annotations

import gc
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.api import admin_api, archive_api
from ninaivu.archive import scanner as archive_scanner
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import keyring, offsite as offsite_mod
from ninaivu.cloud.offsite import Offsite
from ninaivu.cloud.service import CloudService
from ninaivu.media import video_compress as vc
from ninaivu.media.scanner import CLAIM_STORAGE_CHECK
from ninaivu.storage import backup, db, mirror as mirror_mod, resume
from ninaivu.utils.location import LocationFreeCopies


def _wait_for(predicate, timeout=5.0):
    ends = time.monotonic() + timeout
    while time.monotonic() < ends:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def _the_check_is_not_left_stopped():
    """A Stop pressed here is not left set for a later test that runs the
    storage check directly (only start_scrubber_job clears it)."""
    yield
    admin_api._SCRUBBER_STOP.clear()


def _check_finished():
    assert _wait_for(lambda: not admin_api.scrubber_running(), 30)


# -- 1. the storage check carried on after a restart ---------------------------

def test_a_check_carried_on_after_a_restart_hands_over_to_repair(scanned):
    cfg, conn, _ = scanned
    repaired = []
    admin_api.after_every_check(cfg.db_path, lambda: repaired.append(True))
    try:
        # As start-up starts it (_resume_the_jobs): no on_done of its own.
        assert admin_api.start_scrubber_job(cfg.db_path, 0)
        _check_finished()
    finally:
        admin_api.after_every_check(cfg.db_path, None)
    assert repaired == [True]


def test_the_alert_after_a_restart_counts_what_was_found_before_it(scanned):
    cfg, conn, _ = scanned
    ids = [r["id"] for r in conn.execute("SELECT id FROM assets WHERE trashed=0 ORDER BY id")]
    admin_api._run_scrubber(cfg.db_path)                 # fingerprints first
    # The check had reached the third file, and found two damaged, when the
    # machine restarted: written down at its checkpoint.
    resume.want(conn, admin_api.SCRUBBER_RESUME,
                {"after_id": ids[2], "found": {"corrupt": 2, "verified": 1}})
    sent = []
    admin_api._SCRUBBER_RUNNING = True                  # what start_scrubber_job sets
    assert admin_api._run_scrubber(
        cfg.db_path, ids[2], notify=lambda event, summary, detail="": sent.append(summary))
    assert admin_api._SCRUBBER_PROGRESS["corrupt"] == 2
    assert admin_api._SCRUBBER_PROGRESS["verified"] == 1 + len(ids[3:])
    assert "2 file(s) changed on disk" in sent


def test_the_resume_record_carries_the_counts(scanned):
    cfg, conn, _ = scanned
    seen = []
    real_want = resume.want

    def watching(c, name, args=None):
        seen.append(dict(args or {}))
        real_want(c, name, args)

    admin_api._SCRUBBER_RUNNING = True
    try:
        resume.want = watching
        admin_api.SCRUBBER_CHECKPOINT, old = 2, admin_api.SCRUBBER_CHECKPOINT
        admin_api._run_scrubber(cfg.db_path)
    finally:
        resume.want = real_want
        admin_api.SCRUBBER_CHECKPOINT = old
    checkpoints = [a for a in seen if a.get("after_id")]
    assert checkpoints and all("found" in a for a in checkpoints)
    assert checkpoints[-1]["found"]["baseline"] >= 2


# -- 11. the check waiting for its turn ------------------------------------------

def test_a_check_waiting_for_its_turn_lets_the_indexer_go_and_hears_stop(scanned):
    cfg, _conn, _ = scanned
    calls = []
    waiting = threading.Event()

    class Night:
        def wait_turn(self, job, stop=None, on_hold=None):
            on_hold("saved for the night")
            waiting.set()
            try:
                return not stop.wait(30)
            finally:
                on_hold(None)

    scanner = SimpleNamespace(workload=Night(),
                              defer=lambda r: calls.append(("defer", r)),
                              resume=lambda r=None: calls.append(("resume", r)))
    assert admin_api.start_scrubber_job(cfg.db_path, scanner=scanner)
    assert waiting.wait(10)
    # Waiting for the night, the indexer is free for the day's photographs.
    assert calls == [("defer", CLAIM_STORAGE_CHECK), ("resume", CLAIM_STORAGE_CHECK)]
    started = time.monotonic()
    assert admin_api.stop_scrubber(timeout=10)
    assert time.monotonic() - started < 10, "Stop was not heard during the wait"
    _check_finished()
    # Taken back when the wait ended, and let go once more at the end: balanced.
    assert calls == [("defer", CLAIM_STORAGE_CHECK), ("resume", CLAIM_STORAGE_CHECK),
                     ("defer", CLAIM_STORAGE_CHECK), ("resume", CLAIM_STORAGE_CHECK)]


# -- 2. the cloud backup told of a change as it finished -----------------------

def _engine():
    return engine_mod.SyncEngine(connect=lambda: object(), open_db=lambda: None)


def test_a_change_arriving_as_a_run_ends_starts_another(monkeypatch):
    engine = _engine()
    runs = []

    def once():
        runs.append(1)
        if len(runs) == 1:
            # The scan found something while the run was on its way out.
            assert engine.library_changed() is True
        return True

    monkeypatch.setattr(engine, "_run_once", once)
    assert engine.start()
    assert _wait_for(lambda: len(runs) == 2 and not engine.running)
    assert len(runs) == 2


def test_a_run_that_has_left_its_loop_is_not_running_and_can_be_started(monkeypatch):
    engine = _engine()
    engine._thread = threading.current_thread()          # alive, tidying up
    engine._wind_down(True)
    assert engine.running is False
    assert engine.library_changed() is False             # the caller starts one
    ran = threading.Event()
    monkeypatch.setattr(engine, "_run_once", lambda: ran.set() or False)
    assert engine.start() is True
    assert ran.wait(5)


def test_a_paused_run_is_not_started_again_by_its_last_look(monkeypatch):
    engine = _engine()
    runs = []

    def once():
        runs.append(1)
        engine.library_changed()
        engine._stop.set()
        return True

    monkeypatch.setattr(engine, "_run_once", once)
    engine.start()
    assert _wait_for(lambda: not engine.running)
    time.sleep(0.1)
    assert runs == [1]


def test_the_service_starts_a_run_when_the_engine_says_it_will_not_see_it(cfg, monkeypatch):
    conn = db.init_db(cfg.db_path)
    service = CloudService(cfg, lambda: conn)
    monkeypatch.setattr(type(service.creds), "connected", property(lambda _self: True))
    started = []
    monkeypatch.setattr(service, "start", lambda: started.append(True))
    cfg.cloud_enabled = True
    service._follow(True)
    service._engine = SimpleNamespace(running=True, library_changed=lambda: False)
    service.library_changed()
    assert started == [True]


# -- 3. a compressed video goes to the cloud backup ----------------------------

def _compress_setup(tmp_path, monkeypatch):
    folder = tmp_path / "lib" / "2024"
    folder.mkdir(parents=True)
    (folder / "party.mov").write_bytes(b"V" * 4000)
    row = {"id": 7, "root": str(tmp_path / "lib"), "rel_path": "2024/party.mov",
           "filename": "party.mov", "duration": 10.0, "size": 4000}
    monkeypatch.setattr(admin_api, "_copy_place", lambda cfg, r: (r["root"], folder))
    monkeypatch.setattr(vc, "same_disk_space", lambda folder, needed: True)
    monkeypatch.setattr(vc, "encode", lambda s, t, d, report, cancelled: t.write_bytes(b"v"))
    monkeypatch.setattr(vc, "verify", lambda *a: {"frame": None})
    cfg = SimpleNamespace(db_path=tmp_path / "index.db")
    return cfg, row, folder


def test_a_finished_compression_tells_the_backup(tmp_path, monkeypatch):
    cfg, row, _folder = _compress_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(admin_api, "_publish_copy", lambda *a: {"mode": "copy"})
    heard = []
    cloud = SimpleNamespace(library_changed=lambda: heard.append(True))
    work = admin_api._compress_work(cfg, row, "copy", 1, cloud=cloud)
    assert work({"id": "job"}, lambda p: None, lambda: False) == {"mode": "copy"}
    assert heard == [True]


def test_a_failed_compression_does_not(tmp_path, monkeypatch):
    cfg, row, _folder = _compress_setup(tmp_path, monkeypatch)

    def refused(*a):
        raise vc.CompressError("no")

    monkeypatch.setattr(admin_api, "_publish_copy", refused)
    heard = []
    work = admin_api._compress_work(cfg, row, "copy", 1,
                                    cloud=SimpleNamespace(library_changed=lambda: heard.append(1)))
    with pytest.raises(vc.CompressError):
        work({"id": "job"}, lambda p: None, lambda: False)
    assert heard == []


# -- 4. half-written compressed copies left by a restart -------------------------

def _video_library(tmp_path):
    lib = tmp_path / "lib"
    films, elsewhere = lib / "films", lib / "other"
    films.mkdir(parents=True)
    elsewhere.mkdir()
    (films / "party.mov").write_bytes(b"V")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE assets (root TEXT, rel_path TEXT, kind TEXT, trashed INT)")
    conn.execute("INSERT INTO assets VALUES (?, 'films/party.mov', 'video', 0)", (str(lib),))
    return conn, films, elsewhere


def test_leftovers_beside_indexed_videos_are_cleared_and_nothing_else(tmp_path):
    conn, films, elsewhere = _video_library(tmp_path)
    old = time.time() - 2 * vc.STALE_TEMP_SECONDS
    stale = vc.temporary_for(films, "party", "0123456789ab")
    other = vc.temporary_for(films, "holiday", "ba9876543210")
    fresh = vc.temporary_for(films, "party", "aaaaaaaaaaaa")    # an encode still writing
    unrelated = films / "party.tmp"
    beyond = vc.temporary_for(elsewhere, "party", "0123456789ab")  # no indexed video here
    for path in (stale, other, fresh, unrelated, beyond):
        path.write_bytes(b"x")
    for path in (stale, other, unrelated, beyond):
        os.utime(path, (old, old))
    assert vc.clear_leftovers(conn) == 2
    assert not stale.exists() and not other.exists()
    assert fresh.exists() and unrelated.exists() and beyond.exists()


def test_the_copy_of_a_compression_still_queued_is_left(tmp_path, monkeypatch):
    conn, films, _elsewhere = _video_library(tmp_path)
    monkeypatch.setitem(vc._jobs, "cafecafecafe", {"state": "queued"})
    old = time.time() - 2 * vc.STALE_TEMP_SECONDS
    queued = vc.temporary_for(films, "party", "cafecafecafe")
    queued.write_bytes(b"x")
    os.utime(queued, (old, old))
    assert vc.clear_leftovers(conn) == 0
    assert queued.exists()


# -- 5. the off-site copy after a refusal ----------------------------------------

@pytest.fixture()
def offsite_lib(scanned, tmp_path):
    cfg, conn, _ = scanned
    keyring.create(cfg.state_dir, "correct horse battery", "correct horse battery")
    cfg.offsite_kind = "folder"
    cfg.offsite_enabled = True
    cfg.offsite_folder = str(tmp_path / "friends-disk")
    Path(cfg.offsite_folder).mkdir()
    clock = [1_000_000.0]
    offsite = Offsite(cfg, lambda: db.connect(cfg.db_path), clock=lambda: clock[0])
    return cfg, offsite, clock


def _meta(cfg, key):
    row = db.connect(cfg.db_path).execute(
        "SELECT value FROM offsite_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def test_a_refused_run_is_tried_again_in_an_hour_not_a_day(offsite_lib, monkeypatch):
    cfg, offsite, clock = offsite_lib

    def refused(*a, **k):
        raise offsite_mod.Refused("the disk is not plugged in")

    monkeypatch.setattr(offsite, "_know_destination", refused)
    offsite.start()
    offsite.join(60)
    assert not _meta(cfg, "last_run"), "a refused run is not a finished one"
    assert float(_meta(cfg, "last_attempt")) == clock[0]
    clock[0] += 20 * 60
    assert not offsite.due(), "not every ten minutes"
    clock[0] += 45 * 60
    assert offsite.due(), "an hour later, not a day"


def test_what_was_sent_before_the_trouble_is_in_the_manifest(offsite_lib, monkeypatch):
    cfg, offsite, _clock = offsite_lib
    real_put = offsite_mod.FolderTarget.put_file
    sent = []

    def one_then_gone(self, name, source, on_bytes=None, stop=None):
        if name.startswith("files/") and sent:
            raise OSError("the disk went away")
        if name.startswith("files/"):
            sent.append(name)
        return real_put(self, name, source, on_bytes=on_bytes, stop=stop)

    monkeypatch.setattr(offsite_mod.FolderTarget, "put_file", one_then_gone)
    monkeypatch.setattr(offsite_mod.FolderTarget, "gone",
                        lambda self: "the disk went away" if sent else None)
    finals = []
    real_manifest = Offsite._manifest

    def recording(self, conn, target, key, key_id, final=False):
        if final:
            finals.append(True)
        return real_manifest(self, conn, target, key, key_id, final=final)

    monkeypatch.setattr(Offsite, "_manifest", recording)
    offsite.start()
    offsite.join(60)
    assert len(sent) == 1
    assert finals, "the file sent is named in a manifest"
    assert (Path(cfg.offsite_folder) / "ninaivu-offsite" / offsite_mod.MANIFEST).exists()


# -- 6. an archive worker lets go only of its own claim ------------------------

def test_an_archive_worker_never_releases_another_workers_claim(tmp_path, monkeypatch):
    src = tmp_path / "s"
    src.mkdir()
    job = archive_scanner.ArchiveJob([str(src)], str(tmp_path / "d"))
    monkeypatch.setattr(archive_scanner.db, "find_verified_duplicate",
                        lambda *a, **k: None)
    claimed, release = threading.Event(), threading.Event()

    def worker():
        assert job._claim_or_wait("abc", ("verified",), "/a.jpg") is None
        claimed.set()
        release.wait(5)
        job._release_inflight("abc")

    other = threading.Thread(target=worker)
    other.start()
    assert claimed.wait(5)
    waiting_on = job._inflight["abc"]
    job._release_inflight("abc")                        # not ours: kept
    assert job._inflight.get("abc") is waiting_on and not waiting_on.is_set()
    release.set()
    other.join(5)
    assert "abc" not in job._inflight and waiting_on.is_set()


# -- 7. the archive's watcher follows its own job ------------------------------

def test_a_finished_jobs_watcher_leaves_the_next_jobs_claim_alone(monkeypatch):
    calls = []
    scanner = SimpleNamespace(defer=lambda r: calls.append(("defer", r)),
                              resume=lambda r=None: calls.append(("resume", r)))
    monkeypatch.setattr(archive_api, "_archive_idle_state", lambda: None)
    first_ends, second_ends = threading.Event(), threading.Event()
    first = threading.Thread(target=first_ends.wait, args=(10,))
    second = threading.Thread(target=second_ends.wait, args=(10,))
    first.start()
    second.start()
    try:
        monkeypatch.setattr(archive_scanner, "_job", SimpleNamespace(thread=first))
        archive_api._yield_the_disk(scanner, "a consolidation is running")
        # The next consolidation starts the moment the first one ends.
        monkeypatch.setattr(archive_scanner, "_job", SimpleNamespace(thread=second))
        first_ends.set()
        first.join(5)
        archive_api._yield_the_disk(scanner, "a consolidation is running")
        time.sleep(0.8)                                 # the first watcher has looked
        assert ("resume", "a consolidation is running") not in calls
    finally:
        second_ends.set()
        second.join(5)
    assert _wait_for(lambda: ("resume", "a consolidation is running") in calls)
    time.sleep(0.5)
    assert calls.count(("resume", "a consolidation is running")) == 1


# -- 8. a backup bundle that keeps failing ---------------------------------------

def test_a_failing_bundle_waits_longer_each_time_and_success_resets(tmp_path, monkeypatch):
    cfg = SimpleNamespace(state_dir=str(tmp_path / "state"), backup_dir=str(tmp_path / "b"),
                          backup_every_hours=24, backup_keep=3)
    Path(cfg.state_dir).mkdir()
    keeper = backup.BackupKeeper(cfg)
    monkeypatch.setattr(backup, "check_folder", lambda folder, cfg: None)
    monkeypatch.setattr(backup, "snapshot", lambda *a, **k: None)
    assert keeper.due()
    keeper.run()
    assert not keeper.due(), "not again a quarter of an hour later"
    assert keeper._retry_wait == backup.RETRY_FIRST
    keeper.run()
    assert keeper._retry_wait == 2 * backup.RETRY_FIRST
    for _ in range(10):
        keeper.run()
    assert keeper._retry_wait == backup.RETRY_MOST
    made = tmp_path / "b" / "bundle.tar"
    made.parent.mkdir(exist_ok=True)
    made.write_bytes(b"x")
    monkeypatch.setattr(backup, "snapshot", lambda *a, **k: made)
    monkeypatch.setattr(keeper, "_verify", lambda archive: {"ok": True})
    monkeypatch.setattr(backup, "prune", lambda folder, keep: [])
    keeper.run()
    assert keeper._retry_wait == 0 and keeper._retry_at == 0


# -- 9. locks for location-free copies -------------------------------------------

def test_the_copies_locks_go_when_nobody_holds_them(tmp_path):
    copies = LocationFreeCopies(tmp_path)
    lock = copies._lock_for("a")
    assert copies._lock_for("a") is lock                # shared while in use
    del lock
    gc.collect()
    assert len(copies._locks) == 0


# -- 10. a check of the second copy steps aside, and the copy goes at once -------

def test_a_check_that_stepped_aside_wakes_the_keeper(tmp_path, monkeypatch):
    cfg = SimpleNamespace(mirror_dir=str(tmp_path / "copy"), state_dir=str(tmp_path))
    mirror = mirror_mod.Mirror(cfg, lambda: sqlite3.connect(":memory:"))
    monkeypatch.setattr(mirror, "_db", lambda: object())

    def due_now(conn, target):
        raise mirror_mod._Yield()

    monkeypatch.setattr(mirror, "_know_the_disk", due_now)
    mirror._verify()
    assert mirror._look_now.is_set()


def test_the_keeper_starts_the_copy_when_woken(tmp_path, monkeypatch):
    cfg = SimpleNamespace(mirror_dir=str(tmp_path / "copy"), state_dir=str(tmp_path))
    mirror = mirror_mod.Mirror(cfg, lambda: sqlite3.connect(":memory:"))
    started = threading.Event()
    monkeypatch.setattr(mirror, "due", lambda: True)
    monkeypatch.setattr(mirror, "start", lambda: started.set())
    mirror.keep()
    try:
        mirror._look_now.set()
        assert started.wait(5), "the copy waited for the keeper's ten minutes"
    finally:
        mirror.stop(join=True)
        mirror._keeper.join(5)
    assert not mirror._keeper.is_alive()


# -- 12. start-up: approvals off the thread that opens the ports -----------------

def test_approvals_are_applied_on_the_boot_thread(scanned, monkeypatch):
    from ninaivu import build_services

    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    try:
        # And every check of this library hands over to repair.
        assert admin_api._AFTER_CHECK.get(str(cfg.db_path)) == services.repairer.after_check
        where = []
        monkeypatch.setattr(services.cloud, "apply_approvals",
                            lambda: where.append(threading.current_thread().name))
        services.start()
        services.boot_thread.join(timeout=60)
        assert where == ["ninaivu-boot"]
    finally:
        services.stop(timeout=5.0)
        admin_api.after_every_check(cfg.db_path, None)


# -- 13. a temporary that will not go does not cost every run a whole walk -------

def test_a_temporary_that_cannot_be_removed_is_remembered_not_walked_for(
        tmp_path, monkeypatch):
    from ninaivu import archive
    from ninaivu.archive import database as adb

    archive.configure(tmp_path)
    adb.close_db()
    adb.init_db()
    try:
        dest = tmp_path / "archive"
        folder = dest / "1998" / "01" / "03"
        folder.mkdir(parents=True)
        stuck = folder / f"{archive_scanner.PARTIAL_PREFIX}9-9-9.tmp"
        stuck.write_bytes(b"x")
        locked = {os.path.abspath(stuck)}
        real_remove = archive_scanner.remove_own

        def refusing(path):
            if os.path.abspath(path) in locked:
                raise PermissionError(13, "Operation not permitted", path)
            return real_remove(path)

        monkeypatch.setattr(archive_scanner, "remove_own", refusing)
        walked = []
        real_walk = os.walk
        monkeypatch.setattr(os, "walk", lambda top, *a, **k: (walked.append(top),
                                                              real_walk(top, *a, **k))[1])

        def a_run():
            job = archive_scanner.ArchiveJob([str(tmp_path / "src")], str(dest))
            job.job_id = adb.create_job([], job.destination, "YYYY/MM/DD")
            return job.sweep_partials()

        assert a_run() == 0                              # the one whole walk
        assert len(walked) == 1 and stuck.exists()
        assert a_run() == 0                              # tried by name, no walk
        assert len(walked) == 1, "one stuck file made every run walk the archive"
        locked.clear()                                   # whatever held it let go
        assert a_run() == 1
        assert not stuck.exists() and len(walked) == 1
    finally:
        adb.close_db()
