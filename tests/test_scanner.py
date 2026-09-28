"""Scanner behaviour: dates, derivatives, incremental runs, duplicates."""

import logging
import os
import time
from pathlib import Path

import pytest

from PIL import Image

from ninaivu.storage import db
from ninaivu.media import media
from ninaivu.media import scanner as scanner_mod
from ninaivu.media.scanner import (
    ScanProgress, Scanner, date_from_path, walk_media)


def test_date_from_path_variants():
    assert date_from_path("2023/05/12/a.jpg")[1] == "2023-05-12"
    assert date_from_path("2023/05/a.jpg")[1] == "2023-05-01"
    assert date_from_path("2023/a.jpg")[1] == "2023-01-01"
    assert date_from_path("holiday/IMG_20220714_101500.jpg")[1] == "2022-07-14"
    assert date_from_path("holiday/2021-12-25 dinner.jpg")[1] == "2021-12-25"
    assert date_from_path("random/file.jpg")[1] == ""


def test_new_media_defaults_to_family_visibility(scanned):
    cfg, conn, _ = scanned
    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    assert rows and all(r["visibility"] == 1 for r in rows)
    assert all(r["vis_source"] == "default" for r in rows)


def test_walk_skips_non_media_and_hidden(cfg, library):
    found = {rel for rel, _ in walk_media(library, cfg)}
    assert "notes.txt" not in found
    assert "2023/05/12/shot0.jpg" in found
    assert all(not part.startswith(".") for rel in found for part in rel.split("/"))


def test_scan_indexes_everything(scanned):
    cfg, conn, scanner = scanned
    rows, total = db.query_assets(conn, cfg.active_root, limit=100)
    # 6 dated + 1 duplicate + 3 private + 4 shared + 1 loose
    assert total == 15
    assert scanner.progress.status == "done"
    assert scanner.progress.errors == 0
    assert all(row["thumb"] for row in rows)
    assert all(row["blurhash"] for row in rows)


def test_exif_date_beats_path_date(scanned):
    cfg, conn, _ = scanned
    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    shot = next(r for r in rows if r["filename"] == "shot0.jpg")
    assert shot["date_key"] == "2023-05-12"
    assert shot["date_source"] == "exif"
    assert shot["camera"] == "Acme TestCam"


def test_undated_file_falls_back_to_mtime(scanned):
    cfg, conn, _ = scanned
    # Windows birthtime may precede the fixture's write time by milliseconds.
    # Make mtime unambiguously oldest instead of depending on filesystem timing.
    import os
    target = Path(cfg.active_root) / 'misc' / 'plain.png'
    modified = 946684800  # 2000-01-01 UTC, before this synthetic file was created.
    os.utime(target, (modified, modified))
    Scanner(cfg)._run(Path(cfg.active_root), full=False)
    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    plain = next(r for r in rows if r["filename"] == "plain.png")
    assert plain["date_source"] == "mtime"
    assert plain['captured_at'] == modified


def test_thumbnails_written_at_every_size(scanned):
    cfg, conn, _ = scanned
    rows, _ = db.query_assets(conn, cfg.active_root, limit=1)
    base = rows[0]["thumb"]
    for size in cfg.thumb_sizes:
        assert (cfg.thumbs_dir / media.thumb_file(base, size, cfg.thumb_format)).is_file()


def test_rescan_is_incremental(scanned):
    cfg, conn, _ = scanned
    scanner = Scanner(cfg)
    scanner._run(Path(cfg.active_root), full=False)
    assert scanner.progress.added == 0
    assert scanner.progress.updated == 0
    assert scanner.progress.processed == 0


def test_changed_file_is_reprocessed(scanned):
    cfg, conn, _ = scanned
    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    existing = next(r for r in rows if r["filename"] == "shot1.jpg")
    target = Path(cfg.active_root) / existing["rel_path"]
    assert target.is_file()
    Image.new("RGB", (900, 300), (255, 0, 0)).save(target, "JPEG")

    scanner = Scanner(cfg)
    scanner._run(Path(cfg.active_root), full=False)
    assert scanner.progress.updated == 1

    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    changed = next(r for r in rows if r["filename"] == "shot1.jpg")
    assert changed["width"] == 900


