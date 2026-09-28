"""Reading what Windows already knows about failing drives.

Every case here feeds the watcher a stand-in for what Windows reports — the
System log's storage events, the physical disks, the volumes — shaped like
the real thing seen on this household's machine, where a backup drive logged
791 bad blocks and the library disk lost a journal write when it vanished for
a moment. Nothing here touches a real drive.
"""

import time
from types import SimpleNamespace

import pytest

from ninaivu.storage import db, disk_health
from ninaivu.storage.disk_health import CRITICAL, DiskWatch, WARNING

NOW = 1_790_000_000.0
TB = 1024 ** 4

LIBRARY = {"n": 1, "name": "Seagate BUP RD", "serial": "NA9S8THX", "bus": "USB",
           "health": "Healthy", "op": "OK"}
BACKUP = {"n": 3, "name": "Seagate BUP Slim BL", "serial": "NA7Q5ZZZ", "bus": "USB",
          "health": "Healthy", "op": "OK"}
SYSTEM = {"n": 0, "name": "AirDisk 512GB SSD", "serial": "0000_0006", "bus": "NVMe",
          "health": "Healthy", "op": "OK"}


def volume(letter, free=2 * TB, size=5 * TB, health="Healthy"):
    return {"l": letter, "label": "", "health": health, "op": "OK",
            "free": free, "size": size}


def bad_block(record, at, disk=3):
    return {"r": record, "p": "disk", "id": 7, "t": at,
            "props": [f"\\Device\\Harddisk{disk}\\DR8"],
            "msg": f"The device, \\Device\\Harddisk{disk}\\DR8, has a bad block."}


class Windows:
    """What the probe would have returned. Tests change it between looks."""

    def __init__(self):
        self.events = []
        self.disks = [SYSTEM, LIBRARY, BACKUP]
        self.parts = [{"n": 0, "l": "C"}, {"n": 1, "l": "E"}, {"n": 3, "l": "F"}]
        self.volumes = [volume("C", 100 * 1024 ** 3, 500 * 1024 ** 3), volume("E"),
                        volume("F")]
        self.asked = []

    def __call__(self, since):
        self.asked.append(since)
        return {"events": list(self.events), "disks": list(self.disks),
                "parts": list(self.parts), "volumes": list(self.volumes)}


@pytest.fixture()
def world(tmp_path):
    path = tmp_path / "index.db"
    db.init_db(path)
    clock = SimpleNamespace(now=NOW)
    windows = Windows()
    told = []
    cfg = SimpleNamespace(roots=[r"E:\MasterArchive"], state_dir=r"C:\Users\x\.ninaivu")
    watch = DiskWatch(cfg, lambda: db.connect(path), probe=windows,
                      notify=lambda *a: told.append(a), clock=lambda: clock.now)
    return watch, windows, clock, told


def drive(report, name):
    return next(d for d in report["drives"] if d["name"] == name)


# -- what an event means --------------------------------------------------------

def test_the_events_that_matter_are_named():
    assert disk_health.classify("disk", 7)[:2] == ("bad_blocks", CRITICAL)
    assert disk_health.classify("UASPStor", 129)[:2] == ("resets", WARNING)
    assert disk_health.classify("Microsoft-Windows-Ntfs", 140)[0] == "writes_lost"
    assert disk_health.classify("disk", 9999) is None


def test_a_volume_reported_healthy_is_not_a_problem():
    assert disk_health.classify("Microsoft-Windows-Ntfs", 98,
                                "Volume C: is healthy.  No action is needed.") is None
    assert disk_health.classify("Microsoft-Windows-Ntfs", 98,
                                "Volume F: needs to be taken offline to perform a "
                                "Full Repair.") is not None


# -- which drive ------------------------------------------------------------------

def test_a_recent_error_is_filed_under_the_drive_by_serial(world):
    watch, windows, clock, _ = world
    windows.events = [bad_block(1, NOW - 60)]
    report = watch.check()
    backup = drive(report, "Seagate BUP Slim BL")
    assert backup["status"] == CRITICAL
    assert backup["problems"][0]["count"] == 1 and backup["letters"] == ["F"]


