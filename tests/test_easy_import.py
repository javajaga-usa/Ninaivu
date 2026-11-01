"""Bringing an index from another computer, made hard to get wrong.

What went wrong on a real move (Mac mini → Linux PC, 208,403 files):

* the import began at a restart and took the better part of an hour; the
  Control Panel was opened again because it seemed stuck, and that second
  start found the import already taken, opened the old, empty folder and
  indexed the library from nothing while the first went on unpacking;
* nothing in the log said how far the unpacking had got, or that it
  finished, or that it did not;
* the imported settings named the Mac's library folder (/Volumes/…), which
  Linux does not have, and it was only ever looked for under /Volumes.

Each test below is one of those, and the rest of what the logs of that day
showed: model downloads cut short reported as damaged, a stop that named
every part as stuck, and files the computer's user may not read reported as
files that cannot be read.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.cli import backup_restore as restore
from ninaivu.media import model_catalog, scanner
from ninaivu.server import runfile, starting
from ninaivu.storage import backup, full_export, library_id


# --- one start at a time ---------------------------------------------------

def test_a_second_start_during_an_import_is_turned_away(monkeypatch, tmp_path, capsys):
    import ninaivu.__main__ as entry

    state = tmp_path / "data"
    state.mkdir()
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(state))
    imported = []
    monkeypatch.setattr(full_export, "apply_pending", lambda s: imported.append(s))
    monkeypatch.setattr(starting, "read", lambda s, now=None: {
        "pid": 1, "step": "bringing in the index from the other computer: unpacking, 40%"})
    began = []
    monkeypatch.setattr(starting, "begin", lambda *a, **k: began.append(a))

    with runfile.starting_lock(state):          # the first start, importing
        assert entry.main([]) == 1

    said = capsys.readouterr().err
    assert "already starting" in said and "unpacking, 40%" in said
    # It neither took the import nor wrote over the first start's note.
    assert imported == [] and began == []


def test_the_starting_lock_is_beside_the_folder_an_import_renames(tmp_path):
    state = tmp_path / "data"
    state.mkdir()
    assert runfile.starting_lock_path(state).parent == tmp_path
    with runfile.starting_lock(state):
        with pytest.raises(runfile.AlreadyStarting):
            with runfile.starting_lock(state):
                pass
        # The folder can be swapped for the imported one while it is held.
        state.rename(tmp_path / "old")
        (tmp_path / "data").mkdir()
    with runfile.starting_lock(tmp_path / "data"):
        pass                                     # and it is let go


def test_the_server_lock_is_taken_before_the_start_lock_is_let_go(monkeypatch, tmp_path):
    import ninaivu.__main__ as entry

    order = []
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(full_export, "apply_pending", lambda s: order.append("import"))
    real = runfile.starting_lock

    class Watched:
        def __init__(self, state):
            self.inner = real(state)

        def __enter__(self):
            order.append("start lock")
            return self.inner.__enter__()

        def __exit__(self, *exc):
            order.append("start lock let go")
            return self.inner.__exit__(*exc)

    def server_lock(state):
        order.append("server lock")
        raise runfile.AlreadyRunning("already running")

    monkeypatch.setattr(runfile, "starting_lock", Watched)
    monkeypatch.setattr(runfile, "server_lock", server_lock)
    assert entry.main([]) == 1
    assert order == ["start lock", "import", "server lock", "start lock let go"]


def test_the_control_panel_waits_on_a_start_it_did_not_make(monkeypatch, tmp_path):
    from ninaivu.desktop import control

    panel = control.Controller.__new__(control.Controller)
    panel.cfg = SimpleNamespace(state_dir=tmp_path)
    panel.started = None
    panel.record = lambda: None
    panel.refresh_network_access = lambda: None
    launched = []
    monkeypatch.setattr(control.subprocess, "Popen", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(control.starting, "read", lambda state: {
        "pid": 4242, "since": time.time() - 600, "step": "bringing in the index"})
    monkeypatch.setattr(control, "_pid_alive", lambda pid: True)
    waited = []
    panel._wait_for_start = lambda since: waited.append(since) or "Ninaivu is running."

    assert panel.start() == "Ninaivu is running."
    assert waited and launched == []


def test_a_long_import_is_not_called_late(monkeypatch, tmp_path):
    from ninaivu.desktop import control

    panel = control.Controller.__new__(control.Controller)
    panel.cfg = SimpleNamespace(state_dir=tmp_path)
    panel.started = None
    panel.on_progress = None
    answers = iter([None] * 3 + [{"pid": 1}])
    panel.record = lambda: next(answers)
    panel.capture_running_settings = lambda: None
    panel.save_mode = lambda mode: None
    panel.mode = "standard"
    panel.runtime = tmp_path
    clock = iter([0, 0, control.START_CEILING_SECONDS + 10, control.START_CEILING_SECONDS + 20,
                  control.START_CEILING_SECONDS + 30])
    monkeypatch.setattr(control.starting, "read", lambda state: {
        "since": time.time(), "step": "bringing in the index from the other computer: unpacking, 80%"})
    assert panel._wait_for_start(time.time(), clock=lambda: next(clock),
                                 sleep=lambda s: None) == "Ninaivu is running."


# --- saying how far it has got ----------------------------------------------

def _state(folder: Path, label: str, roots: list[str] | None = None,
           assets: list[tuple[str, str]] = ()) -> Path:
    folder.mkdir(parents=True)
    with closing(sqlite3.connect(folder / "index.db")) as conn, conn:
        conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, root TEXT, rel_path TEXT)")
        conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO assets (root, rel_path) VALUES (?, ?)", list(assets))
    settings = {"home_name": label}
    if roots is not None:
        settings.update(roots=roots, active_root=roots[0] if roots else "")
    (folder / "config.json").write_text(json.dumps(settings))
    for i in range(30):
        thumb = folder / "thumbs" / f"{i:02x}" / f"{i}.webp"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(f"{label} {i}".encode())
    return folder


@pytest.fixture()
def move(tmp_path, monkeypatch):
    """The Mac's state, with its library at /Volumes/…, and this computer's."""
    mac_root = "/Volumes/JApple Extreme SSD/NinaivuMaster"
    rels = [f"Ninaivu Archive/2024/{i:02d}.jpg" for i in range(40)]
    mac = _state(tmp_path / "mac", "mac", [mac_root], [(mac_root, rel) for rel in rels])
    (mac / "library-ids.json").write_text(json.dumps({mac_root: "lib-1"}))
    drive = tmp_path / "media" / "jaga" / "Toshiba" / "NinaivuMaster"
    for rel in rels:
        (drive / rel).parent.mkdir(parents=True, exist_ok=True)
        (drive / rel).write_bytes(b"photo")
    here = _state(tmp_path / "here", "here", [str(drive)])
    monkeypatch.setattr(restore.Config, "load", lambda: SimpleNamespace(state_dir=here))
    monkeypatch.setattr(library_id, "_local_mount_parents",
                        lambda: [str(tmp_path / "media" / "jaga")])
    monkeypatch.setattr(library_id, "_mounts", lambda: [])
    bundle = backup.snapshot(mac, tmp_path / "out", full=True)
    return SimpleNamespace(mac=mac, here=here, drive=drive, mac_root=mac_root,
                           bundle=bundle, base=tmp_path)


def test_an_import_says_how_far_it_has_got(move, monkeypatch, capsys):
    steps = []
    monkeypatch.setattr(starting, "step", steps.append)
    full_export.ask_for_import(move.here, move.bundle)
    assert full_export.apply_pending(move.here) is True

    assert any("unpacking, 100%" in s for s in steps), steps
    log = capsys.readouterr()
    said = log.out + log.err
    assert "in one pass" in said and "is in place" in said
    assert "Please restart Ninaivu" not in said     # it is starting already


def test_a_failed_import_says_so_in_the_log(move, capsys):
    full_export.ask_for_import(move.here, move.bundle)
    move.bundle.unlink()
    assert full_export.apply_pending(move.here) is False
    assert "The index was not imported" in capsys.readouterr().err


# --- the library, on this computer --------------------------------------------

def test_a_mac_library_is_looked_for_on_this_computers_drives(move):
    found = library_id.candidates(move.mac_root)
    assert move.drive in found


def test_an_import_is_pointed_at_the_folder_chosen_here(move):
    library_id.write_marker(move.drive, "lib-1")
    full_export.ask_for_import(move.here, move.bundle)
    assert full_export.apply_pending(move.here) is True

    waiting = library_id.pending_relocations(move.here)
    assert waiting[move.mac_root]["confirmed"] == str(move.drive)
    assert waiting[move.mac_root]["by"] == "import"
    assert full_export.last_result(move.here)["library_here"] == {move.mac_root: str(move.drive)}


def test_with_no_id_the_files_in_the_folder_decide(move):
    # A copy made without its dot file, or one a start here gave an id of
    # its own: the index's files are what say it is the same library.
    library_id.write_marker(move.drive, "minted-here")
    full_export.ask_for_import(move.here, move.bundle)
    assert full_export.apply_pending(move.here) is True
    assert full_export.last_result(move.here)["library_here"] == {move.mac_root: str(move.drive)}


def test_a_folder_without_the_library_in_it_is_not_taken(move, tmp_path):
    other = tmp_path / "elsewhere" / "Pictures"
    other.mkdir(parents=True)
    (other / "a.jpg").write_bytes(b"x")
    assert library_id.adopt_for_import(move.mac, [str(other)], look=lambda root: []) == {}


def test_two_folders_with_the_id_are_left_for_a_person(move, tmp_path):
    copy = tmp_path / "media" / "jaga" / "Backup" / "NinaivuMaster"
    copy.mkdir(parents=True)
    library_id.write_marker(copy, "lib-1")
    library_id.write_marker(move.drive, "lib-1")
    # Nothing chosen here: a library and its copy, nothing to tell them apart.
    assert library_id.adopt_for_import(move.mac, []) == {}


def test_the_folder_an_import_chose_is_moved_to_at_start(move):
    library_id.write_marker(move.drive, "lib-1")
    full_export.ask_for_import(move.here, move.bundle)
    full_export.apply_pending(move.here)
    cfg = SimpleNamespace(state_dir=move.here, roots=[move.mac_root], active_root=move.mac_root)
    moves = []
    done = library_id.relocate(cfg, look=lambda root: [], move=lambda s, old, new: moves.append((old, new)))
    assert done == [(move.mac_root, str(move.drive))] and moves == done
    assert cfg.active_root == str(move.drive)


# --- the rest of that day's log ------------------------------------------------

class _Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _model(tmp_path, monkeypatch, content):
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(model_catalog.time, "sleep", lambda s: None)
    return {"url": "https://example.com/tiny.onnx", "name": "tiny.onnx", "folder": "onnx",
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def test_a_download_cut_short_carries_on_where_it_stopped(tmp_path, monkeypatch):
    content = b"0123456789" * 1000
    entry = _model(tmp_path, monkeypatch, content)
    asked = []

    def opener(request, timeout):
        asked.append(request.get_header("Range"))
        if len(asked) == 1:
            return _Response(content[:4000])                 # the connection closed early
        if len(asked) == 2:
            raise BrokenPipeError(32, "Broken pipe")
        start = int(request.get_header("Range").split("=")[1].rstrip("-"))
        return _Response(content[start:], status=206)

    path = model_catalog.download_file(entry, None, opener)
    assert path.read_bytes() == content
    assert asked == [None, "bytes=4000-", "bytes=4000-"]


def test_a_download_that_keeps_failing_says_it_was_cut_short(tmp_path, monkeypatch):
    content = b"x" * 5000
    entry = _model(tmp_path, monkeypatch, content)
    with pytest.raises(OSError, match="cut short"):
        model_catalog.download_file(entry, None, lambda request, timeout: _Response(b""))


def test_a_server_that_sends_it_all_again_starts_it_over(tmp_path, monkeypatch):
    content = b"abcdefghij" * 500
    entry = _model(tmp_path, monkeypatch, content)
    calls = []

    def opener(request, timeout):
        calls.append(request.get_header("Range"))
        return _Response(content[:1000] if len(calls) == 1 else content, status=200)

    seen = []
    path = model_catalog.download_file(entry, seen.append, opener)
    assert path.read_bytes() == content and sum(seen) == len(content)


def test_files_this_user_may_not_read_are_said_to_be_that():
    assert scanner.is_denied(PermissionError(13, "Permission denied"))
    assert scanner.is_denied("[Errno 13] Permission denied: '/media/x.HEIC'")
    assert not scanner.is_denied("cannot identify image file")
    advice = scanner.permission_advice("/media/jaga/Toshiba/NinaivuMaster", 569)
    assert "569 files" in advice and "not damaged" in advice
    if not advice.count("Properties"):
        assert 'chmod -R a+rX "/media/jaga/Toshiba/NinaivuMaster"' in advice


def test_a_stop_does_not_name_parts_that_stopped_after_the_budget():
    """The scan used the whole budget; every part asked after it was named as
    "still stopping after 20 seconds" the instant it was asked, though each
    stops in a moment. Only the scan is named now, and the stop keeps close
    to its budget."""
    import gc
    import threading
    import types
    from ninaivu import Services

    hold = threading.Event()
    quick = types.SimpleNamespace(stop=lambda *a, **k: time.sleep(0.01),
                                  pause=lambda: time.sleep(0.01),
                                  restore_stop=lambda: None)
    services = object.__new__(Services)
    services.scanner = types.SimpleNamespace(stop=lambda join=False: hold.wait(3), running=False)
    for name in ("straightener", "cloud", "guardian", "backups", "updates", "digest",
                 "disks", "restore_tests", "index_copy", "mirror", "offsite", "repairer",
                 "xmp", "importer", "power", "library_drives"):
        setattr(services, name, quick)
    services._pause_archive = lambda: None
    gc.collect()
    gc.disable()
    try:
        began = time.monotonic()
        problems = services.stop(timeout=0.3)
        elapsed = time.monotonic() - began
    finally:
        gc.enable()
        hold.set()
    assert [p for p in problems if not p.startswith("the library scan")] == [], problems
    assert any(p.startswith("the library scan") for p in problems)
    assert elapsed < 0.3 + 1.5


def test_a_part_left_a_sliver_of_the_budget_is_not_named():
    """On Windows the scan's wait can end a few milliseconds before the
    budget does; the part asked next had that sliver to stop in, and was
    named as stuck though it stopped a moment later."""
    import types
    from ninaivu import Services

    quick = types.SimpleNamespace(stop=lambda *a, **k: time.sleep(0.05),
                                  pause=lambda: time.sleep(0.05),
                                  restore_stop=lambda: None)
    services = object.__new__(Services)
    services.scanner = types.SimpleNamespace(stop=lambda join=False: time.sleep(0.28),
                                             running=False)
    for name in ("straightener", "cloud", "guardian", "backups", "updates", "digest",
                 "disks", "restore_tests", "index_copy", "mirror", "offsite", "repairer",
                 "xmp", "importer", "power", "library_drives"):
        setattr(services, name, quick)
    services._pause_archive = lambda: None
    assert services.stop(timeout=0.3) == []


# --- faster: one pass over the archive -------------------------------------------

def _bundle_with(tmp_path, change=None):
    """A full export, rewritten with *change* applied to its members."""
    import tarfile as tf
    state = _state(tmp_path / "src", "src", ["/photos"])
    bundle = backup.snapshot(state, tmp_path / "out", full=True)
    if change is None:
        return bundle
    rewritten = tmp_path / "rewritten.tar.gz"
    with tf.open(bundle, "r:gz") as old, tf.open(rewritten, "w:gz") as new:
        for member in old.getmembers():
            data = old.extractfile(member).read() if member.isfile() else None
            for m, d in change(member, data):
                new.addfile(m, io.BytesIO(d) if d is not None else None)
    return rewritten


def test_an_export_is_unpacked_and_checked_in_one_pass(tmp_path, monkeypatch):
    import tarfile as tf
    bundle = _bundle_with(tmp_path)
    opened = []
    real = tf.open
    monkeypatch.setattr(tf, "open", lambda *a, **k: opened.append(a[1:2] or k.get("mode"))
                        or real(*a, **k))
    told = []
    state = backup.extract_verified(bundle, tmp_path / "x", lambda d, t: told.append((d, t)))
    assert (state / "index.db").is_file() and (state / "thumbs").is_dir()
    assert opened == [("r|gz",)]                 # once, as a stream
    assert told[-1][0] == told[-1][1] == len(json.loads(
        (state / "backup_manifest.json").read_text())["files"])


def test_a_damaged_file_is_found_as_it_is_unpacked(tmp_path):
    def damage(member, data):
        if member.name.endswith(".webp") and data:
            data = b"X" + data[1:]
        yield member, data
    with pytest.raises(ValueError, match="mismatch"):
        backup.extract_verified(_bundle_with(tmp_path, damage), tmp_path / "x")


def test_a_file_the_manifest_never_listed_is_refused(tmp_path):
    import tarfile as tf

    def extra(member, data):
        yield member, data
        if member.name == "state/index.db":
            added = tf.TarInfo("state/thumbs/zz/extra.webp")
            added.size = 3
            yield added, b"abc"
    with pytest.raises(ValueError, match="exactly match"):
        backup.extract_verified(_bundle_with(tmp_path, extra), tmp_path / "x")


def test_a_name_outside_the_folder_is_refused_before_it_is_written(tmp_path):
    import tarfile as tf

    def escape(member, data):
        yield member, data
        if member.name == "state/index.db":
            bad = tf.TarInfo("state/../../escaped.txt")
            bad.size = 1
            yield bad, b"x"
    with pytest.raises(ValueError, match="unsafe"):
        backup.extract_verified(_bundle_with(tmp_path, escape), tmp_path / "x")
    assert not (tmp_path / "escaped.txt").exists()