def test_each_scan_asks_the_filesystem_again_whether_a_folder_is_hidden(scanned):
    """On Windows "hidden" is an attribute, and the answer is memoised.

    Memoised for the length of a scan, which is what makes a library of forty
    thousand folders affordable — but no longer, or a folder the household
    un-hid in Explorer would stay out of the library until the server was
    restarted.
    """
    cfg, _, _ = scanned
    for n in range(200):
        scanner_mod._dir_is_hidden(f"Z:/not-a-folder/{n}")
    assert scanner_mod._dir_is_hidden.cache_info().currsize >= 200

    Scanner(cfg)._run(Path(cfg.active_root), full=False)

    # Only what this scan itself looked at, and the fixture has a dozen folders.
    assert scanner_mod._dir_is_hidden.cache_info().currsize < 200


def test_not_re_checking_changed_files_leaves_indexed_ones_alone(scanned):
    """``rescan_on_change`` off means *less* work, not a full re-index.

    Read the other way round, switching it off made every scan rebuild every
    thumbnail in the library — the opposite of what somebody turning it off is
    asking for.
    """
    cfg, conn, _ = scanned
    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    existing = next(r for r in rows if r["filename"] == "shot1.jpg")
    Image.new("RGB", (900, 300), (255, 0, 0)).save(
        Path(cfg.active_root) / existing["rel_path"], "JPEG")

    cfg.rescan_on_change = False
    scanner = Scanner(cfg)
    scanner._run(Path(cfg.active_root), full=False)

    assert scanner.progress.processed == 0
    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    after = next(r for r in rows if r["filename"] == "shot1.jpg")
    assert after["width"] == existing["width"]


def test_a_full_scan_still_re_reads_everything_with_re_checking_off(scanned):
    cfg, _, _ = scanned
    cfg.rescan_on_change = False
    scanner = Scanner(cfg)
    scanner._run(Path(cfg.active_root), full=True)
    assert scanner.progress.processed == 15


def test_the_bar_follows_the_items_once_the_walk_is_done():
    """Tagging, naming, reading and faces all measure themselves in items.

    By the time they run, every file has been walked and processed — so a bar
    drawn from the file counts sits at 99% for the whole of the longest part of
    a first scan, which looks exactly like a scan that has hung.
    """
    progress = ScanProgress(root="/lib", status="indexing", started_at=1.0,
                            total=100, processed=100)
    assert progress.snapshot()["percent"] == 99

    progress.update(status="reading", tag_total=200, tagged=50)
    assert progress.snapshot()["percent"] == 25

    progress.update(status="faces", tag_total=200, tagged=200)
    assert progress.snapshot()["percent"] == 99      # 100 is for a finished scan

    progress.update(status="done", ended_at=2.0)
    assert progress.snapshot()["percent"] == 100


def test_deleted_file_is_removed_with_its_thumbs(scanned):
    cfg, conn, _ = scanned
    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    victim = next(r for r in rows if r["filename"] == "shot2.jpg")
    thumb = cfg.thumbs_dir / media.thumb_file(victim["thumb"], cfg.thumb_sizes[0],
                                              cfg.thumb_format)
    assert thumb.is_file()

    (Path(cfg.active_root) / victim["rel_path"]).unlink()
    Scanner(cfg)._run(Path(cfg.active_root), full=False)

    _, total = db.query_assets(conn, cfg.active_root, limit=100)
    assert total == 14
    assert not thumb.exists()


def test_duplicates_are_grouped(scanned):
    cfg, conn, _ = scanned
    groups = db.duplicate_groups(conn, cfg.active_root)
    names = {item["filename"] for group in groups for item in group["items"]}
    assert {"shot0.jpg", "shot0_copy.jpg"} <= names


def test_stop_event_aborts(cfg, library):
    scanner = Scanner(cfg)
    scanner._stop.set()
    scanner._run(Path(cfg.active_root), full=True)
    assert scanner.progress.status == "idle"


# ---------------------------------------------------------------------------
# Standing down for the archive
# ---------------------------------------------------------------------------
#
# Indexing and consolidating both walk the disk, hash what they find and — once
# the AI passes start — use every core. The archive wins, and stands the indexer
# down for the length of a run. Both of these are ways that had actually failed
# on a real library: a consolidation copying 700 GB into the library armed the
# watcher with its own writes, and the indexer came back one second after being
# stood down, then ran for the rest of the job with nothing left to stop it.


def test_a_finished_scan_leaves_one_line_saying_what_it_did(caplog, scanned):
    """A scan used to leave nothing readable behind at all.

    The only record was a database row, so a library that was not finishing —
    because every scan was being cut short — looked exactly like one that was.
    """
    cfg, _, _ = scanned
    with caplog.at_level(logging.INFO, logger="ninaivu.media.scanner"):
        Scanner(cfg)._run(Path(cfg.active_root), full=False)

    summary = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("scan of ")]
    assert len(summary) == 1, summary
    assert "done" in summary[0] and "files indexed" in summary[0]


