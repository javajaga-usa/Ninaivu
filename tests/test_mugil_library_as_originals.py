"""The Secondary keeping the originals in a library folder already on its disk.

The household chooses the library folder itself as the place for the
originals: the Linux PC's own library, or the Primary's library copied onto
the N300 by hand. The folder above it is then the originals folder, so the
library is ``<originals>/<its name>``, the photographs there are checked at
once, and the Primary marks the ones already there as in sync, sending only
what is missing or different. Nothing on the disk is moved, changed or
removed, apart from the Primary's modified times put back.
"""

import os
import shutil
from pathlib import Path

import pytest

import test_mugil_two_computers as two
from ninaivu.mugil import adopt
from ninaivu.storage import library_id

primary, secondary, two_apps = two.primary, two.secondary, two.two_apps
run = two.run


def _wait_for_the_check(app):
    adopter = app.config["MV_SERVICES"].mugil_adopter
    if adopter._thread is not None:
        adopter._thread.join(30)
    assert adopt.state(app.config["MV_CONFIG"]) == adopt.READY, adopter.status()


def _plain(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in folder.rglob("*") if p.is_file() and not p.name.startswith(".")}


def test_the_primarys_library_copied_by_hand_is_used_where_it_is(
        primary, secondary, app, as_admin, scanned, tmp_path):
    _, courier = primary
    cfg, _ = secondary
    root = Path(cfg.roots[0])
    # Copied onto the big disk with the file manager, under a name of its own,
    # its marker (.ninaivu-library) and all; the copy has new modified times.
    disk = tmp_path / "N300"
    copied = disk / "Family Photos"
    shutil.copytree(root, copied)
    for path in copied.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            os.utime(path, (1_700_000_000, 1_700_000_000))
    assert library_id.read_id(copied) == library_id.read_id(root)
    before = _plain(copied)

    answer = as_admin.post("/api/admin/mugil/role",
                           json={"role": "secondary", "originals_dir": str(copied)})
    assert answer.status_code == 200, answer.get_json()
    # The folder above the library keeps the originals; the check starts by itself.
    assert Path(cfg.mugil_originals_dir) == disk.resolve()
    assert cfg.mugil_copy_mode == "manual"
    _wait_for_the_check(app)

    indexed = scanned[1].execute("SELECT COUNT(*) FROM assets WHERE root=? AND trashed=0",
                                 (str(root),)).fetchone()[0]
    result = run(courier)
    assert result["complete"] is True, result
    # Every one already there is in sync; nothing crossed the network.
    assert result["sent"] == 0 and result["bytes"] == 0, result
    assert result["already"] == indexed
    # Kept in the folder it was copied into, not a new one by the Primary's name.
    assert not (disk / root.name).exists()
    assert _plain(copied) == before
    some = next(p for p in root.rglob("*.jpg"))
    kept = copied / some.relative_to(root)
    assert kept.stat().st_mtime == pytest.approx(some.stat().st_mtime, abs=1e-3)
    assert run(courier)["already"] == indexed


def test_what_is_missing_from_the_library_is_sent_into_it(
        primary, secondary, app, as_admin, scanned, tmp_path):
    _, courier = primary
    cfg, _ = secondary
    root = Path(cfg.roots[0])
    copied = tmp_path / "N300" / root.name
    shutil.copytree(root, copied)
    missing = sorted(p for p in copied.rglob("*.jpg"))[0]
    rel = missing.relative_to(copied)
    missing.unlink()
    assert as_admin.post("/api/admin/mugil/role", json={
        "role": "secondary", "originals_dir": str(copied)}).status_code == 200
    _wait_for_the_check(app)
    result = run(courier)
    assert result["complete"] is True, result
    assert result["sent"] == 1, result
    assert (copied / rel).read_bytes() == (root / rel).read_bytes()


def test_its_own_library_chosen_itself_is_asked_about_then_kept(app, as_admin, scanned, tmp_path):
    cfg = app.config["MV_CONFIG"]
    holder = tmp_path / "media" / "jaga" / "N300" / "NinaivuMaster"
    root = holder / "Ninaivu Archive"
    (root / "2026" / "11").mkdir(parents=True)
    (root / "2026" / "11" / "linux.jpg").write_bytes(b"\xff\xd8 photo")
    cfg.add_library(str(root))
    files = _plain(root)

    asked = as_admin.post("/api/admin/mugil/role",
                          json={"role": "secondary", "originals_dir": str(root)})
    assert asked.status_code == 409, asked.get_json()
    said = asked.get_json()
    assert said["library_inside"] == [str(root)]
    assert Path(said["originals_dir"]) == holder.resolve()
    assert str(root) in cfg.roots                     # nothing changed yet

    done = as_admin.post("/api/admin/mugil/role", json={
        "role": "secondary", "originals_dir": str(root), "library_into_originals": True})
    assert done.status_code == 200, done.get_json()
    assert str(root) not in cfg.roots
    assert Path(cfg.mugil_originals_dir) == holder.resolve()
    _wait_for_the_check(app)
    assert app.config["MV_SERVICES"].mugil_adopter.status()["found"]
    assert _plain(root) == files


def test_a_library_that_is_the_whole_disk_says_what_to_do(app, as_admin, tmp_path, monkeypatch):
    disk = tmp_path / "N300"
    disk.mkdir()
    library_id.ensure_marker(disk)
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(disk.resolve()) or real(p))
    answer = as_admin.post("/api/admin/mugil/role",
                           json={"role": "secondary", "originals_dir": str(disk)})
    assert answer.status_code == 400
    assert "whole disk" in answer.get_json()["error"]


def test_an_empty_folder_waits_for_the_household_as_before(app, as_admin, tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    answer = as_admin.post("/api/admin/mugil/role",
                           json={"role": "secondary", "originals_dir": str(folder)})
    assert answer.status_code == 200
    assert app.config["MV_CONFIG"].mugil_copy_mode == "automatic"
