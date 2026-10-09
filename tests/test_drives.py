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


# -- a Mac: which volumes were carried in, phones on the cable, "don't ask again" --

IOREG_IPHONE_AND_SSD = """\
+-o iPhone@01100000  <class IOUSBHostDevice, id 0x100000a1b, registered, matched, active, busy 0 (12 ms), retain 30>
  | {
  |   "USB Product Name" = "iPhone"
  |   "idVendor" = 1452
  |   "USB Serial Number" = "00008110000A1B2C"
  | }
  |
  +-o AppleUSBHostLegacyClient  <class AppleUSBHostLegacyClient, id 0x100000a1c, !registered, !matched, active, busy 0, retain 8>
  +-o PTP@0  <class IOUSBHostInterface, id 0x100000a20, registered, matched, active, busy 0 (0 ms), retain 7>
  | {
  |   "bInterfaceClass" = 6
  |   "USB Interface Name" = "PTP"
  | }
  |
+-o Portable SSD T7@02100000  <class IOUSBHostDevice, id 0x100000b1b, registered, matched, active, busy 0 (5 ms), retain 25>
  | {
  |   "USB Product Name" = "PSSD T7"
  |   "idVendor" = 1256
  |   "USB Serial Number" = "S5T7"
  | }
  |
  +-o IOUSBMassStorageInterfaceNub@0  <class IOUSBHostInterface, id 0x100000b20, registered, matched, active, busy 0 (0 ms), retain 7>
  | {
  |   "bInterfaceClass" = 8
  | }
+-o Pixel 7@03100000  <class IOUSBHostDevice, id 0x100000c1b, registered, matched, active, busy 0 (5 ms), retain 25>
  | {
  |   "USB Product Name" = "Pixel 7"
  |   "idVendor" = 6353
  |   "USB Serial Number" = "28A1"
  | }
  |
  +-o MTP@0  <class IOUSBHostInterface, id 0x100000c20, registered, matched, active, busy 0 (0 ms), retain 7>
  | {
  |   "bInterfaceClass" = 255
  |   "USB Interface Name" = "MTP"
  | }
"""


def test_a_phone_on_a_mac_is_found_in_the_usb_tree_and_a_usb_disk_is_not():
    found = drives._usb_photo_devices(IOREG_IPHONE_AND_SSD)
    assert [(d["name"], d["serial"]) for d in found] == [
        ("iPhone", "00008110000A1B2C"), ("Pixel 7", "28A1")]


def test_a_phone_on_a_mac_is_listed_as_not_readable(monkeypatch):
    done = SimpleNamespace(returncode=0, stdout=IOREG_IPHONE_AND_SSD)
    monkeypatch.setattr(drives.subprocess, "run", lambda *a, **k: done)
    phones = drives._mac_phones()
    assert [(p.label, p.kind, p.readable) for p in phones] == [
        ("iPhone", "phone", False), ("Pixel 7", "phone", False)]


@pytest.mark.parametrize("info, carried", [
    ({"BusProtocol": "USB", "Internal": False}, True),
    ({"BusProtocol": "Secure Digital", "Internal": True, "RemovableMedia": True}, True),
    ({"BusProtocol": "Thunderbolt", "Internal": False}, True),
    ({"BusProtocol": "Disk Image", "Internal": False}, False),      # an installer .dmg
    ({"BusProtocol": "Apple Fabric", "Internal": True}, False),     # the Mac's own disk
    (None, True),                                                    # diskutil said nothing
])
def test_which_mac_volumes_were_carried_in(tmp_path, info, carried):
    assert drives._mac_carried(str(tmp_path), info) is carried


def test_a_time_machine_disk_is_not_asked_about(tmp_path):
    (tmp_path / "2026-10-07-101500.previous").mkdir()
    assert drives._mac_carried(str(tmp_path), {"BusProtocol": "USB"}) is False
    other = tmp_path / "other"
    (other / "Backups.backupdb").mkdir(parents=True)
    assert drives._mac_carried(str(other), {"BusProtocol": "USB"}) is False


def test_dont_ask_again_is_kept_across_restarts_and_can_be_undone(tmp_path, monkeypatch):
    monkeypatch.setattr(drives, "CACHE_SECONDS", 0)
    remember = tmp_path / "state" / "drives-not-asked.json"
    present = [fake_drive("/Volumes/Immich SSD", "Immich SSD", "ssd1")]
    watcher = drives.Watcher(lister=lambda: list(present), remember=remember)
    watcher.never_ask(present[0])
    assert watcher.pending() == []
    # Plugged in again after a restart, with a new mount (a new id): still quiet.
    present[:] = [fake_drive("/Volumes/Immich SSD", "Immich SSD", "ssd1-again")]
    restarted = drives.Watcher(lister=lambda: list(present), remember=remember)
    assert restarted.pending() == [] and restarted.is_quiet(present[0])
    restarted.ask_again(present[0])
    assert [d.id for d in restarted.pending()] == ["ssd1-again"]


def test_the_console_can_set_a_drive_aside_and_bring_it_back(admin, plugged):
    assert admin.post("/api/admin/drives/answer", json={"id": "usb1", "never": True}).status_code == 200
    entry = admin.get("/api/admin/drives").get_json()["drives"][0]
    assert entry["quiet"] is True and entry["pending"] is False
    assert admin.post("/api/admin/drives/ask-again", json={"id": "usb1"}).status_code == 200
    entry = admin.get("/api/admin/drives").get_json()["drives"][0]
    assert entry["quiet"] is False
    assert admin.post("/api/admin/drives/ask-again", json={"id": "gone"}).status_code == 404


def test_the_import_destination_drive_is_not_asked_about(app, admin, plugged, monkeypatch):
    from ninaivu.archive import database as adb
    monkeypatch.setattr(adb, "load_settings",
                        lambda: {"destination_dir": str(plugged.path / "Archive")})
    entry = admin.get("/api/admin/drives").get_json()["drives"][0]
    assert entry["holds_library"] is True and entry["pending"] is False