def test_a_week_old_error_from_before_watching_is_not_guessed_at(world):
    """Disk numbers change as USB drives come and go: "Disk 3" last week need
    not be the drive that is Disk 3 now."""
    watch, windows, _, _ = world
    windows.events = [bad_block(1, NOW - 3 * 86400)]
    report = watch.check()
    assert drive(report, "Seagate BUP Slim BL")["status"] == "ok"
    old = drive(report, "A drive Windows called Disk 3")
    assert old["status"] == CRITICAL and not old["connected"]


def test_an_error_between_two_looks_is_filed_when_the_number_held(world):
    watch, windows, clock, _ = world
    watch.check()
    clock.now += disk_health.EVERY
    windows.events = [bad_block(2, clock.now - 120)]
    report = watch.check()
    assert drive(report, "Seagate BUP Slim BL")["problems"][0]["kind"] == "bad_blocks"


def test_a_drive_replugged_between_looks_does_not_inherit_the_errors(world):
    """Disk 3 was the backup drive at one look and a different drive at the
    next; an error in between could have been either, so it is neither's."""
    watch, windows, clock, _ = world
    watch.check()
    clock.now += disk_health.EVERY
    other = {"n": 3, "name": "SanDisk Extreme", "serial": "SD55AE", "bus": "USB",
             "health": "Healthy", "op": "OK"}
    windows.disks = [SYSTEM, LIBRARY, other]
    windows.events = [bad_block(3, clock.now - 120)]
    report = watch.check()
    assert drive(report, "SanDisk Extreme")["status"] == "ok"
    assert drive(report, "A drive Windows called Disk 3")["status"] == CRITICAL


def test_a_file_system_event_is_filed_by_the_serial_it_names(world):
    watch, windows, _, _ = world
    windows.events = [{"r": 5, "p": "Microsoft-Windows-Ntfs", "id": 140, "t": NOW - 86400,
                       "props": ["2", "E:", "23", "\\Device\\HarddiskVolume8",
                                 "-1073741810", "x", "8", "Seagate ", "16", "BUP RD",
                                 "4", "0304", "8", "NA9S8THX"],
                       "msg": "The system failed to flush data to the transaction log."}]
    report = watch.check()
    library = drive(report, "Seagate BUP RD")
    assert library["used_for"] == ["library"]
    assert "unplugged" in library["problems"][0]["advice"]
    # One lost write is what a drive yanked mid-copy leaves: worth a look.
    assert library["status"] == WARNING


def test_a_drive_that_keeps_losing_writes_is_failing(world):
    watch, windows, _, _ = world
    windows.events = [{"r": 20 + n, "p": "Ntfs", "id": 50, "t": NOW - 3600 * n,
                       "props": ["", r"F:\$Extend\$UsnJrnl:$J"], "msg": "Delayed Write Failed"}
                      for n in range(3)]
    assert drive(watch.check(), "Seagate BUP Slim BL")["status"] == CRITICAL


def test_a_usb_reset_is_filed_with_the_disk_that_failed_at_that_moment(world):
    watch, windows, _, _ = world
    windows.events = [
        {"r": 7, "p": "UASPStor", "id": 129, "t": NOW - 60,
         "props": ["\\Device\\RaidPort5"], "msg": "Reset to device was issued."},
        {"r": 8, "p": "disk", "id": 153, "t": NOW - 58,
         "props": ["\\Device\\Harddisk3\\DR5", "0x12", "3"], "msg": "retried"},
    ]
    kinds = {p["kind"] for p in drive(watch.check(), "Seagate BUP Slim BL")["problems"]}
    assert kinds == {"resets", "retried"}


def test_the_same_event_seen_twice_is_counted_once(world):
    watch, windows, clock, _ = world
    windows.events = [bad_block(1, NOW - 60)]
    watch.check()
    clock.now += 60
    report = watch.check()
    assert drive(report, "Seagate BUP Slim BL")["problems"][0]["count"] == 1


def test_later_looks_ask_only_for_what_is_new(world):
    watch, windows, clock, _ = world
    watch.check()
    clock.now += disk_health.EVERY
    watch.check()
    assert windows.asked[0] == disk_health.WINDOW
    assert windows.asked[1] < disk_health.EVERY + 300


