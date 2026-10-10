"""The fourth flow pass: hand-offs that read too much, waited too long, or
forgot what they were doing.

A photograph arriving read the whole library, an approval or an import stopped
an analysis that had hours to run, a change seen mid-scan waited for all of
that analysis, a restart in the middle of analysis walked the library again,
and a few status lines and ETAs said the wrong thing while it all happened.
"""

import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from ninaivu import Services
from ninaivu.media import scanner as scanner_mod
from ninaivu.media.scanner import (
    CLAIM_IMPORT, SCOPE_MAX_FOLDERS, TAG_LEFT, ScanProgress, Scanner, _merge_scopes,
)
from ninaivu.storage import db


def _wait_for(predicate, timeout=5.0):
    ends = time.monotonic() + timeout
    while time.monotonic() < ends:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _photo(path: Path, colour=(10, 120, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 240), colour).save(path, "JPEG")
    return path


def _paths(conn, root) -> set[str]:
    return {r[0] for r in conn.execute("SELECT rel_path FROM assets WHERE root=?",
                                       (str(root),))}


# -- reading only the folders that changed ----------------------------------------

def test_the_index_can_be_asked_about_some_folders_only(scanned):
    cfg, conn, _ = scanned
    root = str(cfg.active_root)
    every = db.existing_signatures(conn, root)
    some = db.existing_signatures(conn, root, under=["shared"])
    assert set(some) == {rel for rel in every if rel.startswith("shared/")}
    # "a/" to "a0" is a range on the name, not a pattern: an underscore or a
    # folder whose name only starts the same is not inside.
    assert db.existing_signatures(conn, root, under=["2023/0"]) == {}
    assert db.live_paths(conn, root, under=["misc"]) == {"misc/plain.png"}


def test_a_scan_of_one_folder_reads_that_folder_and_nothing_else(scanned):
    cfg, conn, scanner = scanned
    root = Path(cfg.active_root)
    _photo(root / "shared/holiday/new.jpg")
    _photo(root / "misc/elsewhere.jpg", (200, 10, 10))
    (root / "shared/holiday/beach0.jpg").unlink()

    scanner._run(root, False, only=["shared/holiday"])

    paths = _paths(conn, root)
    assert "shared/holiday/new.jpg" in paths
    assert "shared/holiday/beach0.jpg" not in paths           # gone, and settled
    assert "misc/elsewhere.jpg" not in paths                  # not read: not asked
    assert "2023/05/12/shot0.jpg" in paths                    # nothing else touched
    assert scanner.progress.snapshot()["status"] == "done"


def test_a_folder_emptied_all_at_once_is_settled_by_reading_everything(scanned):
    """A few folders cannot tell an emptied folder from an unreadable one;
    the whole library folder can, by the rules every scan always used."""
    cfg, conn, scanner = scanned
    root = Path(cfg.active_root)
    for photo in (root / "shared/holiday").glob("*.jpg"):
        photo.unlink()

    scanner._run(root, False, only=["shared/holiday"])

    assert not {p for p in _paths(conn, root) if p.startswith("shared/")}


def test_a_folder_that_has_gone_is_settled_from_the_one_above(scanned):
    cfg, conn, scanner = scanned
    root = Path(cfg.active_root)
    shutil.rmtree(root / "shared/holiday")
    assert scanner._usable_scope(root, ["shared/holiday"]) == ["shared"]
    assert scanner._usable_scope(root, ["shared", "shared/holiday", "misc"]) == ["misc", "shared"]
    assert scanner._usable_scope(root, ["_deleted/x"]) is None    # nothing to read there
    assert scanner._usable_scope(root, ["../elsewhere"]) is None


