"""Drives plugged in and taken out, end to end.

* a library drive plugged back in while Ninaivu runs is watched afresh and
  read again, and one mounted at a new place is noticed without a restart;
* on a Mac, a folder in /Volumes that no disk is mounted at is never the
  library, and an import is not started onto or from a drive that is away;
* the Import page lists every drive plugged in now, and a drive can be
  ejected from the console, never the library's own or one in use;
* a copy to a drive that is pulled out part way stops, once, saying so.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ninaivu.storage import library_id
from ninaivu.storage import roots as roots_kit
from ninaivu.storage.library_drives import LibraryDrives
from ninaivu.utils import drives

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def fake_drive(path, label="USB STICK", drive_id="usb1", kind="drive") -> drives.Drive:
    return drives.Drive(drive_id, str(path), label, 16 * 1024 ** 3, 8 * 1024 ** 3, kind=kind)


@pytest.fixture()
def plugged(app, tmp_path, monkeypatch):
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


# -- the console's script -------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_the_drive_notices_and_the_import_page_list():
    done = subprocess.run([NODE, str(ROOT / "tests" / "drive_notice.mjs")],
                          capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert done.returncode == 0, done.stdout + done.stderr


# -- which drive a path is on ---------------------------------------------------


def test_the_drive_a_path_is_on_is_read_from_its_spelling():
    assert drives.drive_of("/Volumes/J Red/EXT4/Red") == "/Volumes/J Red"
    assert drives.drive_of("/media/pi/N300/Photos") == "/media/pi/N300"
    assert drives.drive_of("/run/media/anna/CARD/DCIM") == "/run/media/anna/CARD"
    assert drives.drive_of("/Users/jagadeesh/Pictures") is None
    assert drives.drive_of("/Volumes") is None


def test_a_drive_that_is_not_there_is_missing_and_one_that_is_is_not(monkeypatch):
    monkeypatch.setattr(os.path, "islink", lambda p: False)
    monkeypatch.setattr(os.path, "isdir", lambda p: p == "/Volumes/SSD" or p == "/Volumes/Ghost")
    monkeypatch.setattr(os.path, "ismount", lambda p: p == "/Volumes/SSD")
    assert drives.drive_missing("/Volumes/SSD/Archive") is None
    assert drives.drive_missing("/Volumes/Away/Archive") == "/Volumes/Away"
    # A folder left in /Volumes by something that wrote to an unplugged
    # drive's path: on the Mac's own disk, not the drive.
    assert drives.drive_missing("/Volumes/Ghost/Archive") == "/Volumes/Ghost"
    assert drives.drive_missing("/Users/me/Pictures") is None


def test_the_macs_own_disk_in_volumes_is_never_missing(monkeypatch):
    monkeypatch.setattr(os.path, "islink", lambda p: p == "/Volumes/Macintosh HD")
    assert drives.drive_missing("/Volumes/Macintosh HD/Users/me") is None


@pytest.fixture()
def mac_volumes(tmp_path, monkeypatch):
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    monkeypatch.setattr(roots_kit, "VOLUMES", volumes)
    monkeypatch.setattr(roots_kit, "_on_a_mac", lambda: True)
    mounted: set[str] = set()
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount",
                        lambda p: str(p) in mounted or real(p))
    return SimpleNamespace(path=volumes, mounted=mounted)


def test_a_folder_in_volumes_with_no_disk_at_it_is_not_the_library(mac_volumes):
    library = mac_volumes.path / "SSD" / "Archive"
    library.mkdir(parents=True)
    (library / "2024").mkdir()                     # it holds something
    assert roots_kit.ghost_volume(library)
    assert not roots_kit.root_present(library)
    mac_volumes.mounted.add(str(mac_volumes.path / "SSD"))
    assert not roots_kit.ghost_volume(library)
    assert roots_kit.root_present(library)


def test_the_macs_own_disk_listed_in_volumes_is_not_a_ghost(mac_volumes, tmp_path):
    own = tmp_path / "own"
    (own / "Pictures").mkdir(parents=True)
    (mac_volumes.path / "Macintosh HD").symlink_to(own)
    assert not roots_kit.ghost_volume(mac_volumes.path / "Macintosh HD" / "Pictures")


def test_an_import_is_not_started_onto_a_drive_that_is_away(tmp_path, monkeypatch):
    from ninaivu.archive import safety

    source = tmp_path / "card"
    source.mkdir()
    # Full paths as this system writes them: "/Volumes/…" is not one on Windows.
    if os.name == "nt":
        archive, ssd, card, red = r"X:\Archive", "X:\\", r"Y:\EXT4", "Y:\\"
    else:
        archive, ssd, card, red = ("/Volumes/SSD/Archive", "/Volumes/SSD",
                                   "/Volumes/J Red/EXT4", "/Volumes/J Red")
    away = {archive: ssd, card: red}
    monkeypatch.setattr(drives, "drive_missing", lambda p: away.get(str(p)))
    problems = safety.validate_job([{"path": str(source), "types": ["photo"]}], archive)
    assert any("which is not plugged in" in str(p) and ssd in str(p)
               for p in problems), problems
    problems = safety.validate_job([{"path": card, "types": ["photo"]}],
                                   str(tmp_path / "Archive"))
    assert any(f"“{red}”, which is not plugged in" in str(p) for p in problems), problems


# -- a library drive away and back -----------------------------------------------


def test_a_library_drive_plugged_back_in_is_handed_on(monkeypatch):
    there = {"/lib/a": True, "/lib/b": True}
    monkeypatch.setattr(roots_kit, "root_present", lambda root, **_: there[str(root)])
    monkeypatch.setattr(roots_kit, "BELIEVE_FOR", 0.0)
    roots_kit.forget()
    back, looked = [], []
    watch = LibraryDrives(SimpleNamespace(roots=["/lib/a", "/lib/b"]), back.append,
                          notice_moves=lambda cfg: looked.append(cfg) or {})
    assert watch.check() == []                     # the first look only takes note
    there["/lib/b"] = False
    assert watch.check() == []
    assert looked, "one away is looked for on the other disks"
    assert watch.check() == []
    there["/lib/b"] = True
    assert watch.check() == ["/lib/b"]
    assert back == [["/lib/b"]]
    assert watch.check() == [], "handed on once, not at every look"


def test_a_library_away_at_start_is_handed_on_when_it_arrives(monkeypatch):
    there = {"/lib/a": False}
    monkeypatch.setattr(roots_kit, "root_present", lambda root, **_: there[str(root)])
    roots_kit.forget()
    back = []
    watch = LibraryDrives(SimpleNamespace(roots=["/lib/a"]), back.append,
                          notice_moves=lambda cfg: {})
    watch.check()
    there["/lib/a"] = True
    # Believed "away" for its usual half a minute, it would be read only then.
    assert watch.check() == ["/lib/a"]


def test_looking_for_a_moved_library_is_not_done_at_every_look(monkeypatch):
    monkeypatch.setattr(roots_kit, "root_present", lambda root, **_: False)
    roots_kit.forget()
    now = [0.0]
    looked = []
    watch = LibraryDrives(SimpleNamespace(roots=["/lib/a"]), lambda roots: None,
                          notice_moves=lambda cfg: looked.append(now[0]) or {},
                          clock=lambda: now[0])
    for _ in range(3):
        watch.check()
        now[0] += 15
    now[0] += 60
    watch.check()
    assert looked == [0.0, 105.0]


def test_the_watcher_of_a_drive_that_came_back_is_made_afresh(app, monkeypatch):
    scanner = app.config["MV_SCANNER"]
    root = str(app.config["MV_CONFIG"].roots[0])

    class Stale:
        stopped = False

        def stop(self):
            self.stopped = True

    stale = Stale()
    scanner._observers[root] = stale
    started = []
    monkeypatch.setattr(scanner, "_start_watch", lambda path: started.append(str(path)))
    scanner.rewatch([root])
    assert stale.stopped
    assert root not in scanner._observers
    assert root in started


def test_the_app_reads_a_library_that_came_back(app, monkeypatch):
    services = app.config["MV_SERVICES"]
    root = str(app.config["MV_CONFIG"].roots[0])
    calls = []
    monkeypatch.setattr(services.scanner, "rewatch", lambda roots: calls.append(("watch", roots)))
    monkeypatch.setattr(services.scanner, "start",
                        lambda roots, **kw: calls.append(("scan", list(roots), kw)))
    monkeypatch.setattr(services, "_scoped_by_the_drive", lambda roots: {})
    services._library_back([root, "/not/a/library"])
    assert calls == [("watch", [root]), ("scan", [root], {})]


# -- a library found at a new place, while running ---------------------------------


def test_a_library_mounted_at_a_new_place_is_noticed_and_left_for_a_person(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    old = "/Volumes/SSD/Archive"
    new = tmp_path / "Volumes" / "SSD 1" / "Archive"
    new.mkdir(parents=True)
    (new / library_id.MARKER).write_text(json.dumps({"id": "abc"}), encoding="utf-8")
    (state / library_id.KNOWN).write_text(json.dumps({old: "abc"}), encoding="utf-8")
    cfg = SimpleNamespace(state_dir=state, roots=[old])
    look = lambda root: [new, tmp_path / "Volumes" / "Other"]          # noqa: E731

    waiting = library_id.notice_moves(cfg, look=look, mounts=lambda: [])
    assert waiting == {old: {"found": [str(new)], "noticed_at": waiting[old]["noticed_at"],
                             "confirmed": None}}
    assert cfg.roots == [old], "nothing is moved while the index is open"
    assert library_id.confirm_relocation(state, old, str(new))
    again = library_id.notice_moves(cfg, look=look, mounts=lambda: [])
    assert again[old]["confirmed"] == str(new), "a choice made is kept"
    # The drive came back where it was: nothing is waiting any more.
    cfg.roots = [str(tmp_path / "back")]
    assert library_id.notice_moves(cfg, look=look, mounts=lambda: []) == {}


def test_the_console_is_told_of_a_library_found_at_a_new_place(app, admin, plugged):
    state = Path(app.config["MV_CONFIG"].state_dir)
    (state / library_id.PENDING).write_text(json.dumps({
        "/Volumes/SSD/Archive": {"found": ["/Volumes/SSD 1/Archive"], "noticed_at": 1,
                                 "confirmed": None}}), encoding="utf-8")
    data = admin.get("/api/admin/drives").get_json()
    assert data["moved_libraries"] == [{"old": "/Volumes/SSD/Archive",
                                        "found": ["/Volumes/SSD 1/Archive"],
                                        "confirmed": None}]


# -- the drives listed, and ejected ------------------------------------------------


def test_each_drive_says_what_it_is_to_ninaivu(app, admin, plugged, monkeypatch):
    from ninaivu.archive import database as adb

    library = Path(app.config["MV_CONFIG"].roots[0])
    plugged.present.append(fake_drive(library, label="SSD", drive_id="ssd"))
    source = plugged.path / "DCIM"
    source.mkdir()
    monkeypatch.setattr(adb, "load_settings",
                        lambda: {"source_dirs": [{"path": str(source), "types": ["photo"]}]})
    rows = {d["id"]: d for d in admin.get("/api/admin/drives").get_json()["drives"]}
    assert rows["ssd"]["holds"] == "library" and rows["ssd"]["pending"] is False
    assert rows["usb1"]["holds"] is None
    assert rows["usb1"]["sources"] == [str(source)]
    assert rows["usb1"]["in_use"] is None


def test_a_drive_is_ejected_from_the_console(app, admin, plugged, monkeypatch):
    ejected = []
    monkeypatch.setattr(drives, "eject", lambda drive: ejected.append(drive.path))
    answer = admin.post("/api/admin/drives/eject", json={"id": "usb1"})
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json()["message"] == "You can unplug the drive now."
    assert ejected == [str(plugged.path)]


def test_the_system_refusing_to_eject_is_said(app, admin, plugged, monkeypatch):
    def refuse(drive):
        raise drives.EjectRefused("Volume USB STICK on disk4s1 failed to eject")

    monkeypatch.setattr(drives, "eject", refuse)
    answer = admin.post("/api/admin/drives/eject", json={"id": "usb1"})
    assert answer.status_code == 409
    body = answer.get_json()
    assert "failed to eject" in body["error"]
    assert body["said"].startswith("The drive could not be ejected: {why}.")


def test_the_library_drive_is_never_ejected(app, admin, plugged, monkeypatch):
    library = Path(app.config["MV_CONFIG"].roots[0])
    plugged.present[:] = [fake_drive(library, label="SSD", drive_id="ssd")]
    monkeypatch.setattr(drives, "eject", lambda drive: pytest.fail("ejected the library"))
    answer = admin.post("/api/admin/drives/eject", json={"id": "ssd"})
    assert answer.status_code == 409
    assert "photo library" in answer.get_json()["error"]


def test_a_drive_the_import_is_reading_is_not_ejected(app, admin, plugged, monkeypatch):
    from ninaivu.archive import scanner as archive_scanner

    monkeypatch.setattr(archive_scanner, "paths_in_use",
                        lambda: [str(plugged.path / "EXT4"), "/Volumes/SSD/Archive"])
    monkeypatch.setattr(drives, "eject", lambda drive: pytest.fail("ejected mid-import"))
    rows = admin.get("/api/admin/drives").get_json()["drives"]
    assert rows[0]["in_use"] == "import"
    answer = admin.post("/api/admin/drives/eject", json={"id": "usb1"})
    assert answer.status_code == 409
    assert "The import is using this drive" in answer.get_json()["error"]


def test_a_drive_being_copied_to_is_not_ejected(app, admin, plugged, monkeypatch):
    class Busy:
        running = True

        def progress(self):
            return {"running": True, "destination": str(plugged.path / "Ninaivu")}

    app.extensions["ninaivu.drive_export"] = Busy()
    monkeypatch.setattr(drives, "eject", lambda drive: pytest.fail("ejected mid-copy"))
    answer = admin.post("/api/admin/drives/eject", json={"id": "usb1"})
    assert answer.status_code == 409
    assert "being copied onto this drive" in answer.get_json()["error"]


def test_only_an_administrator_ejects(app, as_family, plugged):
    as_family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    assert as_family.post("/api/admin/drives/eject", json={"id": "usb1"}).status_code in (401, 403)


class Ran:
    def __init__(self, code=0, err=""):
        self.calls = []
        self.code, self.err = code, err

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        return SimpleNamespace(returncode=self.code, stdout="", stderr=self.err)


def test_a_mac_ejects_with_diskutil(monkeypatch):
    ran = Ran()
    monkeypatch.setattr(drives.sys, "platform", "darwin")
    monkeypatch.setattr(drives.subprocess, "run", ran)
    drives.eject(fake_drive("/Volumes/J Red"))
    assert ran.calls == [["diskutil", "eject", "/Volumes/J Red"]]


def test_a_mac_saying_no_is_passed_on(monkeypatch):
    ran = Ran(1, "Volume J Red on disk4s1 failed to eject\n")
    monkeypatch.setattr(drives.sys, "platform", "darwin")
    monkeypatch.setattr(drives.subprocess, "run", ran)
    with pytest.raises(drives.EjectRefused, match="failed to eject"):
        drives.eject(fake_drive("/Volumes/J Red"))


def test_linux_unmounts_then_powers_the_drive_down(monkeypatch, tmp_path):
    table = tmp_path / "mounts"
    table.write_text("/dev/sdb1 /media/pi/N300\\040Pro ext4 rw 0 0\n", encoding="utf-8")
    ran = Ran()
    monkeypatch.setattr(drives.sys, "platform", "linux")
    monkeypatch.setattr(drives.subprocess, "run", ran)
    monkeypatch.setattr(drives.shutil, "which", lambda name: f"/usr/bin/{name}")
    real = drives._linux_device
    monkeypatch.setattr(drives, "_linux_device", lambda path: real(path, str(table)))
    drives.eject(fake_drive("/media/pi/N300 Pro"))
    assert ran.calls == [["udisksctl", "unmount", "-b", "/dev/sdb1"],
                         ["udisksctl", "power-off", "-b", "/dev/sdb1"]]


def test_a_phone_is_not_ejected():
    with pytest.raises(drives.EjectRefused):
        drives.eject(fake_drive("usb:1234", kind="phone"))


def test_an_ejected_drive_is_read_afresh():
    present = [fake_drive("/Volumes/CARD")]
    watcher = drives.Watcher(lister=lambda: list(present))
    assert watcher.drives()
    present.clear()
    watcher.forget("usb1")
    assert watcher.drives() == [], "not listed for the seconds the last reading is kept"


# -- the folder picker -------------------------------------------------------------


def test_the_folder_picker_offers_the_drives_plugged_in(app, admin, plugged):
    library = app.config["MV_CONFIG"].roots[0]
    data = admin.get(f"/api/library/browse?path={library}").get_json()
    names = [entry["name"] for entry in data["shortcuts"]]
    assert "USB STICK (drive)" in names
    entry = next(e for e in data["shortcuts"] if e["name"] == "USB STICK (drive)")
    assert entry["path"] == str(plugged.path)


# -- a copy to a drive pulled out part way ------------------------------------------


def test_a_copy_to_a_drive_pulled_out_stops_once_saying_so(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    for n in range(5):
        Image.new("RGB", (8, 8), (n * 40, 0, 0)).save(library / f"p{n}.jpg")
    usb = tmp_path / "USB"
    usb.mkdir()
    tries = []

    def pulled_out(src, dest, cancel):
        tries.append(src)
        shutil.rmtree(usb, ignore_errors=True)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(drives, "_copy", pulled_out)
    exporter = drives.Exporter()
    exporter.start(fake_drive(usb), [str(library)], skip=[])
    exporter.wait(30)
    state = exporter.progress()
    assert state["phase"] == "failed"
    assert state["message"]["key"].startswith("The drive was taken out before the copy finished.")
    assert len(tries) == 1, "not one more error for every file after it"
    assert state["errors"] == 0
