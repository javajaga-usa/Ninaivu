"""Starting Ninaivu does not always mean walking the whole library.

A restart is the case worth skipping: the watcher was live until the process
stopped, a scan finished shortly before that, and the seconds in between are not
enough for the library to have changed underneath us. Anything longer is exactly
when a walk earns its keep.
"""

import time
from pathlib import Path

import pytest

from ninaivu import Services
from ninaivu.storage import db


@pytest.fixture()
def services(cfg):
    # The shared fixture turns the watcher off; every case here is about what
    # the watcher having been live does or does not let start-up assume, so
    # this file turns it back on and the one case that needs it off says so.
    cfg.watch = True
    return Services(cfg)


def finished(conn, root, when, status="done"):
    run = db.start_scan_run(conn, str(root))
    conn.execute("UPDATE scan_runs SET ended_at=?, status=? WHERE id=?",
                 (when, status, run))
    conn.commit()
    return run


def test_a_library_never_scanned_is_scanned(services, cfg):
    assert services._scan_worth_it(cfg.active_root) is True


def test_a_scan_seconds_ago_is_not_repeated(services, cfg):
    conn = db.connect(cfg.db_path)
    finished(conn, cfg.active_root, time.time() - 5)
    assert services._scan_worth_it(cfg.active_root) is False


def test_a_scan_yesterday_is_repeated(services, cfg):
    conn = db.connect(cfg.db_path)
    finished(conn, cfg.active_root, time.time() - 86400)
    assert services._scan_worth_it(cfg.active_root) is True


def test_a_scan_that_never_finished_does_not_count(services, cfg):
    conn = db.connect(cfg.db_path)
    db.start_scan_run(conn, str(cfg.active_root))          # no ended_at
    assert services._scan_worth_it(cfg.active_root) is True


def test_a_scan_stopped_by_a_restart_is_picked_up(services, cfg):
    """Stopping the server closes the run it interrupts, as `idle`. That is
    not a finished scan, and a restart seconds later must carry on with it."""
    conn = db.connect(cfg.db_path)
    finished(conn, cfg.active_root, time.time() - 5, status="idle")
    assert services._scan_worth_it(cfg.active_root) is True


def test_a_scan_that_failed_is_picked_up(services, cfg):
    conn = db.connect(cfg.db_path)
    finished(conn, cfg.active_root, time.time() - 5, status="error")
    assert services._scan_worth_it(cfg.active_root) is True


def test_an_earlier_finished_scan_does_not_hide_a_later_stopped_one(services, cfg):
    conn = db.connect(cfg.db_path)
    finished(conn, cfg.active_root, time.time() - 60)
    run = db.start_scan_run(conn, str(cfg.active_root))
    db.finish_scan_run(conn, run, status="idle", processed=10, total=100)
    # The scan a minute ago did finish, and a minute is inside the window —
    # but a Rescan started after it was cut short, so start-up looks again.
    assert services._scan_worth_it(cfg.active_root) is True


def test_a_real_scan_stopped_part_way_is_picked_up(services, cfg, scanned):
    """The `idle` row comes from the scanner itself, not from a test helper."""
    _, _, scanner = scanned
    scanner.progress.update(status="indexing")
    run = db.start_scan_run(db.connect(cfg.db_path), str(cfg.active_root))
    scanner._run_id = run
    scanner._finish("idle", "Cancelled")
    assert services._scan_worth_it(cfg.active_root) is True


def test_without_the_watcher_it_always_scans(services, cfg):
    """Nothing was watching while the app was down, so nothing can be assumed
    about what changed."""
    conn = db.connect(cfg.db_path)
    finished(conn, cfg.active_root, time.time() - 5)
    services.cfg.watch = False
    assert services._scan_worth_it(cfg.active_root) is True


def test_zero_means_always(services, cfg):
    conn = db.connect(cfg.db_path)
    finished(conn, cfg.active_root, time.time() - 5)
    services.cfg.boot_scan_after = 0
    assert services._scan_worth_it(cfg.active_root) is True


def test_a_run_records_what_it_did(cfg, scanned):
    """The row the decision reads is written by an ordinary scan, not by a
    test helper."""
    _, conn, _ = scanned
    runs = db.scan_history(conn)
    assert runs, "a completed scan recorded nothing"
    assert runs[0]["ended_at"], "the scan run was never closed"
    assert runs[0]["processed"] >= 1


# -- analysis stopped part way ---------------------------------------------------

class StoppingEngine:
    """Tags the first batch, then the server is stopped."""

    name = "fake"
    model_id = "fake/stopping"
    semantic = False

    def __init__(self, scanner):
        self.scanner = scanner

    def analyse(self, paths):
        self.scanner._stop.set()
        return [{"tags": ["beach"]} for _ in paths]


