"""Drive notice audit, 2026-10-10: which drives are asked about, and how a
"don't ask again" is remembered."""

from types import SimpleNamespace

import pytest

from ninaivu.api import drives_api
from ninaivu.utils import drives


def _cfg(tmp_path, **extra):
    base = {"roots": [str(tmp_path / "lib")], "state_dir": str(tmp_path / "state"),
            "mirror_dir": "", "backup_dir": ""}
    return SimpleNamespace(**{**base, **extra})


def test_the_second_copy_disk_is_not_offered_as_an_import(tmp_path):
    disk = tmp_path / "BackupDisk"
    (disk / "Ninaivu second copy").mkdir(parents=True)
    d = drives.Drive("id1", str(disk), "BackupDisk", 10**12, 10**11)
    cfg = _cfg(tmp_path, mirror_dir=str(disk / "Ninaivu second copy"))
    assert drives_api.holds_library(d, cfg) is True


def test_the_backup_disk_is_not_offered_as_an_import(tmp_path):
    disk = tmp_path / "Backups"
    (disk / "ninaivu-backups").mkdir(parents=True)
    d = drives.Drive("id1", str(disk), "Backups", 10**12, 10**11)
    cfg = _cfg(tmp_path, backup_dir=str(disk / "ninaivu-backups"))
    assert drives_api.holds_library(d, cfg) is True


def test_an_offsite_folder_on_the_disk_counts_but_a_bucket_name_does_not(tmp_path):
    disk = tmp_path / "Grandma"
    (disk / "ours").mkdir(parents=True)
    d = drives.Drive("id1", str(disk), "Grandma", 10**12, 10**11)
    folder = _cfg(tmp_path, offsite_kind="folder", offsite_folder=str(disk / "ours"))
    bucket = _cfg(tmp_path, offsite_kind="s3", offsite_folder=str(disk / "ours"))
    assert drives_api.holds_library(d, folder) is True
    assert drives_api.holds_library(d, bucket) is False


def test_an_unrelated_stick_is_still_asked_about(tmp_path):
    disk = tmp_path / "SANDISK"
    disk.mkdir()
    d = drives.Drive("id1", str(disk), "SANDISK", 64 * 10**9, 10**9)
    cfg = _cfg(tmp_path, mirror_dir=str(tmp_path / "elsewhere" / "copy"),
               backup_dir=str(tmp_path / "elsewhere" / "backups"))
    assert drives_api.holds_library(d, cfg) is False


def test_dont_ask_about_one_phone_does_not_quiet_another_of_the_same_model():
    p1 = drives.Drive("idP", "usb:111", "iPhone", 0, 0, kind="phone", readable=False)
    p2 = drives.Drive("idQ", "usb:222", "iPhone", 0, 0, kind="phone", readable=False)
    again = drives.Drive("idR", "usb:111", "iPhone", 0, 0, kind="phone", readable=False)
    assert p1.remember_key != p2.remember_key
    assert p1.remember_key == again.remember_key          # the same phone, plugged in again


def test_a_disks_key_is_as_it_was(tmp_path):
    # Saved "don't ask" answers for disks keep working after the update.
    d = drives.Drive("idA", "/media/u/SANDISK", "SANDISK", 64_000_000_000, 1)
    assert d.remember_key == "drive|SANDISK|64000000000"


@pytest.fixture
def _no_verdicts(monkeypatch):
    monkeypatch.setattr(drives, "_mac_verdicts", {})
    monkeypatch.setattr(drives, "_mac_guessed", {})


def test_a_failed_diskutil_is_asked_again_rather_than_kept(monkeypatch, _no_verdicts):
    answers = [None, {"Internal": True, "BusProtocol": "PCI-Express"}]
    calls = []

    def diskutil(path):
        calls.append(path)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    clock = {"now": 1000.0}
    monkeypatch.setattr(drives, "_diskutil_info", diskutil)
    monkeypatch.setattr(drives, "_is_time_machine", lambda _p: False)
    monkeypatch.setattr(drives.time, "monotonic", lambda: clock["now"])
    # diskutil timed out: a guess, "carried in", for now.
    assert drives._mac_volume_is_carried("/Volumes/Data", 5) is True
    assert drives._mac_volume_is_carried("/Volumes/Data", 5) is True
    assert len(calls) == 1                                  # not asked every poll
    clock["now"] += drives.MAC_RETRY_SECONDS + 1
    # Asked again: an internal disk, never offered; and that answer is kept.
    assert drives._mac_volume_is_carried("/Volumes/Data", 5) is False
    clock["now"] += drives.MAC_RETRY_SECONDS * 10
    assert drives._mac_volume_is_carried("/Volumes/Data", 5) is False
    assert len(calls) == 2