def test_the_watcher_names_the_folder_that_changed(cfg, tmp_path):
    root = Path(cfg.active_root)
    scanner = Scanner(cfg)

    def event(kind, src, dest="", is_dir=False):
        return SimpleNamespace(event_type=kind, src_path=str(src), dest_path=str(dest),
                               is_directory=is_dir)

    assert scanner._changed_folders(root, event("created", root / "2024/01/02/a.jpg")) == {"2024/01/02"}
    # Moved to the bin: only the folder it left.
    assert scanner._changed_folders(
        root, event("moved", root / "misc/a.jpg", root / "_deleted/misc/a.jpg")) == {"misc"}
    # A folder dragged in is read itself; its files have no events of their own.
    assert scanner._changed_folders(
        root, event("created", root / "trip", is_dir=True)) == {"trip"}
    assert scanner._changed_folders(
        root, event("deleted", root / "trip/day1", is_dir=True)) == {"trip"}
    # The library folder itself, and what is not media, and a folder modified.
    assert scanner._changed_folders(root, event("created", root / "a.jpg")) == {None}
    assert scanner._changed_folders(root, event("created", root / "misc/notes.txt")) == set()
    assert scanner._changed_folders(
        root, event("modified", root / "misc", is_dir=True)) == set()


def test_changes_in_a_quiet_period_are_read_together(cfg, monkeypatch):
    root = Path(cfg.active_root)
    scanner = Scanner(cfg)
    asked = []
    monkeypatch.setattr(scanner, "_rescan_quiet", lambda r, folders=None: asked.append(folders))
    scanner._schedule_rescan(root, 0.2, folders={"a"})
    scanner._schedule_rescan(root, 0.05, folders={"b"})
    assert _wait_for(lambda: asked)
    assert asked == [{"a", "b"}]
    scanner._schedule_rescan(root, 0.05, folders={"c", None})
    assert _wait_for(lambda: len(asked) == 2)
    assert asked[1] is None                                  # the whole folder