def test_a_scan_stopped_during_analysis_is_not_recorded_as_done(cfg, scanned):
    """It used to carry on past the stopped tagging to "Library up to date"."""
    _, conn, scanner = scanned
    cfg.ai_enabled = True
    cfg.clip_batch_size = 2
    scanner.ai = StoppingEngine(scanner)
    conn.execute("UPDATE assets SET ai_version=0")
    conn.commit()

    scanner._run(Path(cfg.active_root), full=False)

    assert db.scan_history(conn)[0]["status"] == "idle"
    owed = conn.execute("SELECT COUNT(*) FROM assets WHERE ai_version=0").fetchone()[0]
    assert owed > 0, "the fixture finished analysis before the stop"


def test_analysis_still_owed_is_carried_on_at_start_up(services, cfg, scanned):
    """Covers a library a stopped scan already marked done."""
    _, conn, _ = scanned
    cfg.ai_enabled = True
    finished(conn, cfg.active_root, time.time() - 5)
    conn.execute("UPDATE assets SET ai_version=0")
    conn.commit()
    assert services._scan_worth_it(cfg.active_root) is True


def test_nothing_owed_and_a_scan_moments_ago_is_still_skipped(services, cfg, scanned):
    from ninaivu.media.scanner import AI_VERSION

    _, conn, _ = scanned
    cfg.ai_enabled = True
    finished(conn, cfg.active_root, time.time() - 5)
    conn.execute("UPDATE assets SET ai_version=?", (AI_VERSION,))
    conn.commit()
    assert services._scan_worth_it(cfg.active_root) is False


def test_sound_files_are_not_analysis_owed(services, cfg, scanned):
    """A sound file's picture is a drawn tile the image model never tags. Counted
    as owed, the sound files in a library made every restart walk the whole
    drive: 3.5 minutes of an external disk, each time."""
    from ninaivu.media.scanner import AI_VERSION

    _, conn, _ = scanned
    cfg.ai_enabled = True
    finished(conn, cfg.active_root, time.time() - 5)
    conn.execute("UPDATE assets SET ai_version=?", (AI_VERSION,))
    one = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
    conn.execute("UPDATE assets SET kind='audio', thumb='aa/tile', ai_version=0 WHERE id=?", (one,))
    conn.commit()
    assert services._scan_worth_it(cfg.active_root) is False


def test_with_analysis_off_nothing_is_owed(services, cfg, scanned):
    _, conn, _ = scanned
    cfg.ai_enabled = False
    finished(conn, cfg.active_root, time.time() - 5)
    conn.execute("UPDATE assets SET ai_version=0")
    conn.commit()
    assert services._scan_worth_it(cfg.active_root) is False


# -- what the progress strip says a scan is doing ---------------------------------

def test_a_pass_with_nothing_to_do_does_not_leave_the_last_one_on_screen(cfg, scanned):
    """"Analysing 512 / 512" at 100% sat there for the hours the passes after
    tagging took, because a pass that returns early changed nothing."""

    _, conn, scanner = scanned
    cfg.ai_enabled = False                 # so every pass after indexing is a no-op
    cfg.faces_enabled = False
    scanner.progress.update(status="tagging", tagged=512, tag_total=512,
                            message="Analysing 512 items with clip…")

    scanner._run(Path(cfg.active_root), full=False)

    snapshot = scanner.progress.snapshot()
    assert snapshot["status"] == "done", snapshot
    assert "Analysing" not in snapshot["message"]


def test_describing_videos_is_a_phase_of_its_own(cfg, scanned, monkeypatch):
    """It decodes every clip — hours on a big library — and said nothing."""
    from ninaivu.media import scanner as scanner_mod

    _, conn, scanner = scanned
    cfg.ai_enabled = True
    cfg.video_keyframes = 3
    scanner.ai = object()                   # present, but never asked to analyse
    conn.execute("INSERT INTO assets(root, rel_path, filename, folder, ext, kind, "
                 "size, mtime, date_key, date_source, duration, keyframe_version) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                 (str(cfg.active_root), "clips/a.mp4", "a.mp4", "clips", "mp4",
                  "video", 10, 0, "2024-01-01", "mtime", 12.0))
    conn.commit()
    seen = []
    monkeypatch.setattr(scanner_mod.Scanner, "_tag_one_video",
                        lambda self, *a, **k: seen.append(self.progress.snapshot()))

    scanner._tag_video_keyframes(conn, str(cfg.active_root))

    assert seen, "the pass did not run"
    assert seen[0]["status"] == "videos"
    assert "video" in seen[0]["message"].lower()
    assert seen[0]["tag_total"] == 1


# -- every library, walked or watched ---------------------------------------------

class Recorder:
    def __init__(self):
        self.started, self.watched = [], 0

    def start(self, roots, full=False):
        self.started.append(([str(r) for r in roots], full))

    def watch(self, roots=()):
        self.watched += 1


def second_library(cfg, tmp_path):
    second = tmp_path / "second-drive"
    second.mkdir()
    cfg.add_library(str(second))
    return str(second.resolve())


