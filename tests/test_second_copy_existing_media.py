"""A second copy made in a folder that already holds the photographs.

In a single-computer setup the household often chooses a backup folder or a
network share that another backup filled long ago. Nothing there is to be
overwritten, renamed or moved: the same bytes already there are taken as the
copy, wherever they are on the disk, and a different file at the library's
own place is left alone, with the library's going beside it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.server import auth
from ninaivu.storage import mirror as mirror_mod


@pytest.fixture()
def house(scanned, tmp_path: Path):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    disk = tmp_path / "backup-share"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    login(create_admin_app(services).test_client(), *ADMIN)
    return {"conn": conn, "mirror": services.mirror, "disk": disk,
            "library": Path(cfg.active_root)}


def _run(house):
    assert house["mirror"].start() == {"started": True}
    house["mirror"].join(30)
    status = house["mirror"].status()
    assert not status["error"], status["error"]
    return status


def _row(house, rel):
    return house["conn"].execute(
        "SELECT stored_as, origin, size FROM mirror_copies WHERE rel_path=?", (rel,)).fetchone()


REL = "misc/plain.png"


def test_the_same_photograph_at_its_own_place_is_left_untouched(house):
    there = house["disk"] / REL
    there.parent.mkdir(parents=True)
    shutil.copyfile(house["library"] / REL, there)
    os.utime(there, (1_000_000_000, 1_000_000_000))      # another backup's time
    inode = there.stat().st_ino
    status = _run(house)
    assert there.stat().st_ino == inode and there.stat().st_mtime == 1_000_000_000
    assert _row(house, REL)["origin"] == mirror_mod.FOUND
    assert status["already_there"] >= 1 and "already on the disk" in status["message"]
    assert not list(there.parent.glob("plain (*"))


def test_the_same_photograph_elsewhere_on_the_disk_is_not_written_again(house):
    # A backup laid out its own way: by year, under another top folder.
    there = house["disk"] / "Old backup" / "2019" / "IMG_0001.png"
    there.parent.mkdir(parents=True)
    shutil.copyfile(house["library"] / REL, there)
    status = _run(house)
    assert not (house["disk"] / REL).exists()
    row = _row(house, REL)
    assert row["origin"] == mirror_mod.FOUND and row["stored_as"] == "Old backup/2019/IMG_0001.png"
    assert there.read_bytes() == (house["library"] / REL).read_bytes()
    assert status["owed"] == 0
    names = (house["disk"] / mirror_mod.NAMES).read_text(encoding="utf-8")
    assert f"Old backup/2019/IMG_0001.png\t{REL}" in names
    # The copy reads back from where it is, and the library can be put back from it.
    assert house["mirror"].copy_path(str(house["library"]), REL) == there


def test_a_different_file_at_the_librarys_place_is_kept_and_the_copy_goes_beside(house):
    there = house["disk"] / REL
    there.parent.mkdir(parents=True)
    theirs = b"someone else's picture, not this one"
    there.write_bytes(theirs)
    _run(house)
    assert there.read_bytes() == theirs, "never overwritten"
    assert not list(there.parent.glob("plain (before *"))
    row = _row(house, REL)
    assert row["origin"] == mirror_mod.COPIED and row["stored_as"]
    beside = house["disk"] / row["stored_as"]
    assert beside.name == "plain (2).png"
    assert beside.read_bytes() == (house["library"] / REL).read_bytes()
    # And again: nothing new, nothing moved.
    assert _run(house)["copied_this_run"] == 0
    assert there.read_bytes() == theirs


def test_a_found_file_is_never_set_aside_when_the_library_one_changes(house):
    there = house["disk"] / REL
    there.parent.mkdir(parents=True)
    shutil.copyfile(house["library"] / REL, there)
    original = there.read_bytes()
    _run(house)
    # The library's file is edited; the one that was on the disk is not Ninaivu's.
    source = house["library"] / REL
    source.write_bytes(original + b"edited")
    st = source.stat()
    house["conn"].execute("UPDATE assets SET size=?, mtime=? WHERE rel_path=?",
                          (st.st_size, st.st_mtime, REL))
    house["conn"].commit()
    _run(house)
    assert there.read_bytes() == original
    assert not list(there.parent.glob("plain (before *"))
    row = _row(house, REL)
    assert row["origin"] == mirror_mod.COPIED
    assert (house["disk"] / row["stored_as"]).read_bytes() == original + b"edited"


def test_a_found_file_changed_by_its_owner_is_copied_again_beside_it(house):
    there = house["disk"] / REL
    there.parent.mkdir(parents=True)
    shutil.copyfile(house["library"] / REL, there)
    _run(house)
    there.write_bytes(b"retouched by hand")
    house["mirror"].verify()
    house["mirror"].join(30)
    assert _row(house, REL) is None
    _run(house)
    assert there.read_bytes() == b"retouched by hand"
    row = _row(house, REL)
    assert (house["disk"] / row["stored_as"]).read_bytes() == (house["library"] / REL).read_bytes()


def test_a_file_put_there_while_one_is_written_is_not_overwritten(house, monkeypatch):
    there = house["disk"] / REL
    real = mirror_mod.publish_no_replace

    def someone_else_writes_first(src, dst):
        if Path(dst) == there and not there.exists():
            there.write_bytes(b"arrived meanwhile")
        return real(src, dst)

    monkeypatch.setattr(mirror_mod, "publish_no_replace", someone_else_writes_first)
    _run(house)
    assert there.read_bytes() == b"arrived meanwhile"
    row = _row(house, REL)
    assert row["stored_as"].endswith("plain (2).png")
    assert (house["disk"] / row["stored_as"]).read_bytes() == (house["library"] / REL).read_bytes()
    assert not list(house["disk"].rglob("*.ninaivu-part"))


def test_the_disk_is_looked_over_once(house, monkeypatch):
    walks = []
    real = os.walk
    monkeypatch.setattr(mirror_mod.os, "walk", lambda *a, **k: walks.append(a) or real(*a, **k))
    _run(house)
    _run(house)
    assert len(walks) == 1


def test_ninaivus_own_files_and_the_disks_folders_are_not_taken_as_found(house):
    hidden = house["disk"] / ".Trashes" / "plain.png"
    hidden.parent.mkdir()
    shutil.copyfile(house["library"] / REL, hidden)
    _run(house)
    row = _row(house, REL)
    assert row["origin"] == mirror_mod.COPIED and row["stored_as"] == ""
