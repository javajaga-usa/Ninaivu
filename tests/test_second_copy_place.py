"""Where the second copy is kept, and what is on it (storage/mirror_place.py).

The folder is described before it is chosen; a share that has gone, or one
whose disk dropped behind it, is never written to; the Folders screen lists
the copy, opens a file on it, moves between it and the library, and deletes a
file on it only after the admin's password, a library file then being copied
again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.server import auth
from ninaivu.storage import mirror as mirror_mod, mirror_place


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


def _as_share(monkeypatch, mount: Path):
    monkeypatch.setattr(mirror_place, "mount_of", lambda path: (str(mount), "smbfs"))


# -- describing a folder ---------------------------------------------------

def test_a_folder_is_described_before_it_is_chosen(house, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    about = house["client"].get(f"/api/mirror/folder?path={other}").get_json()
    assert about["there"] and about["writable"] and about["problem"] is None
    assert about["free_bytes"] > 0 and about["total_bytes"] >= about["free_bytes"]
    assert about["needed_bytes"] > 0 and about["holds"] == ""
    # tmp_path is one disk: the library's too.
    assert about["same_disk_as"] == str(house["library"])


def test_a_folder_inside_the_library_is_said_to_be(house):
    inside = house["library"] / "misc"
    about = house["client"].get(f"/api/mirror/folder?path={inside}").get_json()
    assert "inside" in about["problem"]


def test_a_folder_that_holds_this_copy_says_so(house):
    _run(house)
    about = house["client"].get("/api/mirror/folder").get_json()
    assert about["holds"] == "this" and about["needed_bytes"] == 0


def test_too_little_room_is_said(house, monkeypatch):
    class Usage:
        free, total, used = 10, 100, 90
    monkeypatch.setattr(mirror_place.shutil, "disk_usage", lambda path: Usage)
    about = house["client"].get("/api/mirror/folder").get_json()
    assert about["short_of_room"] is True


def test_a_share_that_is_not_connected_is_called_one(house, monkeypatch, tmp_path):
    mount = tmp_path / "n300"
    folder = mount / "Ninaivu"
    folder.mkdir(parents=True)
    _as_share(monkeypatch, mount)
    house["cfg"].mirror_dir = str(folder)
    status = house["mirror"].status()
    assert status["network"] is True and status["mount_point"] == str(mount)
    folder.rmdir()
    status = house["mirror"].status()
    assert status["there"] is False
    assert "network share" in status["problem"] and str(mount) in status["problem"]
    assert "network share" in status["away"]


# -- a share whose disk dropped -------------------------------------------

def test_a_share_with_its_disk_gone_is_never_written_to(house, monkeypatch):
    _as_share(monkeypatch, house["disk"])
    house["mirror"].fstype = "smbfs"
    _run(house)
    # The VM shares its own empty mount point when the N300 drops.
    for path in sorted(house["disk"].rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    status = _run(house)
    assert "the share is connected but the second copy is not on it" in status["error"]
    assert status["copy_missing"] is True
    assert not any(house["disk"].iterdir())
    # Said to be a new disk: copied from the beginning.
    assert house["mirror"].start_afresh() == {"started": True}
    house["mirror"].join(30)
    status = house["mirror"].status()
    assert status["owed"] == 0 and not status["error"] and status["copy_missing"] is False


def test_a_local_disk_wiped_at_the_same_place_is_still_copied_to(house):
    _run(house)
    for path in sorted(house["disk"].rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    assert _run(house)["owed"] == 0


def test_a_copy_that_goes_away_while_copying_stops_writing(house):
    mirror = house["mirror"]
    marker = house["disk"] / mirror_mod.MARKER
    real = mirror._copy_one
    calls = []

    def copy_then_drop(target, root, rel, conn):
        calls.append(rel)
        out = real(target, root, rel, conn)
        marker.unlink(missing_ok=True)        # the disk behind the share dropped
        return out
    mirror._copy_one = copy_then_drop
    status = _run(house)
    assert len(calls) == 1 and "no longer there" in status["error"]


# -- the Folders screen ----------------------------------------------------

def test_the_copy_is_listed_with_what_each_file_is(house):
    _run(house)
    top = house["library"].name
    (house["disk"] / top / "misc" / "mine.txt").write_text("someone else's")
    client = house["client"]
    root = client.get("/api/mirror/browse").get_json()
    assert [d["name"] for d in root["dirs"]] == [top] and root["library_folder"] is None
    misc = client.get(f"/api/mirror/browse?path={top}/misc").get_json()
    kinds = {f["name"]: f["is"] for f in misc["files"]}
    assert kinds["plain.png"] == "copied" and kinds["mine.txt"] == "other"
    plain = next(f for f in misc["files"] if f["name"] == "plain.png")
    assert plain["library_path"] == "misc/plain.png" and misc["library_folder"] == "misc"
    assert not any(f["name"].startswith(".") for f in root["files"])
    assert client.get("/api/mirror/browse?path=../..").status_code == 400
    opened = client.get(f"/api/mirror/file?path={top}/misc/plain.png")
    assert opened.status_code == 200
    assert opened.data == (house["library"] / "misc/plain.png").read_bytes()
    assert client.get(f"/api/mirror/file?path={mirror_mod.MARKER}").status_code == 400
    assert client.get("/api/mirror/file?path=../x").status_code == 400


def test_a_page_on_the_copy_is_handed_over_never_opened_as_the_console(house):
    """A file on the copy that is not a picture, video or sound is a download:
    an HTML or SVG page (the share may be written by other programs) opened
    inline would run its scripts as the signed-in admin's console."""
    _run(house)
    top = house["library"].name
    folder = house["disk"] / top / "misc"
    (folder / "page.html").write_text("<script src='x.js'></script>")
    (folder / "x.js").write_text("fetch('/api/admin/users')")
    (folder / "pic.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    client = house["client"]
    for name in ("page.html", "x.js", "pic.svg"):
        got = client.get(f"/api/mirror/file?path={top}/misc/{name}")
        assert got.status_code == 200
        assert got.mimetype == "application/octet-stream"
        assert got.headers["Content-Disposition"].startswith("attachment")
    photo = client.get(f"/api/mirror/file?path={top}/misc/plain.png")
    assert photo.mimetype == "image/png"
    assert not photo.headers.get("Content-Disposition", "").startswith("attachment")
    assert "no-store" in photo.headers["Cache-Control"]


def test_library_and_copy_lead_to_each_other(house):
    _run(house)
    top = house["library"].name
    client = house["client"]
    assert client.get("/api/mirror/where?folder=misc").get_json()["path"] == f"{top}/misc"
    asset = house["conn"].execute(
        "SELECT id FROM assets WHERE rel_path='misc/plain.png'").fetchone()["id"]
    place = client.get(f"/api/mirror/where?id={asset}").get_json()
    assert place == {"path": f"{top}/misc", "file": f"{top}/misc/plain.png", "copied": True}


def test_deleting_on_the_copy_needs_the_password_and_copies_a_library_file_again(house):
    _run(house)
    top = house["library"].name
    client = house["client"]
    copy = house["disk"] / top / "misc" / "plain.png"
    stray = house["disk"] / top / "misc" / "mine.txt"
    stray.write_text("someone else's")
    paths = [f"{top}/misc/plain.png", f"{top}/misc/mine.txt"]
    refused = client.post("/api/mirror/delete", json={"paths": paths})
    assert refused.status_code == 401 and refused.get_json()["needs_password"]
    assert client.post("/api/mirror/delete",
                       json={"paths": paths, "password": "wrong"}).status_code == 401
    assert copy.is_file() and stray.is_file()
    done = client.post("/api/mirror/delete", json={"paths": paths, "password": ADMIN[1]}).get_json()
    assert sorted(done["deleted"]) == sorted(paths) and done["copied_again"] == 1
    assert not copy.exists() and not stray.exists()
    assert house["mirror"].status()["owed"] == 1
    # The library still has it, so the copy follows the library; the stray stays gone.
    house["services"].mirror.cfg.mirror_every_hours = 24
    _run(house)
    assert copy.is_file() and not stray.exists()
    bad = client.post("/api/mirror/delete", json={"paths": [f"../{top}"], "password": ADMIN[1]})
    assert bad.get_json()["failed"] and not bad.get_json()["deleted"]


def test_moving_the_copy_to_a_new_empty_folder_starts_a_copy_there(house, tmp_path):
    _run(house)
    # On the same disk as Ninaivu's own folder, not a mount point: the folder
    # an unplugged disk leaves behind looks the same, but this one was chosen.
    fresh = tmp_path / "new-place"
    fresh.mkdir()
    saved = house["client"].post("/api/mirror/settings", json={"folder": str(fresh)})
    assert saved.status_code == 200
    status = _run(house)
    assert not status["error"] and status["owed"] == 0
    assert (fresh / mirror_mod.MARKER).is_file()


def test_the_same_copy_under_another_name_keeps_its_record(house, tmp_path):
    _run(house)
    renamed = tmp_path / "renamed-share"
    house["disk"].rename(renamed)
    house["client"].post("/api/mirror/settings", json={"folder": str(renamed)})
    status = house["mirror"].status()
    assert status["owed"] == 0 and status["copied"] == status["files"]
