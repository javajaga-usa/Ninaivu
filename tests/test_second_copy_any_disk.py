"""The second copy on any kind of disk (storage/mirror.py with storage/fit.py).

A library on a Mac or Linux disk holds names an exFAT, NTFS or FAT32 drive or
a Samba share refuses. On such a disk those files go under names it keeps,
the record and a plain names list say where, and restoring puts them back
under their own names. A file larger than the disk can hold at all is left
out and said so, not tried and failed on every run. On a disk that keeps
every name, nothing changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.server import auth
from ninaivu.storage import fit, mirror as mirror_mod


@pytest.fixture()
def house(scanned, tmp_path: Path):
    if sys.platform == "win32":
        pytest.skip("a library made on a Mac or Linux disk: Windows cannot hold these names")
    cfg, conn, scanner = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    library = Path(cfg.active_root)
    (library / "trips").mkdir(exist_ok=True)
    (library / "trips" / "Diwali: day 2.jpg").write_bytes(b"lamps")
    (library / "trips" / "what?.jpg").write_bytes(b"a question")
    # The name the first one would be given, already a file of its own.
    (library / "trips" / "Diwali day 2.jpg").write_bytes(b"the other one")
    scanner._run(library, full=True)
    services = build_services(cfg)
    services.scanner.stop()
    disk = tmp_path / "pendrive"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    client = login(create_admin_app(services).test_client(), *ADMIN)
    return {"cfg": cfg, "conn": conn, "mirror": services.mirror, "disk": disk,
            "client": client, "library": library}


def _run(house, fstype):
    house["mirror"].fstype = fstype
    assert house["mirror"].start() == {"started": True}
    house["mirror"].join(30)
    return house["mirror"].status()


def _indexed(house, rel):
    return house["conn"].execute("SELECT 1 FROM assets WHERE rel_path=?", (rel,)).fetchone()


def test_names_an_exfat_disk_refuses_go_under_names_it_keeps(house):
    if not _indexed(house, "trips/Diwali: day 2.jpg"):
        pytest.skip("this system's own disk cannot hold the name either")
    status = _run(house, "exfat")
    assert not status["error"] and status["owed"] == 0 and status["filesystem"] == "exfat"
    copy = house["disk"] / house["library"].name / "trips"
    names = sorted(p.name for p in copy.iterdir())
    assert not [n for n in names if ":" in n or "?" in n], names
    assert (copy / "Diwali day 2.jpg").read_bytes() == b"the other one", \
        "the library's own file of that name is not overwritten"
    assert (copy / "Diwali day 2 (2).jpg").read_bytes() == b"lamps"
    assert (copy / "what.jpg").read_bytes() == b"a question"
    stored = dict(tuple(r) for r in house["conn"].execute(
        "SELECT rel_path, stored_as FROM mirror_copies WHERE stored_as != ''"))
    lib = house["library"].name
    assert stored == {"trips/Diwali: day 2.jpg": f"{lib}/trips/Diwali day 2 (2).jpg",
                      "trips/what?.jpg": f"{lib}/trips/what.jpg"}
    assert status["renamed"] == 2


def test_the_names_list_says_where_each_one_is(house):
    if not _indexed(house, "trips/what?.jpg"):
        pytest.skip("this system's own disk cannot hold the name either")
    _run(house, "ntfs")
    lib = house["library"].name
    text = (house["disk"] / mirror_mod.NAMES).read_text(encoding="utf-8")
    assert f"{lib}/trips/what.jpg\t{lib}/trips/what?.jpg" in text.splitlines()
    assert all(line.startswith("#") or "\t" in line for line in text.splitlines() if line)


def test_a_second_run_copies_nothing_again(house):
    _run(house, "exfat")
    before = {p: p.stat().st_mtime_ns for p in house["disk"].rglob("*") if p.is_file()}
    status = _run(house, "exfat")
    assert status["copied_this_run"] == 0 and "Nothing new" in status["message"]
    assert {p: p.stat().st_mtime_ns for p in house["disk"].rglob("*") if p.is_file()} == before


def test_restoring_puts_them_back_under_their_own_names(house):
    rel = "trips/what?.jpg"
    if not _indexed(house, rel):
        pytest.skip("this system's own disk cannot hold the name either")
    _run(house, "smbfs")
    (house["library"] / rel).unlink()
    assert house["mirror"].restore() == {"started": True}
    house["mirror"].join(30)
    assert (house["library"] / rel).read_bytes() == b"a question"


def test_a_check_reads_the_renamed_files_where_they_are(house):
    _run(house, "exfat")
    assert house["mirror"].verify() == {"started": True}
    house["mirror"].join(30)
    status = house["mirror"].status()
    assert status["verify_bad"] == 0 and status["verify_missing"] == 0


def test_a_disk_that_keeps_every_name_is_written_as_before(house):
    status = _run(house, "ext4")
    assert status["renamed"] == 0 and status["owed"] == 0
    assert not (house["disk"] / mirror_mod.NAMES).exists()
    if _indexed(house, "trips/what?.jpg"):
        assert (house["disk"] / house["library"].name / "trips" / "what?.jpg").is_file()


def test_a_file_too_large_for_fat32_is_left_out_and_said(house, monkeypatch):
    monkeypatch.setitem(fit.FAMILIES, "fat", fit.Family("fat", True, True, True, True, 4, True))
    status = _run(house, "vfat")
    assert not status["error"]
    assert status["too_large"] > 0 and status["too_large_examples"]
    assert "too large" in status["message"]
    assert status["owed"] == status["too_large"]
    small = house["conn"].execute("SELECT COUNT(*) FROM assets WHERE size <= 4 AND trashed=0"
                                  ).fetchone()[0]
    assert status["copied"] == small
    # Not tried again and failed: the next run says the same, with no error.
    again = _run(house, "vfat")
    assert again["too_large"] == status["too_large"] and not again["error"]


def test_the_console_status_carries_it(house):
    _run(house, "exfat")
    got = house["client"].get("/api/mirror/status").get_json()
    assert got["filesystem"] == "exfat" and "renamed" in got and "too_large" in got


def test_fitting_path_leaves_good_names_alone():
    exfat = fit.family_of("exfat")
    assert fit.fitting_path("2024/01/a.jpg", exfat) == "2024/01/a.jpg"
    assert fit.fitting_path("Diwali: day 2/a?.jpg", exfat) == "Diwali day 2/a.jpg"
    assert fit.fitting_path("trip./con.jpg", exfat) == "trip/_con.jpg"
    assert fit.fitting_path("a:b/c.jpg", fit.family_of("ext4")) == "a:b/c.jpg"
    assert fit.too_large(5 * 1024 ** 3, fit.family_of("vfat"))
    assert not fit.too_large(5 * 1024 ** 3, fit.family_of("exfat"))
