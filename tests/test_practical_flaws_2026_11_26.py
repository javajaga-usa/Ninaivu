"""What went wrong in real use, found on 26 November 2026.

1. A photograph given another date (or moved in Finder) kept the second
   copy's record at its old place, so Restore from this disk put it back
   there: the same photograph twice, the second shown to the family even when
   the first was hidden.
2. A phone backup waiting for an administrator was counted, and told to the
   phone, as safely backed up: it is one copy, in a holding folder.
3. Erase for good said nothing was left to put the files back from.
4. Hidden things went nowhere outside the house unless somebody changed it.
5. Explicit photographs went to Drive whenever hidden ones were held back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.media import date_edit
from ninaivu.server import auth
from ninaivu.storage import db, recycle

ROOT = Path(__file__).resolve().parents[1]


# --- 1. the second copy follows a file that moved ---------------------------

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


def _restore(house):
    assert house["mirror"].restore() == {"started": True}
    house["mirror"].join(30)
    return house["mirror"].status()


def _redate(house, rel: str, day: str):
    conn, root = house["conn"], str(house["library"])
    asset = conn.execute("SELECT id FROM assets WHERE root=? AND rel_path=?",
                         (root, rel)).fetchone()
    moved = date_edit.relocate(conn, asset["id"], date_edit.parse_date(day), [root])
    return moved


def _photos_named(library: Path, name: str) -> list[str]:
    return sorted(p.relative_to(library).as_posix() for p in library.rglob(name)
                  if "_deleted" not in p.parts)


def test_a_redated_photograph_is_put_back_at_its_new_date_only(house):
    _run(house)
    old = "shared/holiday/beach1.jpg"
    moved = _redate(house, old, "2019-07-14")
    new = moved["rel_path"]
    assert new == "2019/07/14/beach1.jpg"
    original = (house["library"] / new).read_bytes()
    status = _run(house)
    assert "Nothing new to copy" in status["message"], "kept where it was on the disk, not copied again"
    (house["library"] / new).unlink()                  # the library disk loses it
    _restore(house)
    assert _photos_named(house["library"], "beach1.jpg") == [new], \
        "put back at its new date, and not at its old place as well"
    assert (house["library"] / new).read_bytes() == original


def test_a_redated_photograph_that_is_there_is_not_brought_back_at_its_old_place(house):
    _run(house)
    moved = _redate(house, "shared/holiday/beach1.jpg", "2019-07-14")
    status = _restore(house)
    assert _photos_named(house["library"], "beach1.jpg") == [moved["rel_path"]]
    assert "Nothing on the second copy is missing" in status["message"]


def test_a_redated_photograph_deleted_afterwards_stays_deleted(house):
    _run(house)
    moved = _redate(house, "shared/holiday/beach1.jpg", "2019-07-14")
    recycle.recycle(house["conn"], [moved["id"]], roots=[str(house["library"])])
    _restore(house)
    assert _photos_named(house["library"], "beach1.jpg") == []


def test_a_move_before_the_first_copy_changes_nothing(house):
    moved = _redate(house, "shared/holiday/beach1.jpg", "2019-07-14")
    status = _run(house)
    assert (house["disk"] / house["library"].name / moved["rel_path"]).is_file()
    assert "Copied" in status["message"]


def test_a_photograph_moved_in_finder_and_known_by_its_fingerprint_follows(house):
    _run(house)
    conn, root = house["conn"], str(house["library"])
    db.respell_asset(conn, root, "shared/holiday/beach1.jpg", "shared/beach1.jpg")
    os.replace(house["library"] / "shared/holiday/beach1.jpg", house["library"] / "shared/beach1.jpg")
    (house["library"] / "shared/beach1.jpg").unlink()
    _restore(house)
    assert _photos_named(house["library"], "beach1.jpg") == ["shared/beach1.jpg"]


def test_the_names_list_says_where_a_moved_file_is_on_the_disk(house):
    _run(house)
    moved = _redate(house, "shared/holiday/beach1.jpg", "2019-07-14")
    _run(house)
    names = (house["disk"] / "NINAIVU-NAMES.txt").read_text(encoding="utf-8")
    lib = house["library"].name
    assert f"{lib}/shared/holiday/beach1.jpg\t{lib}/{moved['rel_path']}" in names


# --- 2. a phone backup waiting for approval -----------------------------------

def test_a_backup_waiting_for_approval_is_not_counted_as_safe():
    source = (ROOT / "ninaivu/media/phone_backup.py").read_text(encoding="utf-8")
    safe = source.split("AS safe,")[0].rsplit("SUM(", 1)[1]
    assert "STAGED" not in safe, "waiting for approval is one copy, in a holding folder"


def test_the_phone_says_to_keep_what_waits_for_approval():
    script = (ROOT / "ninaivu/static/js/phone-backup.js").read_text(encoding="utf-8")
    assert "Keep them on your phone until they are in the library." in script
    for name in ("en", "ta"):
        words = json.loads((ROOT / f"ninaivu/static/i18n/{name}.json").read_text(encoding="utf-8"))
        assert "{count} wait for an administrator. Keep them on your phone until they are in the library." in words


# --- 3. erase for good ----------------------------------------------------------

def test_erasing_for_good_says_which_copies_keep_the_files():
    page = (ROOT / "ninaivu/templates/admin.html").read_text(encoding="utf-8")
    assert "nowhere left to put them back from" not in page
    dialog = page.split('id="purge-modal"')[1].split('id="purge-confirm"')[0]
    assert "second copy" in dialog and "Google Drive" in dialog and "off-site" in dialog


# --- 4 and 5. what goes to Drive ------------------------------------------------

def test_hidden_things_go_to_drive_while_it_is_encrypted_by_default():
    from ninaivu.server.config import Config
    assert Config.cloud_hidden == "encrypted"


def test_explicit_photographs_are_held_back_like_hidden_ones(scanned):
    from ninaivu.cloud import service as cloud_service

    cfg, conn, _ = scanned
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET nsfw=1 WHERE id=?", (row["id"],))
    conn.commit()
    level = cloud_service.visibility_for_backup(conn, row["root"], row["rel_path"])
    assert level is not None and level >= 2
