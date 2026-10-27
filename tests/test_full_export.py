"""The whole index out of one computer and into another (storage/full_export.py).

A backup keeps what cannot be worked out again; a full export keeps what can
as well, because on a large library working it out again is days. What these
protect: the thumbnails, face crops and films travel and land; the thumbnail
keys travel in every bundle, so thumbnails copied by hand still match; an
import asked for from the console is done once, at the next start, before
anything is opened, and a failed one leaves the index that was there.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu import __version__
from ninaivu.cli import backup_restore as restore
from ninaivu.storage import backup, full_export

from conftest import ADMIN, login


def _state(folder: Path, label: str) -> Path:
    folder.mkdir(parents=True)
    # Closed, not only committed: `with sqlite3.connect()` leaves the file
    # open, and on Windows a folder with an open file in it cannot be
    # renamed, which is what a restore does to the state folder.
    with closing(sqlite3.connect(folder / "index.db")) as conn, conn:
        conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO assets (name) VALUES (?)", [(f"{label}-1",), (f"{label}-2",)])
        conn.execute("INSERT INTO albums (name) VALUES ('Holiday')")
    (folder / "config.json").write_text(json.dumps({"home_name": label}))
    (folder / "thumb-keys.json").write_text(json.dumps({"/photos": f"key-{label}"}))
    for name, inner in (("thumbs", "ab/abcdef.webp"), ("faces", "1.jpg"),
                        ("films", "f1/film.mp4"), ("letters", "l1.json")):
        path = folder / name / inner
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{label} {name}".encode())
    return folder


def members(bundle: Path) -> set[str]:
    with tarfile.open(bundle) as tar:
        return {m.name for m in tar.getmembers() if m.isfile()}


@pytest.fixture()
def machines(tmp_path, monkeypatch):
    """This computer's state, and a new computer's, with restore pointed at it."""
    here = _state(tmp_path / "here", "here")
    there = _state(tmp_path / "there", "there")
    monkeypatch.setattr(restore.Config, "load", lambda: SimpleNamespace(state_dir=there))
    return SimpleNamespace(here=here, there=there, base=tmp_path)


# --- what goes in --------------------------------------------------------

def test_a_full_export_carries_what_a_backup_leaves_to_be_rebuilt(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    assert bundle.name.startswith(backup.EXPORT_PREFIX)
    names = members(bundle)
    for name in ("state/index.db", "state/thumb-keys.json", "state/thumbs/ab/abcdef.webp",
                 "state/faces/1.jpg", "state/films/f1/film.mp4", "state/letters/l1.json"):
        assert name in names
    manifest = full_export.look(bundle)
    assert manifest["full"] and manifest["thumbnails"] == 1
    assert manifest["counts"]["items"] == 2 and manifest["counts"]["albums"] == 1
    assert backup.verify_bundle(bundle)["ok"]


def test_a_backup_carries_the_thumbnail_keys_but_not_the_thumbnails(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out")
    names = members(bundle)
    assert "state/thumb-keys.json" in names
    assert not any(name.startswith("state/thumbs/") for name in names)
    assert not full_export.look(bundle)["full"]


def test_an_export_is_not_one_of_the_scheduled_backups(machines):
    out = machines.base / "out"
    backup.snapshot(machines.here, out, full=True)
    assert backup.bundles(out) == []


def test_each_step_says_how_far_it_has_got(machines, monkeypatch):
    monkeypatch.setattr(backup, "REPORT_EVERY", 1)
    told = []
    backup.snapshot(machines.here, machines.base / "out", full=True,
                    progress=lambda step, **counts: told.append((step, counts)))
    steps = list(dict.fromkeys(step for step, _ in told))
    assert steps == ["copying", "checking", "verifying", "packing"]
    for step in steps:
        counts = [c for s, c in told if s == step]
        assert counts[0]["files_done"] == 0
        done = [c["files_done"] for c in counts]
        assert done == sorted(done), step
    # Copying counts the four folders' files, then each check goes to the end.
    copying = [c for s, c in told if s == "copying"]
    assert copying[-1]["files_done"] == copying[-1]["files_total"] == 4
    for step in ("checking", "verifying"):
        last = [c for s, c in told if s == step][-1]
        assert last["files_done"] == last["files_total"]
        assert last["bytes_done"] == last["bytes_total"] > 0


def test_the_last_export_is_still_said_after_a_restart(machines):
    exporter = full_export.Exporter()
    cfg = SimpleNamespace(state_dir=machines.here, roots=[], mirror_dir="")
    out = machines.base / "usb"
    out.mkdir()
    exporter.start(cfg, out)
    exporter._thread.join(60)
    done = exporter.status(machines.here)
    assert done["ok"] and done["bundle"] and done["seconds"] >= 0
    assert done["files_done"] == done["files_total"]
    # A new server process remembers nothing, and reads it back.
    again = full_export.Exporter().status(machines.here)
    assert again["bundle"] == done["bundle"] and again["ok"] and not again["running"]


# --- what comes back -----------------------------------------------------

def test_a_full_import_brings_the_thumbnails_and_replaces_the_old_ones(machines):
    (machines.there / "thumbs" / "zz").mkdir()
    (machines.there / "thumbs" / "zz" / "stale.webp").write_bytes(b"old")
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    assert restore.restore_backup(bundle)
    there = machines.there
    assert (there / "thumbs" / "ab" / "abcdef.webp").read_bytes() == b"here thumbs"
    assert not (there / "thumbs" / "zz").exists()
    assert (there / "films" / "f1" / "film.mp4").read_bytes() == b"here films"
    assert json.loads((there / "thumb-keys.json").read_text()) == {"/photos": "key-here"}
    assert json.loads((there / "config.json").read_text())["home_name"] == "here"
    # The index that was there is kept beside it.
    kept = list(machines.base.glob(".ninaivu_pre_restore_there_*"))
    assert kept and (kept[0] / "thumbs" / "zz" / "stale.webp").is_file()
    # Nothing unpacked is left behind.
    assert not list(machines.base.glob(".ninaivu_restore_unpacked_*"))


def test_a_full_import_keeps_how_this_computer_is_reached(machines):
    # The other computer's ports and console settings came with its settings,
    # and after the import's restart Ninaivu answered where the page waiting
    # for it was not looking.
    (machines.here / "config.json").write_text(json.dumps({
        "home_name": "here", "port": 8080, "admin_port": 3001,
        "console_on_network": True, "https": "on", "roots": ["/Volumes/Photos"]}))
    (machines.there / "config.json").write_text(json.dumps({
        "home_name": "there", "admin_port": 3000, "network_access": False}))
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    assert restore.restore_backup(bundle)
    settings = json.loads((machines.there / "config.json").read_text())
    # The household's settings and library folders come with the export ...
    assert settings["home_name"] == "here" and settings["roots"] == ["/Volumes/Photos"]
    # ... and this computer stays where it was, its defaults included.
    assert settings["admin_port"] == 3000 and settings["network_access"] is False
    for name in ("port", "console_on_network", "https"):
        assert name not in settings


def test_a_backup_restore_keeps_the_thumbnails_this_machine_has(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out")
    assert restore.restore_backup(bundle)
    assert (machines.there / "thumbs" / "ab" / "abcdef.webp").read_bytes() == b"there thumbs"


def test_a_bundle_from_before_the_keys_keeps_this_machines_keys(machines):
    (machines.here / "thumb-keys.json").unlink()
    bundle = backup.snapshot(machines.here, machines.base / "out")
    assert restore.restore_backup(bundle)
    assert json.loads((machines.there / "thumb-keys.json").read_text()) == {"/photos": "key-there"}


# --- looking before importing -------------------------------------------

def test_a_folder_is_looked_in_for_the_newest_export(machines):
    out = machines.base / "out"
    backup.snapshot(machines.here, out)
    made = backup.snapshot(machines.here, out, full=True)
    assert full_export.find_bundle(out) == made


def test_a_folder_with_nothing_in_it_is_said_plainly(tmp_path):
    with pytest.raises(ValueError, match="no Ninaivu export"):
        full_export.look(tmp_path)


def test_a_file_that_is_not_an_export_is_refused(tmp_path):
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a tar")
    with pytest.raises(ValueError, match="cannot be read"):
        full_export.look(junk)


def test_an_export_another_account_owns_says_so_rather_than_damaged(machines, monkeypatch):
    # Made private on a Mac, carried on a drive that keeps owners to Linux:
    # the file is whole, only not this account's to open.
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)

    def refused(*args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(full_export.tarfile, "open", refused)
    with pytest.raises(ValueError, match="sudo chmod a\\+r") as caught:
        full_export.look(bundle)
    assert "damaged" not in str(caught.value)


def test_an_export_cut_short_or_empty_is_said_to_be(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    whole = bundle.read_bytes()
    bundle.write_bytes(whole[:120])
    with pytest.raises(ValueError, match="cut short"):
        full_export.look(bundle)
    bundle.write_bytes(b"")
    with pytest.raises(ValueError, match="is empty"):
        full_export.look(bundle)


def test_an_export_from_a_newer_ninaivu_is_refused(machines, monkeypatch):
    monkeypatch.setattr(backup, "__version__", "999.0.0")
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    with pytest.raises(ValueError, match="newer than this"):
        full_export.look(bundle)


# --- the import the next start does ---------------------------------------

def test_an_import_asked_for_is_done_once_at_the_next_start(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    full_export.ask_for_import(machines.there, bundle, by="dad")
    assert full_export.pending(machines.there)["bundle"] == str(bundle)
    assert full_export.apply_pending(machines.there) is True
    assert json.loads((machines.there / "config.json").read_text())["home_name"] == "here"
    assert full_export.pending(machines.there) is None
    result = full_export.last_result(machines.there)
    assert result["ok"] is True and result["full"] and result["thumbnails"] == 1
    assert result["counts"]["items"] == 2 and result["seconds"] >= 0
    assert full_export.apply_pending(machines.there) is None


def test_a_failed_import_leaves_the_index_that_was_there(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    full_export.ask_for_import(machines.there, bundle)
    bundle.unlink()
    assert full_export.apply_pending(machines.there) is False
    assert json.loads((machines.there / "config.json").read_text())["home_name"] == "there"
    result = full_export.last_result(machines.there)
    assert result["ok"] is False and "not found" in result["error"]
    assert result["seconds"] >= 0
    # Tried once, not at every start.
    assert full_export.pending(machines.there) is None


def test_the_import_is_done_before_the_server_takes_the_folder(monkeypatch, tmp_path):
    import ninaivu.__main__ as entry
    from ninaivu.server import runfile

    order = []
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(full_export, "apply_pending", lambda state: order.append("import"))

    def lock(state):
        order.append("lock")
        raise runfile.AlreadyRunning("already running")

    monkeypatch.setattr(runfile, "server_lock", lock)
    assert entry.main([]) == 1
    assert order == ["import", "lock"]


# --- the console --------------------------------------------------------

@pytest.fixture()
def console(scanned):
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.save()
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    yield login(app.test_client(), *ADMIN), cfg
    services.stop(timeout=5.0)


def _wait_for_export():
    from ninaivu.api import migration_api
    exporter = migration_api._the_exporter()
    if exporter._thread is not None:
        exporter._thread.join(60)
    return exporter


def test_the_console_exports_and_reads_it_back(console, tmp_path):
    client, cfg = console
    out = tmp_path / "usb"
    out.mkdir()
    started = client.post("/api/admin/migration/export", json={"folder": str(out)})
    assert started.status_code == 202, started.get_json()
    _wait_for_export()
    state = client.get("/api/admin/migration/export").get_json()
    bundle = state["export"]["bundle"]
    assert bundle and Path(bundle).parent == out and not state["export"]["error"]

    looked = client.post("/api/admin/migration/import/look", json={"path": str(out)}).get_json()
    assert looked["full"] and looked["counts"]["items"] > 0
    assert looked["version"] == __version__


def test_the_console_will_not_export_into_a_library_or_its_own_folder(console):
    client, cfg = console
    for folder in (cfg.roots[0], str(cfg.state_dir), "relative/path"):
        reply = client.post("/api/admin/migration/export", json={"folder": folder})
        assert reply.status_code == 400, folder


def test_the_console_asks_for_an_import_and_can_take_it_back(console, tmp_path):
    client, cfg = console
    source = _state(tmp_path / "elsewhere", "elsewhere")
    bundle = backup.snapshot(source, tmp_path / "usb", full=True)
    reply = client.post("/api/admin/migration/import", json={"path": str(bundle)})
    assert reply.status_code == 202
    body = reply.get_json()
    # A test server cannot restart itself; it says so instead.
    assert body["restarting"] is False and body["restart_problem"]
    # What the page needs to tell a slow import from a server that is gone,
    # which it cannot ask once the server is down.
    assert body["patience_seconds"] >= 300
    assert body["restart_log"].endswith("restart.log") and body["server_log"].endswith("server.log")
    assert body["pending"]["bundle"] == str(bundle)
    assert full_export.pending(cfg.state_dir)["bundle"] == str(bundle)

    cancelled = client.post("/api/admin/migration/import/cancel", json={}).get_json()
    assert cancelled["pending"] is None
    assert full_export.pending(cfg.state_dir) is None


def test_the_console_refuses_an_import_it_cannot_read(console, tmp_path):
    client, cfg = console
    reply = client.post("/api/admin/migration/import", json={"path": str(tmp_path / "nothing")})
    assert reply.status_code == 400
    assert full_export.pending(cfg.state_dir) is None


# --- saying at once what is happening, and refusing early -----------------

def test_an_export_goes_only_into_an_empty_folder(machines):
    cfg = SimpleNamespace(state_dir=machines.here, roots=[], mirror_dir="")
    out = machines.base / "usb"
    out.mkdir()
    (out / "holiday.jpg").write_bytes(b"x")
    with pytest.raises(full_export.Refused) as refused:
        full_export.Exporter().start(cfg, out)
    assert "not empty" in refused.value.said and "holiday.jpg" in refused.value.said
    assert refused.value.said.template.startswith("{folder} is not empty")


def test_what_a_computer_leaves_in_a_folder_does_not_make_it_full(machines):
    cfg = SimpleNamespace(state_dir=machines.here, roots=[], mirror_dir="")
    out = machines.base / "usb"
    out.mkdir()
    for name in (".DS_Store", "._holiday.jpg", "Thumbs.db", "desktop.ini"):
        (out / name).write_bytes(b"x")
    full_export.check_export_folder(out, cfg)


def test_an_export_says_it_is_preparing_before_it_has_measured_anything(machines, monkeypatch):
    import threading
    measuring = threading.Event()
    go_on = threading.Event()
    real = full_export.size_of_export

    def slow(state_dir):
        measuring.set()
        go_on.wait(10)
        return real(state_dir)

    monkeypatch.setattr(full_export, "size_of_export", slow)
    exporter = full_export.Exporter()
    cfg = SimpleNamespace(state_dir=machines.here, roots=[], mirror_dir="")
    out = machines.base / "usb"
    out.mkdir()
    started = exporter.start(cfg, out)
    assert started["running"] and started["step"] == "preparing"
    assert measuring.wait(10)
    assert exporter.status()["step_number"] == 1
    go_on.set()
    exporter._thread.join(60)
    assert exporter.status()["ok"]


def test_a_drive_too_small_is_said_as_the_export_result(machines, monkeypatch):
    exporter = full_export.Exporter()
    cfg = SimpleNamespace(state_dir=machines.here, roots=[], mirror_dir="")
    out = machines.base / "usb"
    out.mkdir()
    monkeypatch.setattr(full_export.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=1024, total=1, used=1))
    exporter.start(cfg, out)
    exporter._thread.join(60)
    done = exporter.status()
    assert not done["ok"] and "Choose a bigger drive" in done["error"]
    assert done["said"].startswith("{folder} has {free} MB free") and done["params"]["free"] == "0"
    assert list(out.iterdir()) == []


def _check(bundle: Path) -> dict:
    checker = full_export.Checker()
    checker.start(full_export.look(bundle))
    checker._thread.join(60)
    return checker.status()


def test_a_whole_export_reads_through(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    done = _check(bundle)
    assert done["ok"] and not done["error"]
    assert done["files_done"] == done["files_total"] > 0
    assert done["bytes_done"] == done["bytes_total"] > 0


def test_an_export_cut_short_is_found_before_the_restart(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    data = bundle.read_bytes()
    bundle.write_bytes(data[: len(data) - 200])
    done = _check(bundle)
    assert not done["ok"] and "cut short" in done["error"]


def test_a_damaged_file_in_an_export_is_named(machines):
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    unpacked = machines.base / "unpacked"
    with tarfile.open(bundle) as tar:
        tar.extractall(unpacked, filter="data")
    (unpacked / "state" / "faces" / "1.jpg").write_bytes(b"something else")
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(unpacked / "state", arcname="state")
    done = _check(bundle)
    assert not done["ok"] and "faces/1.jpg" in done["error"] and "damaged" in done["error"]


def test_an_export_that_never_finished_is_said_plainly(tmp_path):
    (tmp_path / f"{backup.PARTIAL_PREFIX}abc.tmp").write_bytes(b"half")
    with pytest.raises(full_export.Refused) as refused:
        full_export.look(tmp_path)
    assert "never finished" in refused.value.said


def test_an_import_with_no_room_to_unpack_is_refused(machines, monkeypatch):
    bundle = backup.snapshot(machines.here, machines.base / "out", full=True)
    assert full_export.look(bundle)["unpacked_bytes"] > 0
    monkeypatch.setattr(full_export.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=1024, total=1, used=1))
    with pytest.raises(full_export.Refused) as refused:
        full_export.ask_for_import(machines.there, bundle)
    assert "Make room" in refused.value.said
    assert full_export.pending(machines.there) is None


def test_the_console_refuses_a_folder_that_is_not_empty(console, tmp_path):
    client, cfg = console
    out = tmp_path / "usb"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    reply = client.post("/api/admin/migration/export", json={"folder": str(out)})
    assert reply.status_code == 400
    body = reply.get_json()
    assert "not empty" in body["error"] and body["said"].startswith("{folder} is not empty")
    assert body["params"]["name"] == "notes.txt"


def test_the_console_checks_an_export_and_will_not_import_a_damaged_one(console, tmp_path):
    from ninaivu.api import migration_api
    client, cfg = console
    source = _state(tmp_path / "elsewhere", "elsewhere")
    bundle = backup.snapshot(source, tmp_path / "usb", full=True)
    data = bundle.read_bytes()
    bundle.write_bytes(data[: len(data) - 200])
    looked = client.post("/api/admin/migration/import/look", json={"path": str(bundle)})
    assert looked.status_code == 200 and looked.get_json()["check"]["running"] in (True, False)
    migration_api._the_checker()._thread.join(60)
    state = client.get("/api/admin/migration/export").get_json()
    assert state["check"]["ok"] is False and "cut short" in state["check"]["error"]
    reply = client.post("/api/admin/migration/import", json={"path": str(bundle)})
    assert reply.status_code == 400 and "cut short" in reply.get_json()["error"]
    assert full_export.pending(cfg.state_dir) is None


def test_the_wait_for_an_import_grows_with_its_size():
    from ninaivu.api import migration_api
    small = migration_api.import_patience({"unpacked_bytes": 0})
    large = migration_api.import_patience({"unpacked_bytes": 40 * 1024 ** 3})
    assert small == 300 and large > small + 60 * 60
