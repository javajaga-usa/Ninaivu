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
    with sqlite3.connect(folder / "index.db") as conn:
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
