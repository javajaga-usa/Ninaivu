"""The second copy has the library's own layout.

The folder chosen for the copy stands for the library folder: 2024/05/01/a.jpg
in the library is 2024/05/01/a.jpg on the copy. A folder that already holds
the library laid out so (copied there by hand, or by another program, onto a
share) is recognised before anything is copied, and the first run takes each
file with the same bytes as it is, writing nothing. A check with nothing on
the copy to read does not say every file matched.
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
    disk = tmp_path / "share" / "NinaivuMaster" / "Ninaivu Archive"
    disk.mkdir(parents=True)
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    client = create_admin_app(services).test_client()
    login(client, *ADMIN)
    return {"conn": conn, "mirror": services.mirror, "disk": disk, "client": client,
            "library": Path(cfg.active_root), "cfg": cfg}


def _go(house, start):
    assert start() == {"started": True}
    house["mirror"].join(30)
    status = house["mirror"].status()
    assert not status["error"], status["error"]
    return status


def _indexed(house) -> list[str]:
    return [r[0] for r in house["conn"].execute(
        "SELECT rel_path FROM assets WHERE trashed=0 ORDER BY rel_path")]


def _copy_by_hand(house, leave_out: str = "") -> dict[str, int]:
    """The library copied onto the disk at its own paths, with other times."""
    inodes = {}
    for rel in _indexed(house):
        if rel == leave_out:
            continue
        there = house["disk"] / rel
        there.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(house["library"] / rel, there)
        os.utime(there, (1_000_000_000, 1_000_000_000))
        inodes[rel] = there.stat().st_ino
    return inodes


def test_a_new_copy_has_the_library_layout(house):
    status = _go(house, house["mirror"].start)
    assert status["owed"] == 0 and status["layout"] == "flat"
    for rel in _indexed(house):
        assert (house["disk"] / rel).read_bytes() == (house["library"] / rel).read_bytes()
    assert not (house["disk"] / house["library"].name).exists()


def test_a_library_already_there_is_seen_and_taken_without_writing(house, monkeypatch):
    missing = "misc/plain.png"
    inodes = _copy_by_hand(house, leave_out=missing)
    before = house["mirror"].status()
    assert before["copied"] == 0
    here = before["library_here"]
    assert here and here["found"] > 0 and here["looked_at"] >= here["found"]
    about = house["client"].get("/api/mirror/folder").get_json()
    assert about["holds"] == "library" and about["library_here"]["found"] == here["found"]

    written = []
    real = mirror_mod.create_new
    monkeypatch.setattr(mirror_mod, "create_new", lambda p: written.append(p) or real(p))
    status = _go(house, house["mirror"].start)
    files = len(_indexed(house))
    assert status["copied"] == files and status["owed"] == 0
    assert status["already_there"] == files - 1
    assert [Path(p).name for p in written] == [Path(missing).name + ".ninaivu-part"]
    assert (house["disk"] / missing).read_bytes() == (house["library"] / missing).read_bytes()
    for rel, inode in inodes.items():
        there = house["disk"] / rel
        assert there.stat().st_ino == inode and there.stat().st_mtime == 1_000_000_000
    assert status["library_here"] is None
    assert not list(house["disk"].rglob("* (2).*"))


def test_a_copy_made_the_old_way_keeps_its_layout(house):
    house["mirror"]._flat_root = None                                     # noqa: SLF001
    conn = house["mirror"]._db()                                          # noqa: SLF001
    status = _go(house, house["mirror"].start)
    assert status["layout"] == "flat"
    # A copy whose record was made before the layout was kept.
    mirror_mod.Mirror._set_meta(conn, "layout", "")                       # noqa: SLF001
    mirror_mod.Mirror._set_meta(conn, "flat_root", "")                    # noqa: SLF001
    house["mirror"]._flat_root = None                                     # noqa: SLF001
    house["mirror"]._choose_layout(conn, house["disk"])                   # noqa: SLF001
    assert mirror_mod.Mirror._meta(conn, "layout") == "nested"            # noqa: SLF001
    assert house["mirror"]._library_folder(str(house["library"])) == house["library"].name  # noqa: SLF001


def test_a_check_of_an_empty_copy_does_not_say_every_file_matched(house):
    status = _go(house, house["mirror"].verify)
    assert status["last_verified"] == 0 and status["verify_checked"] == 0
    assert "Nothing is on the second copy yet" in status["message"]


def test_a_new_folder_forgets_what_was_said_of_the_old_one(house, tmp_path):
    _go(house, house["mirror"].start)
    _go(house, house["mirror"].verify)
    assert house["mirror"].status()["last_verified"] > 0
    other = tmp_path / "other"
    other.mkdir()
    house["cfg"].mirror_dir = str(other)
    house["mirror"].moved_to(str(other))
    status = house["mirror"].status()
    assert status["last_verified"] == 0 and status["last_finished"] == 0
    assert status["copied"] == 0 and status["layout"] == ""


def test_the_page_says_the_library_is_there_and_hides_an_empty_check():
    script = (Path(__file__).resolve().parents[1] / "ninaivu/static/js/second-copy.js").read_text(
        encoding="utf-8")
    assert "status.library_here" in script and "about.holds === 'library'" in script
    assert "status.verify_checked !== 0" in script


@pytest.mark.parametrize("lang", ["en", "ta"])
def test_the_new_words_are_in_both_languages(lang):
    import json
    words = json.loads((Path(__file__).resolve().parents[1]
                        / f"ninaivu/static/i18n/{lang}.json").read_text(encoding="utf-8"))
    assert any(k.startswith("This folder already holds the library, in the same folders")
               for k in words)