def test_the_log_says_when_a_change_was_queued_instead_of_indexed(caplog, cfg):
    """The line that would have caught this: who had the disk, and why."""
    scanner = Scanner(cfg)
    scanner.defer("a consolidation is running")

    with caplog.at_level(logging.INFO, logger="ninaivu.media.scanner"):
        scanner._rescan_quiet(Path(cfg.active_root))

    assert any("queued rather than started" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_the_same_change_queued_again_is_not_said_again(caplog, cfg):
    """A consolidation writes into the library all day: this line every few
    seconds was 175 lines of one evening's log."""
    scanner = Scanner(cfg)
    scanner.defer("a consolidation is running")

    with caplog.at_level(logging.INFO, logger="ninaivu.media.scanner"):
        for _ in range(5):
            scanner._rescan_quiet(Path(cfg.active_root))

    said = [r for r in caplog.records if "queued rather than started" in r.getMessage()
            and r.levelno >= logging.INFO]
    assert len(said) == 1, [r.getMessage() for r in said]


def test_standing_down_and_coming_back_are_both_logged(caplog, cfg):
    scanner = Scanner(cfg)
    with caplog.at_level(logging.INFO, logger="ninaivu.media.scanner"):
        scanner.defer("a consolidation is running")
        scanner._rescan_quiet(Path(cfg.active_root))
        scanner.resume()
    scanner.stop(join=True)

    logged = " | ".join(r.getMessage() for r in caplog.records)
    assert "stood down — a consolidation is running" in logged, logged
    assert "indexing resumed" in logged, logged


def test_the_watcher_does_not_restart_indexing_during_a_consolidation(cfg):
    """It fires on the archive's own writes into the library."""
    scanner = Scanner(cfg)
    scanner.defer("a consolidation is running")

    scanner._rescan_quiet(Path(cfg.active_root))

    assert not scanner.running, "the archive has the disk"
    assert scanner.deferred == "a consolidation is running"


def test_a_change_noticed_while_deferred_is_indexed_afterwards(cfg):
    """Standing down must not mean forgetting: the files did arrive."""
    scanner = Scanner(cfg)
    scanner.defer("a consolidation is running")
    scanner._rescan_quiet(Path(cfg.active_root))

    assert scanner.resume(), "the queued scan runs once the disk is free"
    scanner.stop(join=True)


def test_stopping_the_watcher_cancels_a_quiet_period_already_running(cfg):
    """A timer that outlives the watcher starts the scan the stop prevented.

    This is the exact leak: ``defer`` stops the watcher, but the timer armed a
    moment earlier was still counting, and two seconds later it started a full
    scan by a path that never looked at the deferral.
    """
    cfg.watch_debounce = 30.0
    scanner = Scanner(cfg)
    scanner._schedule_rescan(Path(cfg.active_root))
    assert scanner._watch_timers, "a quiet period should be running"
    waiting = next(iter(scanner._watch_timers.values()))

    scanner._stop_watch()

    assert not scanner._watch_timers
    assert not waiting.is_alive() or waiting.finished.is_set()


def test_the_quiet_period_restarts_rather_than_stacking_up(cfg):
    """A folder being copied into fires thousands of events, not one."""
    cfg.watch_debounce = 30.0
    scanner = Scanner(cfg)
    root = Path(cfg.active_root)
    scanner._schedule_rescan(root)
    first = next(iter(scanner._watch_timers.values()))
    scanner._schedule_rescan(root)

    assert len(scanner._watch_timers) == 1
    assert first.finished.is_set(), "the earlier timer must be cancelled"
    scanner._stop_watch()


def test_a_motion_photo_marker_alone_does_not_set_is_live(scanned):
    """A Google Motion Photo mixes its video inside the JPEG itself.

    ``media.detect_motion_photo()`` can tell one is there, but nothing
    extracts the embedded clip, so there is no companion file to serve. This
    used to set ``is_live`` anyway, which showed a LIVE badge that failed
    silently on tap (see the comment above the Live Photo detection block,
    and ``ninaivu/api/api.py``'s ``live_src``). An asset carrying the marker
    with no paired .mov/.mp4 should scan in as an ordinary photo.
    """
    cfg, conn, _ = scanned
    target = Path(cfg.active_root) / "motion.jpg"
    Image.new("RGB", (300, 200), (10, 80, 200)).save(target, "JPEG")
    with open(target, "ab") as handle:
        handle.write(b"GCamera:MotionPhoto")
    assert media.detect_motion_photo(target), "fixture should trip the marker check"

    Scanner(cfg)._run(Path(cfg.active_root), full=False)

    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    motion = next(r for r in rows if r["filename"] == "motion.jpg")
    assert not motion["is_live"]
    assert not motion["live_video_path"]


def test_a_big_photograph_is_indexed_at_its_real_size(scanned):
    """Decoded at a quarter, recorded whole.

    The width and height feed the low-resolution flag and the viewer's layout,
    so recording the decoded size would call every camera photograph small.
    """
    cfg, conn, _ = scanned
    target = Path(cfg.active_root) / "camera" / "IMG_9000.jpg"
    target.parent.mkdir()
    Image.new("RGB", (4000, 3000), (120, 90, 60)).save(target, "JPEG")

    Scanner(cfg)._run(Path(cfg.active_root), full=False)

    rows, _ = db.query_assets(conn, cfg.active_root, limit=100)
    photo = next(r for r in rows if r["filename"] == "IMG_9000.jpg")
    assert (photo["width"], photo["height"]) == (4000, 3000)
    assert photo["thumb"]
    assert "lowres" not in (photo["quality"] or "")


# --- more than one job holding the indexer --------------------------------

def test_the_indexer_waits_for_every_holder_to_let_go(cfg):
    """One job finishing must not hand the disk back while another has it."""
    scanner = Scanner(cfg)
    scanner.defer("a consolidation is running")
    scanner.defer("approving a family upload")
    scanner._rescan_quiet(Path(cfg.active_root))

    assert scanner.resume("approving a family upload") is False
    assert not scanner.running
    assert scanner.deferred == "a consolidation is running"

    assert scanner.resume("a consolidation is running") is True
    scanner.stop(join=True)
    assert scanner.deferred is None


def test_asking_twice_for_the_same_reason_is_one_claim(cfg):
    scanner = Scanner(cfg)
    scanner.defer("a consolidation is running")
    scanner.defer("a consolidation is running")
    scanner.resume("a consolidation is running")
    assert scanner.deferred is None


def test_held_lets_go_even_when_the_job_fails(cfg):
    scanner = Scanner(cfg)
    with pytest.raises(RuntimeError):
        with scanner.held("checking the library's storage"):
            assert scanner.deferred == "checking the library's storage"
            raise RuntimeError("the job broke")
    assert scanner.deferred is None


# --- the watcher and reads ------------------------------------------------

class _Event:
    def __init__(self, kind, path, dest=""):
        self.event_type, self.src_path, self.dest_path = kind, str(path), str(dest)
        self.is_directory = False


def test_reading_a_photograph_is_not_a_change(tmp_path):
    """Windows reports a last-access update as "modified"."""
    from ninaivu.media.scanner import _worth_a_rescan

    photo = tmp_path / "old.jpg"
    photo.write_bytes(b"x")
    long_ago = time.time() - 30 * 86400
    os.utime(photo, (time.time(), long_ago))
    assert not _worth_a_rescan(_Event("modified", photo))
    assert not _worth_a_rescan(_Event("closed_no_write", photo))


def test_editing_adding_and_removing_are_changes(tmp_path):
    from ninaivu.media.scanner import _worth_a_rescan

    photo = tmp_path / "new.jpg"
    photo.write_bytes(b"x")
    assert _worth_a_rescan(_Event("modified", photo))
    assert _worth_a_rescan(_Event("created", photo))
    assert _worth_a_rescan(_Event("deleted", tmp_path / "gone.jpg"))
    assert _worth_a_rescan(_Event("moved", photo, tmp_path / "moved.jpg"))


def test_a_change_seen_mid_scan_is_asked_about_again(cfg, monkeypatch):
    """Dropped, it waited for some unrelated change to be noticed."""
    scanner = Scanner(cfg)
    armed = []
    monkeypatch.setattr(type(scanner), "running", property(lambda self: True))
    monkeypatch.setattr(scanner, "_schedule_rescan",
                        lambda root, delay=None: armed.append(delay))
    scanner._rescan_quiet(Path(cfg.active_root))
    assert armed and armed[0] > 0


# ---------------------------------------------------------------------------
# Watching every library, and one scan at a time
# ---------------------------------------------------------------------------


def two_libraries(cfg, tmp_path):
    second = tmp_path / "second-drive"
    second.mkdir()
    cfg.add_library(str(second))
    cfg.watch = True
    return list(cfg.roots)


def test_scanning_one_library_keeps_every_library_watched(cfg, tmp_path):
    """Scanning one folder used to stop the watchers on all the others."""
    first, second = two_libraries(cfg, tmp_path)
    scanner = Scanner(cfg)
    try:
        scanner.start(second)
        assert set(scanner._observers) == {first, second}
    finally:
        scanner.stop(join=True)


def test_a_folder_is_watched_once_under_the_library_spelling(cfg, tmp_path):
    """A change is rescanned under the path its watcher was given, and rows
    are keyed by that path — so a second spelling must not get a watcher."""
    first, second = two_libraries(cfg, tmp_path)
    scanner = Scanner(cfg)
    try:
        scanner.watch([Path(first) / "2023" / ".."])
        assert set(scanner._observers) == {first, second}
    finally:
        scanner.stop()


def test_watching_is_idempotent(cfg, tmp_path):
    two_libraries(cfg, tmp_path)
    scanner = Scanner(cfg)
    try:
        scanner.watch()
        observers = dict(scanner._observers)
        scanner.watch()
        assert scanner._observers == observers, "a second observer per folder"
    finally:
        scanner.stop()


def test_a_scan_asked_for_while_the_last_is_stopping_waits_for_it(cfg, monkeypatch):
    """Clearing the stop the old scan had not seen yet brought it back to life.

    A batch of analysis or a clip being decoded can outlast the wait, and the
    old scan then carried on beside the new one: two scans writing one index.
    """
    import threading

    monkeypatch.setattr(scanner_mod, "STOP_WAIT_SECONDS", 0.05)
    release = threading.Event()
    runs = []

    def busy(self, roots, full):
        runs.append(threading.current_thread())
        if len(runs) == 1:
            release.wait(5)            # deaf to the stop, like a long batch

    monkeypatch.setattr(Scanner, "_run_each", busy)
    scanner = Scanner(cfg)
    scanner.start()
    first = scanner._thread

    scanner.start()                    # Rescan, pressed mid-batch

    assert scanner._thread is first, "a second scan started beside the first"
    assert scanner._stop.is_set(), "the first scan's stop was taken back"
    assert len(runs) == 1

    release.set()
    first.join(5)
    deadline = time.time() + 5
    while len(runs) < 2 and time.time() < deadline:
        time.sleep(0.01)
    scanner.stop(join=True)
    assert len(runs) == 2, "the scan that was asked for never ran"
    assert runs[1] is not first


def test_stopping_drops_a_scan_queued_behind_one_still_stopping(cfg, monkeypatch):
    import threading

    monkeypatch.setattr(scanner_mod, "STOP_WAIT_SECONDS", 0.05)
    release = threading.Event()
    runs = []

    def busy(self, roots, full):
        runs.append(1)
        release.wait(5)

    monkeypatch.setattr(Scanner, "_run_each", busy)
    scanner = Scanner(cfg)
    scanner.start()
    first = scanner._thread
    scanner.start()
    scanner.stop()                     # the admin presses Stop

    release.set()
    first.join(5)
    time.sleep(0.1)
    assert len(runs) == 1


def test_a_file_that_cannot_be_read_is_named_in_the_log(caplog, scanned):
    """It went to ``print``: a scan said "1 error" and nothing said which."""
    cfg, _, _ = scanned
    (Path(cfg.active_root) / "broken.jpg").write_bytes(b"not a photograph" * 10)
    with caplog.at_level(logging.WARNING, logger="ninaivu.media.scanner"):
        scanner = Scanner(cfg)
        scanner._run(Path(cfg.active_root), full=False)

    assert scanner.progress.status == "done", scanner.progress.message
    assert scanner.progress.errors == 1
    named = [r.getMessage() for r in caplog.records if "broken.jpg" in r.getMessage()]
    assert named, [r.getMessage() for r in caplog.records]


def test_a_library_of_damaged_files_is_counted_not_listed(caplog, scanned, monkeypatch):
    cfg, _, _ = scanned
    monkeypatch.setattr(scanner_mod, "FAILED_FILES_NAMED", 2)
    for n in range(5):
        (Path(cfg.active_root) / f"broken{n}.jpg").write_bytes(b"x" * 200)
    with caplog.at_level(logging.WARNING, logger="ninaivu.media.scanner"):
        Scanner(cfg)._run(Path(cfg.active_root), full=False)

    messages = [r.getMessage() for r in caplog.records]
    assert sum("could not fully read" in m for m in messages) == 2, messages
    assert any("3 more files" in m for m in messages), messages