def test_whole_wins_when_requests_are_merged(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    merged = _merge_scopes(([a, b], {a: frozenset({"x"}), b: frozenset({"y"})}),
                           ([a, b], {a: frozenset({"z"})}))
    assert merged == {a: frozenset({"x", "z"})}              # b was wanted whole
    many = frozenset(str(i) for i in range(SCOPE_MAX_FOLDERS + 1))
    assert _merge_scopes(([a], {a: many}), ([], {})) == {}


def test_a_scan_asked_for_some_folders_while_held_keeps_them(cfg, tmp_path):
    scanner = Scanner(cfg)
    root = Path(cfg.active_root)
    scanner.defer("a test")
    scanner.start(folders=[root / "misc"])
    scanner.start(folders=[root / "shared"])
    assert scanner._defer_pending == ([root], False)
    assert scanner._defer_scopes == {root: frozenset({"misc", "shared"})}
    asked = []
    scanner.start = lambda roots=None, full=False, **kw: asked.append((roots, full, kw))
    scanner.resume("a test")
    assert asked == [([root], False, {"scopes": {root: frozenset({"misc", "shared"})}})]


# -- an analysis carries on through a short hold ---------------------------------

def _analysing(scanner):
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    scanner._thread = thread
    scanner._current = ([Path(scanner.cfg.active_root)], False)
    return release


def test_an_approval_leaves_an_analysis_running(cfg):
    scanner = Scanner(cfg)
    release = _analysing(scanner)
    try:
        scanner._analysing = True
        assert scanner.defer("approving", analysis_may_continue=True) is False
        assert not scanner._stop.is_set()
        assert scanner._defer_pending is None
        assert scanner.deferred == "approving"
        # Reading is not analysis: that is stopped as it always was.
        scanner._analysing = False
        assert scanner.defer("another", analysis_may_continue=True) is True
        assert scanner._stop.is_set()
    finally:
        release.set()
        scanner.resume()
        scanner.stop(join=True)


def test_the_next_library_folder_waits_for_a_hold(cfg, tmp_path):
    scanner = Scanner(cfg)
    second = tmp_path / "second"
    scanner._claims.append("an import is copying into the library")
    assert scanner._held_before([second], False, {}) is True
    assert scanner._defer_pending == ([second], False)
    assert scanner.progress.snapshot()["status"] == "paused"


def test_files_seen_while_reading_are_read_before_the_analysis(scanned, monkeypatch):
    cfg, conn, scanner = scanned
    root = Path(cfg.active_root)
    passes = []
    monkeypatch.setattr(scanner, "_name_places", lambda *a: passes.append("places"))
    scanner._after_stop = ([root], False)       # the watcher queued it mid-walk
    scanner._roots_done = []
    scanner._run(root, False)
    assert passes == []
    assert scanner._stop.is_set()
    assert root in scanner._roots_done          # read through: not walked again
    assert "arrived" in scanner.progress.snapshot()["message"]
    scanner._after_stop = None


def test_an_analysis_only_scan_reads_nothing(scanned, monkeypatch):
    cfg, conn, scanner = scanned
    root = Path(cfg.active_root)
    passes = []
    monkeypatch.setattr(scanner_mod, "walk_media",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked")))
    monkeypatch.setattr(scanner, "_name_places", lambda *a: passes.append("places"))
    before = _paths(conn, root)
    scanner._run(root, False, only=[])
    assert passes == ["places"]
    assert _paths(conn, root) == before
    assert scanner.progress.snapshot()["status"] == "done"


# -- holds, stops and shutting down ------------------------------------------------

def test_a_scan_stopped_for_a_hold_says_it_is_paused(cfg):
    scanner = Scanner(cfg)
    scanner._claims.append("checking the library's storage")
    scanner._finish("idle", "Cancelled")
    snap = scanner.progress.snapshot()
    assert snap["status"] == "paused"
    assert "checking the library's storage" in snap["message"]


def test_nothing_starts_once_ninaivu_is_shutting_down(cfg):
    scanner = Scanner(cfg)
    scanner.close()
    scanner.defer("an import is copying into the library")
    scanner.start()
    assert scanner.resume("an import is copying into the library") is False
    scanner.start()
    assert not scanner.running


def test_waiting_for_a_turn_is_not_counted_in_the_finish_time():
    progress = ScanProgress(status="tagging", tag_total=1000)
    progress.update(status="tagging")
    progress.phase_started_at = time.time() - 3600 - 100
    progress.held_seconds = 3600.0               # an hour waiting for the night
    progress.update(tagged=100)
    eta = progress.snapshot()["eta"]
    assert eta is not None and eta < 1000       # 100 s for 100 items: 900 s left


def test_a_hold_is_timed_from_when_it_starts_and_ends():
    progress = ScanProgress()
    progress.update(status="tagging")
    progress.update(held="waiting for the night")
    progress._held_since -= 50
    assert progress.snapshot()["held_seconds"] >= 50
    progress.update(held="")
    assert progress.held_seconds >= 50
    progress.update(status="faces")
    assert progress.held_seconds == 0.0


# -- start-up ------------------------------------------------------------------------

def test_a_restart_during_analysis_does_not_walk_the_library_again(cfg):
    cfg.watch = True
    services = Services(cfg)
    conn = db.init_db(cfg.db_path)
    root = str(cfg.active_root)
    run = db.start_scan_run(conn, root)
    db.set_meta(conn, f"read_through:{root}", str(run))
    db.finish_scan_run(conn, run, status="idle")
    started = []
    services.scanner = SimpleNamespace(
        start=lambda roots, full=False, **kw: started.append((roots, full, kw)),
        watch=lambda roots=(): None)
    assert services._scan_the_libraries() == [root]
    assert started == [([root], False, {"scopes": {Path(root): frozenset()}})]


def test_a_restart_long_after_still_walks(cfg):
    cfg.watch = True
    services = Services(cfg)
    conn = db.init_db(cfg.db_path)
    root = str(cfg.active_root)
    run = db.start_scan_run(conn, root)
    db.set_meta(conn, f"read_through:{root}", str(run))
    conn.execute("UPDATE scan_runs SET ended_at=?, status='idle' WHERE id=?",
                 (time.time() - 86400, run))
    conn.commit()
    assert services._read_through(root) is False


def test_items_the_last_tagging_could_not_tag_are_not_owed_again(scanned):
    cfg, conn, _ = scanned
    cfg.watch = True
    services = Services(cfg)
    root = str(cfg.active_root)
    owed = conn.execute(
        "SELECT COUNT(*), MAX(id) FROM assets WHERE root=? AND thumb IS NOT NULL "
        "AND kind != 'audio' AND visibility < 2", (root,)).fetchone()
    assert services._analysis_owed(root) is True
    db.set_meta(conn, TAG_LEFT.format(root=root), f"{owed[0]}:{owed[1]}")
    assert services._analysis_owed(root) is False
    # Anything owed since — a photograph added, another left untagged — is.
    db.set_meta(conn, TAG_LEFT.format(root=root), f"{owed[0] - 1}:{owed[1]}")
    assert services._analysis_owed(root) is True


# -- imports and phone backups ------------------------------------------------------

def test_an_import_has_only_the_folders_it_filled_read(cfg, tmp_path):
    from ninaivu.storage.importer import Importer

    calls = []
    scanner = SimpleNamespace(start=lambda roots, full=False, **kw: calls.append((roots, kw)),
                              add_listener=lambda _fn: None)
    importer = Importer(cfg, lambda: db.init_db(cfg.db_path), scanner=scanner)
    root = str(cfg.active_root)
    importer._landed = {str(Path(root) / "2024/01/02")}
    importer._index_what_landed(None, root)
    assert calls == [([root], {"folders": [str(Path(root) / "2024/01/02")]})]


def test_a_stopped_import_has_what_it_copied_indexed(cfg):
    from ninaivu.storage.importer import Importer

    calls = []
    scanner = SimpleNamespace(start=lambda roots, full=False, **kw: calls.append(roots),
                              add_listener=lambda _fn: None)
    importer = Importer(cfg, lambda: db.init_db(cfg.db_path), scanner=scanner)
    importer._landed = set()
    importer._index_what_landed(None, str(cfg.active_root))
    assert calls == []                          # nothing copied, nothing to read


def test_an_import_does_not_keep_phone_backups_waiting(app):
    from ninaivu.api import admin_api

    scanner = Scanner(app.config["MV_CONFIG"])
    scanner.defer(CLAIM_IMPORT)
    assert admin_api._moving_files(scanner) is False
    scanner.defer("a consolidation is running")
    assert admin_api._moving_files(scanner) is True


def test_finishing_a_phone_backup_with_nothing_staged_leaves_the_scan_alone(app):
    from ninaivu.api import api_phone_backup
    from ninaivu.media import phone_backup

    cfg = app.config["MV_CONFIG"]
    asked = []
    scanner = SimpleNamespace(deferred=None,
                              defer=lambda *a, **k: asked.append("defer"),
                              stop=lambda *a, **k: asked.append("stop"))
    conn = db.init_db(cfg.db_path)
    phone_backup.init_schema(conn)
    with app.app_context():
        assert api_phone_backup._file(conn, cfg, scanner, 1, 1) == {"approved": 0, "failed": 0}
    assert asked == []


# -- the passes ---------------------------------------------------------------------

def test_a_clip_gone_from_under_the_video_pass_is_left_for_the_next_scan(scanned):
    cfg, conn, scanner = scanned
    row = {"id": 1, "rel_path": "gone/clip.mp4", "duration": 10.0}
    read = scanner._read_keyframes(str(cfg.active_root), row, [0.5])
    assert read.get("later") is True
    stamped = []
    scanner_db = scanner_mod.db
    real = scanner_db.update_asset
    scanner_db.update_asset = lambda *a, **k: stamped.append(k)
    try:
        scanner._tag_one_video(conn, str(cfg.active_root), row, [0.5], read=read)
    finally:
        scanner_db.update_asset = real
    assert stamped == []


def test_a_turn_the_straightening_applied_survives_a_full_rescan(scanned):
    from ninaivu.media import straighten

    cfg, conn, scanner = scanned
    root = Path(cfg.active_root)
    straighten.init_schema(conn)
    asset = conn.execute("SELECT id, rel_path FROM assets WHERE rel_path='misc/plain.png'"
                         ).fetchone()
    conn.execute("UPDATE assets SET rotation=90, rot_source='model' WHERE id=?", (asset["id"],))
    conn.execute("INSERT INTO orientation_proposals(asset_id, rotation, status) "
                 "VALUES(?, 90, 'applied')", (asset["id"],))
    conn.commit()

    scanner._run(root, full=True)

    row = conn.execute("SELECT rotation, rot_source FROM assets WHERE id=?",
                       (asset["id"],)).fetchone()
    assert (row["rotation"], row["rot_source"]) == (90, "model")