# -- what Windows says about health and space ----------------------------------------

def test_a_volume_windows_calls_unhealthy_is_critical(world):
    watch, windows, _, _ = world
    windows.volumes[2] = volume("F", health="Warning")
    assert drive(watch.check(), "Seagate BUP Slim BL")["status"] == CRITICAL


def test_a_nearly_full_library_drive_is_worth_watching(world):
    watch, windows, _, _ = world
    windows.volumes[1] = volume("E", free=10 * 1024 ** 3)
    library = drive(watch.check(), "Seagate BUP RD")
    assert library["status"] == WARNING
    assert library["problems"][0]["kind"] == "low_space"


def test_a_nearly_full_drive_ninaivu_does_not_use_is_not(world):
    watch, windows, _, _ = world
    windows.volumes[2] = volume("F", free=1024 ** 3)
    assert drive(watch.check(), "Seagate BUP Slim BL")["status"] == "ok"


def test_quiet_drives_are_listed_as_healthy(world):
    watch, _, _, _ = world
    report = watch.check()
    assert {d["status"] for d in report["drives"]} == {"ok"}
    assert drive(report, "AirDisk 512GB SSD")["used_for"] == ["Ninaivu's own records"]


# -- telling somebody --------------------------------------------------------------

def test_a_failing_drive_is_reported_once_not_every_ten_minutes(world):
    watch, windows, clock, told = world
    windows.events = [bad_block(1, NOW - 60)]
    watch.check()
    assert len(told) == 1 and told[0][0] == "disk_health"
    assert "Seagate BUP Slim BL may be failing" == told[0][1]
    for _ in range(5):
        clock.now += disk_health.EVERY
        watch.check()
    assert len(told) == 1


def test_it_is_reported_again_only_for_something_new_and_not_within_a_day(world):
    watch, windows, clock, told = world
    windows.events = [bad_block(1, NOW - 60)]
    watch.check()
    clock.now += 2 * 86400                 # a day and more, nothing new
    watch.check()
    assert len(told) == 1
    clock.now += 60
    windows.events = [bad_block(9, clock.now - 30)]
    watch.check()
    assert len(told) == 2


def test_it_is_written_to_the_log_for_the_problem_list(world, caplog):
    import logging
    watch, windows, _, _ = world
    windows.events = [bad_block(1, NOW - 60), bad_block(2, NOW - 50)]
    with caplog.at_level(logging.WARNING, logger="ninaivu.storage.disk_health"):
        watch.check()
    assert any("2 new bad blocks" in r.getMessage() for r in caplog.records)


# -- where it cannot see ---------------------------------------------------------------

def test_off_windows_it_says_it_cannot_see_rather_than_all_is_well(tmp_path):
    db.init_db(tmp_path / "i.db")
    watch = DiskWatch(SimpleNamespace(roots=[], state_dir=""),
                      lambda: db.connect(tmp_path / "i.db"), probe=lambda since: None)
    report = watch.check()
    assert report["supported"] is False and report["drives"] == []


def test_a_probe_that_fails_is_reported_not_raised(world):
    watch, _, _, _ = world

    def broken(since):
        raise RuntimeError("PowerShell is not available")

    watch._probe = broken
    report = watch.check()
    assert "PowerShell" in report["error"]


# -- the console ------------------------------------------------------------------------

def test_the_console_shows_and_checks_the_drives(app, people):
    from conftest import ADMIN, FAMILY, login

    services = app.config["MV_SERVICES"]
    windows = Windows()
    windows.events = [bad_block(1, time.time() - 30)]
    services.disks._probe = windows
    services.disks.cfg = SimpleNamespace(roots=[r"E:\MasterArchive"], state_dir="")
    admin = login(app.test_client(), *ADMIN)
    report = admin.post("/api/admin/disks/check").get_json()
    assert drive(report, "Seagate BUP Slim BL")["status"] == CRITICAL
    assert admin.get("/api/admin/disks").get_json()["checked_at"]
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/admin/disks").status_code in (401, 403, 404)
