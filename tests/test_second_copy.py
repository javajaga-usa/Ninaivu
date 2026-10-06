"""A second copy of the library on another disk.

The promises worth pinning down: it only goes out, nothing on the copy is
overwritten or removed, a file is whole or absent, and a different disk at the
same path is not mistaken for the one the record was made against.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.server import auth
from ninaivu.storage import mirror as mirror_mod


@pytest.fixture()
def house(scanned, tmp_path: Path):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    disk = tmp_path / "other-disk"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    client = login(create_admin_app(services).test_client(), *ADMIN)
    return {"cfg": cfg, "conn": conn, "services": services, "mirror": services.mirror,
            "disk": disk, "client": client, "library": Path(cfg.active_root)}


def _run(house):
    assert house["mirror"].start() == {"started": True}
    house["mirror"].join(30)
    return house["mirror"].status()


def _files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*"))
            if p.is_file() and p.name != mirror_mod.MARKER}


def test_the_copy_is_the_library_as_plain_files_in_their_folders(house):
    status = _run(house)
    assert status["owed"] == 0 and status["copied"] == status["files"] > 0
    assert not status["error"] and status["last_finished"] > 0
    copy = house["disk"] / house["library"].name
    indexed = {r["rel_path"] for r in house["conn"].execute("SELECT rel_path FROM assets")}
    assert set(_files(copy)) == indexed
    for rel, data in _files(copy).items():
        source = house["library"] / rel
        assert data == source.read_bytes()
        assert abs((copy / rel).stat().st_mtime - source.stat().st_mtime) < 1
    row = house["conn"].execute(
        "SELECT sha256 FROM mirror_copies WHERE rel_path='misc/plain.png'").fetchone()
    assert row["sha256"] == hashlib.sha256((copy / "misc/plain.png").read_bytes()).hexdigest()
    assert not list(house["disk"].rglob("*.ninaivu-part"))


def test_a_second_run_copies_nothing(house):
    _run(house)
    before = {p: p.stat().st_mtime_ns for p in house["disk"].rglob("*") if p.is_file()}
    status = _run(house)
    assert status["copied_this_run"] == 0 and "Nothing new" in status["message"]
    assert {p: p.stat().st_mtime_ns for p in house["disk"].rglob("*") if p.is_file()} == before


def test_a_photograph_deleted_from_the_library_stays_on_the_copy(house):
    _run(house)
    rel = "misc/plain.png"
    (house["library"] / rel).unlink()
    house["conn"].execute("DELETE FROM assets WHERE rel_path=?", (rel,))
    house["conn"].commit()
    _run(house)
    assert (house["disk"] / house["library"].name / rel).is_file()


def test_a_changed_file_is_copied_beside_the_older_one(house):
    _run(house)
    rel = "misc/plain.png"
    copy = house["disk"] / house["library"].name / rel
    old = copy.read_bytes()
    source = house["library"] / rel
    source.write_bytes(b"a different picture entirely")
    stat = source.stat()
    house["conn"].execute("UPDATE assets SET size=?, mtime=? WHERE rel_path=?",
                          (stat.st_size, stat.st_mtime, rel))
    house["conn"].commit()
    assert _run(house)["copied_this_run"] == 1
    assert copy.read_bytes() == b"a different picture entirely"
    kept = list(copy.parent.glob("plain (before *).png"))
    assert len(kept) == 1 and kept[0].read_bytes() == old


def test_a_file_whose_time_moved_but_not_its_bytes_is_not_kept_twice(house):
    _run(house)
    rel = "misc/plain.png"
    source = house["library"] / rel
    os.utime(source, (1_700_000_000, 1_700_000_000))
    house["conn"].execute("UPDATE assets SET mtime=? WHERE rel_path=?", (1_700_000_000, rel))
    house["conn"].commit()
    _run(house)
    assert not list((house["disk"] / house["library"].name / "misc").glob("plain (before*"))


def test_a_different_disk_at_the_same_path_is_copied_to_from_the_beginning(house):
    _run(house)
    import shutil
    shutil.rmtree(house["disk"])
    house["disk"].mkdir()                       # the other drive, mounted where this one was
    status = _run(house)                        # …and is not believed
    assert status["copied_this_run"] == status["files"]
    assert (house["disk"] / house["library"].name / "misc/plain.png").is_file()


def test_the_marker_says_what_the_disk_is(house):
    _run(house)
    marker = json.loads((house["disk"] / mirror_mod.MARKER).read_text())
    assert marker["id"] and "ordinary files" in marker["note"]


def test_a_full_disk_stops_it_and_says_so(house, monkeypatch):
    from collections import namedtuple
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(mirror_mod.shutil, "disk_usage", lambda path: usage(10, 10, 5))
    status = _run(house)
    assert "full" in status["error"] and status["copied_this_run"] == 0
    assert _files(house["disk"]) == {}


def test_it_makes_way_for_the_household_and_stops_when_asked(house):
    mirror = house["mirror"]
    mirror._hold = lambda: "somebody is using Ninaivu"           # noqa: SLF001
    assert mirror.start() == {"started": True}
    import time
    for _ in range(100):
        if mirror.status()["waiting"]:
            break
        time.sleep(0.02)
    assert mirror.status()["waiting"] == "somebody is using Ninaivu"
    assert mirror.due() is False
    mirror.stop(join=True)
    status = mirror.status()
    assert status["running"] is False and "Stopped" in status["message"]


def test_it_is_owed_when_the_disk_is_there_and_time_has_passed(house):
    mirror = house["mirror"]
    assert mirror.due() is True
    _run(house)
    assert mirror.due() is False
    mirror._clock = lambda: __import__("time").time() + 25 * 3600   # noqa: SLF001
    assert mirror.due() is True
    house["cfg"].mirror_dir = str(house["disk"] / "unplugged")
    assert mirror.due() is False and "not there" in mirror.status()["problem"]
    house["cfg"].mirror_dir, house["cfg"].mirror_enabled = str(house["disk"]), False
    assert mirror.due() is False


@pytest.mark.parametrize("where,word", [
    ("", "Choose"), ("relative/path", "whole path"), ("{lib}", "already uses"),
    ("{lib}/misc", "already uses"), ("{parent}", "contains"), ("{state}", "already uses"),
    ("{tmp}/nowhere", "plugged in"),
])
def test_a_folder_that_cannot_hold_the_copy_is_refused(house, tmp_path, where, word):
    folder = where.format(lib=house["library"], parent=house["library"].parent,
                          state=house["cfg"].state_dir, tmp=tmp_path)
    answer = house["client"].post("/api/mirror/settings", json={"folder": folder, "enabled": True})
    assert answer.status_code == 400 and word in answer.get_json()["error"], answer.get_json()


def test_the_console_sets_it_up_and_runs_it(house, tmp_path):
    client, cfg = house["client"], house["cfg"]
    cfg.mirror_dir, cfg.mirror_enabled = "", False
    assert client.post("/api/mirror/start").status_code == 409
    saved = client.post("/api/mirror/settings", json={
        "folder": str(house["disk"]), "enabled": True, "every_hours": 168}).get_json()
    assert saved["enabled"] is True and saved["every_hours"] == 168 and saved["owed"] > 0
    stored = json.loads(cfg.config_path.read_text())
    assert stored["mirror_dir"] == str(house["disk"]) and stored["mirror_enabled"] is True
    assert client.post("/api/mirror/start").get_json()["started"] is True
    house["mirror"].join(30)
    assert client.get("/api/mirror/status").get_json()["owed"] == 0
    for bad in ({"every_hours": -1}, {"every_hours": "daily"}, {"enabled": "yes"}, {"nope": 1}):
        assert client.post("/api/mirror/settings", json=bad).status_code == 400


def test_the_family_app_cannot_reach_it(house):
    home = login(create_home_app(house["services"]).test_client(), *ADMIN)
    assert home.get("/api/mirror/status").status_code == 404
    assert home.post("/api/mirror/start").status_code == 404


# --- checking the copy, and bringing it back ----------------------------------

def _wait(house):
    house["mirror"].join(30)
    return house["mirror"].status()


def test_a_check_finds_a_file_that_went_bad_and_the_next_run_mends_it(house):
    _run(house)
    copy = house["disk"] / house["library"].name / "misc/plain.png"
    good = copy.read_bytes()
    copy.write_bytes(b"\0" * len(good))              # a disk going bad in a drawer
    assert house["mirror"].verify() == {"started": True}
    status = _wait(house)
    assert status["verify_bad"] == 1 and status["verify_missing"] == 0 and status["last_verified"]
    assert "no longer matched" in status["message"]
    _run(house)
    assert copy.read_bytes() == good
    assert len(list(copy.parent.glob("plain (before *).png"))) == 1, "the bad one is kept aside"


def test_a_clean_check_says_so(house):
    _run(house)
    house["mirror"].verify()
    status = _wait(house)
    assert status["verify_bad"] == 0 and "intact" in status["message"]


def test_restoring_puts_back_what_the_library_lost_and_nothing_else(house):
    _run(house)
    lost = house["library"] / "shared/holiday/beach1.jpg"
    kept = house["library"] / "misc/plain.png"
    original = lost.read_bytes()
    lost.unlink()
    kept.write_bytes(b"changed here on purpose")
    assert house["mirror"].restore() == {"started": True}
    status = _wait(house)
    assert lost.read_bytes() == original
    assert kept.read_bytes() == b"changed here on purpose", "a file that is there is left alone"
    assert "Put back 1" in status["message"]


def test_restoring_one_folder_only(house):
    _run(house)
    one = house["library"] / "shared/holiday/beach1.jpg"
    other = house["library"] / "misc/plain.png"
    one.unlink()
    other.unlink()
    house["mirror"].restore("shared")
    _wait(house)
    assert one.is_file() and not other.exists()


def test_a_copy_that_does_not_match_its_fingerprint_is_not_put_back(house):
    _run(house)
    lost = house["library"] / "misc/plain.png"
    lost.unlink()
    (house["disk"] / house["library"].name / "misc/plain.png").write_bytes(b"rotten")
    house["mirror"].restore()
    status = _wait(house)
    assert not lost.exists() and "did not match" in status["message"]


def test_the_console_checks_and_restores(house):
    client = house["client"]
    _run(house)
    assert client.post("/api/mirror/verify").get_json()["started"] is True
    _wait(house)
    (house["library"] / "misc/plain.png").unlink()
    assert client.post("/api/mirror/restore", json={"folder": "misc"}).get_json()["started"] is True
    _wait(house)
    assert (house["library"] / "misc/plain.png").is_file()
    assert client.post("/api/mirror/restore", json={"folder": 7}).status_code == 400


def test_restoring_a_folder_with_an_underscore_leaves_its_neighbours(house):
    # Hearth took the underscore out of the folder's name before matching, so
    # "my_trip" brought back nothing; unescaped, it would also match "myXtrip".
    from ninaivu.media.scanner import Scanner
    library = house["library"]
    photo = (library / "shared/holiday/beach1.jpg").read_bytes()
    mine, neighbour = library / "my_trip/a.jpg", library / "myXtrip/b.jpg"
    for n, path in enumerate((mine, neighbour)):
        path.parent.mkdir()
        path.write_bytes(photo + bytes([n]))
    Scanner(house["cfg"])._run(library, full=True)                # noqa: SLF001
    _run(house)
    mine.unlink()
    neighbour.unlink()
    house["mirror"].restore("my_trip")
    _wait(house)
    assert mine.is_file() and not neighbour.exists()


def test_stopping_one_run_leaves_the_schedule_running(house):
    # In Hearth, stopping a run from the console also ended the schedule, so
    # nothing was copied by itself again until Ninaivu restarted.
    mirror = house["mirror"]
    mirror.keep()
    mirror.stop()
    assert mirror._keeper.is_alive()                              # noqa: SLF001
    mirror.stop(join=True)
    mirror._keeper.join(5)                                        # noqa: SLF001
    assert not mirror._keeper.is_alive()                          # noqa: SLF001
