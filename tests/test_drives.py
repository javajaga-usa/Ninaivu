"""A pendrive, an external hard drive or a phone plugged in (from Ninaivu Lite
1.5): found, asked about once, and the library copied onto a drive without
touching what is already there."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ninaivu.utils import drives


def fake_drive(path, label="USB STICK", drive_id="usb1", kind="drive") -> drives.Drive:
    return drives.Drive(drive_id, str(path), label, 16 * 1024 ** 3, 8 * 1024 ** 3, kind=kind)


@pytest.fixture()
def plugged(app, tmp_path, monkeypatch):
    """One drive, plugged in or taken out by the test."""
    monkeypatch.setattr(drives, "CACHE_SECONDS", 0)
    usb = tmp_path / "USB"
    usb.mkdir()
    present = [fake_drive(usb)]
    app.extensions["ninaivu.drives"] = drives.Watcher(lister=lambda: list(present))
    return SimpleNamespace(path=usb, present=present)


@pytest.fixture()
def admin(as_admin):
    as_admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    return as_admin


def wait_export(admin, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = admin.get("/api/admin/drives/export").get_json()
        if not state["running"]:
            return state
        time.sleep(0.05)
    raise AssertionError("the export did not finish")


def test_listing_the_drives_never_fails_here():
    found = drives._windows_drives() if sys.platform == "win32" else drives.connected()
    assert isinstance(found, list)
    for d in found:
        assert d.id and d.path and d.label


@pytest.mark.skipif(sys.platform == "win32", reason="macOS and Linux mount points")
def test_mounted_folders_are_drives_and_the_own_disk_is_not(tmp_path, monkeypatch):
    volumes = tmp_path / "Volumes"
    (volumes / "PENDRIVE").mkdir(parents=True)
    (volumes / ".hidden").mkdir()
    (volumes / "Macintosh HD").symlink_to("/")
    (volumes / "plain-folder").mkdir()
    monkeypatch.setattr(drives.os.path, "ismount",
                        lambda p: p == "/" or p.endswith("PENDRIVE"))
    found = drives._mount_drives([str(volumes)])
    assert [d.label for d in found] == ["PENDRIVE"]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="GVFS is Linux's, and ':' cannot be in a Windows folder name")
def test_a_phone_the_linux_desktop_opened_is_found(tmp_path):
    gvfs = tmp_path / "gvfs"
    (gvfs / "mtp:host=Google_Pixel_7").mkdir(parents=True)
    (gvfs / "smb-share:server=nas").mkdir()
    [phone] = drives._gvfs_phones(str(gvfs))
    assert phone.kind == "phone" and phone.label == "Google Pixel 7"


def test_a_drive_is_asked_about_once_each_time_it_is_plugged_in(monkeypatch):
    monkeypatch.setattr(drives, "CACHE_SECONDS", 0)
    present = [fake_drive("E:\\")]
    watcher = drives.Watcher(lister=lambda: list(present))
    assert [d.id for d in watcher.pending()] == ["usb1"]
    watcher.answer("usb1")
    assert watcher.pending() == []
    present.clear()                      # taken out
    assert watcher.pending() == []
    present.append(fake_drive("E:\\"))   # and put back: asked again
    assert [d.id for d in watcher.pending()] == ["usb1"]


def test_the_console_asks_and_remembers_the_answer(admin, plugged):
    listed = admin.get("/api/admin/drives").get_json()
    assert [(d["label"], d["pending"]) for d in listed["drives"]] == [("USB STICK", True)]
    assert listed["export"]["running"] is False
    assert admin.post("/api/admin/drives/answer", json={"id": "usb1"}).status_code == 200
    assert admin.get("/api/admin/drives").get_json()["drives"][0]["pending"] is False
    gone = admin.post("/api/admin/drives/answer", json={"id": "nope"})
    assert gone.status_code == 404


def test_only_an_administrator_is_asked(app, as_family, plugged):
    as_family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    assert as_family.get("/api/admin/drives").status_code == 403
    assert app.test_client().get("/api/admin/drives").status_code == 401
    assert as_family.post("/api/admin/drives/export", json={"id": "usb1"}).status_code == 403


def test_the_drive_the_library_lives_on_is_not_asked_about(app, admin, scanned, monkeypatch):
    cfg, _, _ = scanned
    monkeypatch.setattr(drives, "CACHE_SECONDS", 0)
    disk = fake_drive(cfg.libraries[0])
    app.extensions["ninaivu.drives"] = drives.Watcher(lister=lambda: [disk])
    entry = admin.get("/api/admin/drives").get_json()["drives"][0]
    assert entry["holds_library"] is True and entry["pending"] is False
    assert admin.post("/api/admin/drives/export", json={"id": "usb1"}).status_code == 409


def test_the_library_is_not_copied_onto_a_phone(app, admin, plugged):
    plugged.present[:] = [fake_drive(plugged.path, "Pixel", "usb1", kind="phone")]
    refused = admin.post("/api/admin/drives/export", json={"id": "usb1"})
    assert refused.status_code == 409


def test_export_copies_the_library_and_adds_only_what_is_new(admin, plugged, scanned):
    cfg, _, _ = scanned
    root = Path(cfg.libraries[0])
    started = admin.post("/api/admin/drives/export", json={"id": "usb1"})
    assert started.status_code == 200, started.get_json()
    state = wait_export(admin)
    assert state["phase"] == "done", state
    out = plugged.path / "Ninaivu" / root.name
    shot = root / "2023/05/12/shot0.jpg"
    assert (out / "2023/05/12/shot0.jpg").read_bytes() == shot.read_bytes()
    assert not (out / "notes.txt").exists()                 # photos and videos only
    assert not list(out.rglob("*.partial"))
    copied = state["copied"]
    assert copied >= 10 and state["errors"] == 0
    assert state["message"]["key"].startswith("Copied {copied}")
    # Answering export is answering the question.
    assert admin.get("/api/admin/drives").get_json()["drives"][0]["pending"] is False

    # Next time: only the new photograph is copied.
    Image.new("RGB", (90, 60), (1, 2, 3)).save(root / "misc" / "new.jpg")
    assert admin.post("/api/admin/drives/export", json={"id": "usb1"}).status_code == 200
    again = wait_export(admin)
    assert again["copied"] == 1 and again["skipped"] == copied
    assert (out / "misc" / "new.jpg").is_file()


def test_a_different_file_with_the_same_name_is_kept_beside_it(admin, plugged, scanned):
    root = Path(scanned[0].libraries[0])
    theirs = plugged.path / "Ninaivu" / root.name / "misc" / "plain.png"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"someone else's file")
    assert admin.post("/api/admin/drives/export", json={"id": "usb1"}).status_code == 200
    assert wait_export(admin)["phase"] == "done"
    assert theirs.read_bytes() == b"someone else's file"     # never overwritten
    assert (theirs.parent / "plain (2).png").read_bytes() == \
        (root / "misc" / "plain.png").read_bytes()
