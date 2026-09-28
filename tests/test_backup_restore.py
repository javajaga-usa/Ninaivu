"""Restore drills use isolated state folders, including deliberate failures."""

import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.storage import backup
from tools import backup_restore as restore


@pytest.fixture()
def recovery(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    with sqlite3.connect(source / "index.db") as conn:
        conn.execute("CREATE TABLE memories (name TEXT)")
        conn.execute("INSERT INTO memories VALUES ('holiday')")
    (source / "config.json").write_text('{"home_name":"Restored"}')
    bundle = backup.snapshot(source, tmp_path / "backups")
    assert bundle is not None
    destination = tmp_path / "live"
    destination.mkdir()
    (destination / "config.json").write_text('{"home_name":"Original"}')
    (destination / "local-token").write_bytes(b"preserve credentials")
    monkeypatch.setattr(restore.Config, "load", lambda: SimpleNamespace(state_dir=destination))
    return SimpleNamespace(source=source, bundle=bundle, destination=destination, base=tmp_path)


def rewrite_bundle(recovery, transform):
    entries = {}
    with tarfile.open(recovery.bundle) as archive:
        for entry in archive:
            if entry.isfile():
                entries[entry.name] = archive.extractfile(entry).read()
    transform(entries)
    changed = recovery.base / "changed.tar.gz"
    with tarfile.open(changed, "w:gz") as archive:
        for name, content in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return changed


def contents(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_restore_round_trip_preserves_original_and_local_files(recovery):
    for suffix in ("-wal", "-shm", "-journal"):
        (recovery.destination / f"index.db{suffix}").write_bytes(b"stale")
    original = contents(recovery.destination)
    assert restore.restore_backup(recovery.bundle)
    with sqlite3.connect(recovery.destination / "index.db") as conn:
        assert conn.execute("SELECT name FROM memories").fetchall() == [("holiday",)]
    assert (recovery.destination / "local-token").read_bytes() == b"preserve credentials"
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (recovery.destination / f"index.db{suffix}").exists()
    saved = list(recovery.base.glob(".ninaivu_pre_restore_*"))
    assert len(saved) == 1
    assert contents(saved[0]) == original


def test_restore_to_new_directory(recovery):
    for child in recovery.destination.iterdir():
        child.unlink()
    recovery.destination.rmdir()
    assert restore.restore_backup(recovery.bundle)
    assert (recovery.destination / "index.db").exists()
    assert not list(recovery.base.glob(".ninaivu_pre_restore_*"))


@pytest.mark.parametrize("bad_name", [
    "state/../escape", "../escape", "/absolute", "state/C:/escape",
    "state/avatars/../../escape", "state/avatars\\escape", "state/unexpected",
])
def test_unsafe_archive_names_leave_destination_untouched(recovery, bad_name):
    original = contents(recovery.destination)
    bundle = rewrite_bundle(recovery, lambda entries: entries.update({bad_name: b"bad"}))
    assert not restore.restore_backup(bundle)
    assert contents(recovery.destination) == original


@pytest.mark.parametrize("corruption", ["unlisted", "missing", "checksum", "size", "json", "schema", "index"])
def test_invalid_manifest_leaves_destination_untouched(recovery, corruption):
    def damage(entries):
        manifest = json.loads(entries["state/backup_manifest.json"])
        if corruption == "unlisted":
            entries["state/library-path"] = b"unverified"
        elif corruption == "missing":
            del entries["state/config.json"]
        elif corruption == "checksum":
            entries["state/config.json"] = b"changed"
        elif corruption == "size":
            manifest["files"]["config.json"]["size"] += 1
        elif corruption == "schema":
            manifest = []
        elif corruption == "index":
            del entries["state/index.db"]
            del manifest["files"]["index.db"]
        entries["state/backup_manifest.json"] = (
            b"{" if corruption == "json" else json.dumps(manifest).encode())

    original = contents(recovery.destination)
    assert not restore.restore_backup(rewrite_bundle(recovery, damage))
    assert contents(recovery.destination) == original


def test_invalid_database_with_valid_checksum_is_rejected(recovery):
    def damage(entries):
        entries["state/index.db"] = b"not sqlite"
        manifest = json.loads(entries["state/backup_manifest.json"])
        manifest["files"]["index.db"] = {
            "size": 10, "sha256": hashlib.sha256(b"not sqlite").hexdigest()}
        entries["state/backup_manifest.json"] = json.dumps(manifest).encode()

    original = contents(recovery.destination)
    assert not restore.restore_backup(rewrite_bundle(recovery, damage))
    assert contents(recovery.destination) == original


def test_copy_failure_leaves_original_untouched(recovery, monkeypatch):
    original = contents(recovery.destination)
    real_copy = restore.shutil.copy2

    def interrupted(source, target, **kwargs):
        if Path(source).name == "local-token":
            raise OSError("disk full")
        return real_copy(source, target, **kwargs)

    monkeypatch.setattr(restore.shutil, "copy2", interrupted)
    assert not restore.restore_backup(recovery.bundle)
    assert contents(recovery.destination) == original
    assert not list(recovery.base.glob(".ninaivu_pre_restore_*"))


def test_publication_failure_rolls_back(recovery, monkeypatch):
    original = contents(recovery.destination)
    rename = Path.rename

    def interrupted(path, destination):
        if path.name == "state" and path.parent.name.startswith(".ninaivu_restore_"):
            raise OSError("publication failed")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", interrupted)
    assert not restore.restore_backup(recovery.bundle)
    assert contents(recovery.destination) == original


def test_live_server_blocks_restore(recovery, monkeypatch):
    monkeypatch.setattr(restore.runfile, "read", lambda folder: {"pid": 123})
    monkeypatch.setattr(restore.runfile, "is_running", lambda pid: True)
    original = contents(recovery.destination)
    assert not restore.restore_backup(recovery.bundle)
    assert contents(recovery.destination) == original


def test_backup_copy_failure_does_not_publish_incomplete_bundle(recovery, monkeypatch):
    def interrupted(*args, **kwargs):
        raise OSError("unreadable config")

    monkeypatch.setattr(backup.shutil, "copy2", interrupted)
    out = recovery.base / "incomplete"
    assert backup.snapshot(recovery.source, out) is None
    assert backup.bundles(out) == []


def test_backup_requires_library_index(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    assert backup.snapshot(source, tmp_path / "out") is None


def test_restore_replaces_entire_backed_up_directory(recovery):
    avatars = recovery.destination / "avatars"
    avatars.mkdir()
    (avatars / "obsolete.png").write_bytes(b"old avatar")
    assert restore.restore_backup(recovery.bundle)
    assert not avatars.exists()
    assert list(recovery.base.glob(".ninaivu_pre_restore_*/avatars/obsolete.png"))


def test_mixed_case_wal_is_not_carried_into_restored_state(recovery):
    (recovery.destination / "INDEX.DB-WAL").write_bytes(b"old wal")
    assert restore.restore_backup(recovery.bundle)
    assert not (recovery.destination / "INDEX.DB-WAL").exists()


@pytest.mark.parametrize("kind", ["duplicate", "symlink", "hardlink"])
def test_duplicate_and_link_members_are_rejected(recovery, kind):
    path = recovery.base / "invalid-members.tar.gz"
    with tarfile.open(path, "w:gz") as output:
        with tarfile.open(recovery.bundle) as original:
            for entry in original:
                output.addfile(entry, original.extractfile(entry) if entry.isfile() else None)
        extra = tarfile.TarInfo("state/config.json")
        if kind != "duplicate":
            extra.name = "state/library-path"
            extra.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            extra.linkname = "state/config.json"
        output.addfile(extra)
    original = contents(recovery.destination)
    assert not restore.restore_backup(path)
    assert contents(recovery.destination) == original


def test_duplicate_manifest_keys_are_rejected(recovery):
    def damage(entries):
        entries["state/backup_manifest.json"] = b'{"files": {}, "files": {}}'

    original = contents(recovery.destination)
    assert not restore.restore_backup(rewrite_bundle(recovery, damage))
    assert contents(recovery.destination) == original


def test_keyboard_interrupt_rolls_back(recovery, monkeypatch):
    original = contents(recovery.destination)
    rename = Path.rename

    def interrupted(path, destination):
        if path.name == "state" and path.parent.name.startswith(".ninaivu_restore_"):
            raise KeyboardInterrupt()
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", interrupted)
    with pytest.raises(KeyboardInterrupt):
        restore.restore_backup(recovery.bundle)
    assert contents(recovery.destination) == original


def test_failed_rollback_preserves_original_at_reported_path(recovery, monkeypatch, capsys):
    original = contents(recovery.destination)
    rename = Path.rename

    def interrupted(path, destination):
        if Path(destination) == recovery.destination:
            raise OSError("destination unavailable")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", interrupted)
    assert not restore.restore_backup(recovery.bundle)
    saved = list(recovery.base.glob(".ninaivu_pre_restore_*"))
    assert len(saved) == 1
    assert contents(saved[0]) == original
    assert str(saved[0]) in capsys.readouterr().err


def test_server_starting_during_preparation_blocks_swap(recovery, monkeypatch):
    checks = iter([None, {"pid": 123}])
    monkeypatch.setattr(restore.runfile, "read", lambda folder: next(checks))
    monkeypatch.setattr(restore.runfile, "is_running", lambda pid: True)
    original = contents(recovery.destination)
    assert not restore.restore_backup(recovery.bundle)
    assert contents(recovery.destination) == original


def test_existing_config_symlink_cannot_overwrite_external_file(recovery):
    outside = recovery.base / "external-config.json"
    outside.write_bytes(b"leave untouched")
    config = recovery.destination / "config.json"
    config.unlink()
    try:
        config.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    assert restore.restore_backup(recovery.bundle)
    assert outside.read_bytes() == b"leave untouched"
    assert not config.is_symlink()
    assert json.loads(config.read_text())["home_name"] == "Restored"


def test_state_directory_symlink_is_refused(recovery, monkeypatch):
    link = recovery.base / "linked-state"
    try:
        link.symlink_to(recovery.destination, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    monkeypatch.setattr(restore.Config, "load", lambda: SimpleNamespace(state_dir=link))
    original = contents(recovery.destination)
    assert not restore.restore_backup(recovery.bundle)
    assert contents(recovery.destination) == original