def test_every_library_folder_is_scanned_at_start_up(services, cfg, tmp_path):
    """Only the folder selected in the console was; a second drive waited for
    somebody to press Rescan."""
    second = second_library(cfg, tmp_path)
    services.scanner = Recorder()

    services._scan_the_libraries()

    assert services.scanner.started == [([str(cfg.active_root), second], False)]


def test_only_the_libraries_that_need_it_are_walked(services, cfg, tmp_path):
    second = second_library(cfg, tmp_path)
    finished(db.connect(cfg.db_path), cfg.active_root, time.time() - 5)
    services.scanner = Recorder()

    assert services._scan_the_libraries() == [second]


def test_a_skipped_start_up_scan_still_watches(services, cfg):
    """Skipping the walk is only safe because a watcher is live. After a quick
    restart nothing started one, so new photographs waited for Rescan."""
    finished(db.connect(cfg.db_path), cfg.active_root, time.time() - 5)
    services.scanner = Recorder()

    assert services._scan_the_libraries() == []
    assert services.scanner.started == []
    assert services.scanner.watched == 1


def test_a_rescan_at_start_up_re_reads_every_library(services, cfg, tmp_path):
    second = second_library(cfg, tmp_path)
    finished(db.connect(cfg.db_path), cfg.active_root, time.time() - 5)
    services.scanner = Recorder()

    services._scan_the_libraries(rescan=True)

    assert services.scanner.started == [([str(cfg.active_root), second], True)]


# -- a read-only drive that has stayed connected ------------------------------------
#
# A library on a drive this computer cannot write to (NTFS on a Mac) changes only
# while it is elsewhere. Each restart more than a quarter of an hour after the
# last scan walked it anyway: four minutes of a USB drive, nothing new.

@pytest.fixture()
def drive(services, cfg, tmp_path, monkeypatch):
    import os
    from ninaivu.storage import new_files

    volumes = tmp_path / "Volumes"
    root = volumes / "Red" / "MasterArchive"
    root.mkdir(parents=True)
    monkeypatch.setattr(Services, "VOLUMES", volumes)
    monkeypatch.setattr("ninaivu.sys.platform", "darwin")
    monkeypatch.setattr(new_files, "writable", lambda _root: False)
    now = time.time()
    os.utime(volumes, (now - 7200, now - 7200))            # mounted two hours ago
    conn = db.connect(cfg.db_path)
    run = db.start_scan_run(conn, str(root))
    conn.execute("UPDATE scan_runs SET started_at=?, ended_at=?, status='done' WHERE id=?",
                 (now - 3900, now - 3600, run))            # scanned an hour ago
    conn.commit()
    return volumes, str(root)


def test_a_read_only_drive_that_stayed_connected_is_not_walked_again(services, drive):
    _, root = drive
    assert services._scan_worth_it(root) is False


def test_a_drive_mounted_again_since_is_walked(services, drive):
    import os
    volumes, root = drive
    os.utime(volumes, None)                                # something (re)mounted just now
    assert services._scan_worth_it(root) is True


def test_a_drive_that_can_be_written_is_walked(services, drive, monkeypatch):
    from ninaivu.storage import new_files
    _, root = drive
    monkeypatch.setattr(new_files, "writable", lambda _root: True)
    assert services._scan_worth_it(root) is True


def test_a_folder_not_on_a_mounted_drive_is_walked(services, drive, cfg):
    conn = db.connect(cfg.db_path)
    finished(conn, cfg.active_root, time.time() - 3600)
    assert services._scan_worth_it(cfg.active_root) is True


def test_elsewhere_than_a_mac_it_is_walked(services, drive, monkeypatch):
    _, root = drive
    monkeypatch.setattr("ninaivu.sys.platform", "win32")
    assert services._scan_worth_it(root) is True


# -- after a change that stood the indexer down --------------------------------------

class Indexer:
    def __init__(self, resumes=False):
        self.resumes, self.calls = resumes, []

    def resume(self, reason):
        self.calls.append(("resume", reason))
        return self.resumes

    def start(self, roots=None, **_):
        self.calls.append(("start", roots))

    def watch(self, roots=()):
        self.calls.append(("watch",))


def test_only_the_folder_written_into_is_scanned_after_a_change(app):
    """Approving one upload walked every library folder: four minutes of a USB
    drive to index one photograph filed in ~/Pictures/Ninaivu."""
    from ninaivu.api import admin_api

    app.config["MV_CONFIG"].watch = True
    with app.app_context():
        indexer = Indexer()
        admin_api.resume_after(indexer, "approving", ["/Users/me/Pictures/Ninaivu", None])
        assert indexer.calls[-1] == ("start", ["/Users/me/Pictures/Ninaivu"])

        indexer = Indexer()
        admin_api.resume_after(indexer, "approving", [])      # it failed: nothing written
        assert indexer.calls[-1] == ("watch",)

        indexer = Indexer(resumes=True)                        # a scan it interrupted
        admin_api.resume_after(indexer, "approving", ["/x"])
        assert indexer.calls == [("resume", "approving")]
