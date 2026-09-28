"""New files go to the library they belong in — and, only when that library
cannot be written, to the new-files folder instead.

A library on an NTFS drive is read-only on a Mac. Edited copies, approved
uploads and phone backups all failed there, with a message about disk space and
permissions. A writable library behaves exactly as it always has.
"""
import io
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, ids_of
from test_date_access import dates as dates
from ninaivu.storage import db, new_files


def png():
    stream = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 120, 90)).save(stream, "PNG")
    return stream.getvalue()


def save(client, target):
    return client.post(f"/api/asset/{target}/edited-copy", data=png(), content_type="image/png")


@pytest.fixture()
def read_only(monkeypatch):
    """Make the given library folders read as not writable, as macOS reports an
    NTFS drive — on any system, and whoever runs the tests."""
    blocked: set[str] = set()
    real = new_files.writable
    monkeypatch.setattr(new_files, "writable",
                        lambda root: str(root) not in blocked and real(root))
    return blocked


class Cfg:
    def __init__(self, tmp_path, roots):
        self.roots = list(roots)
        self.new_files_folder = str(tmp_path / "Pictures" / "Ninaivu")
        self.saved = 0

    def add_library(self, path):
        self.roots.append(path)

    def save(self):
        self.saved += 1


def test_a_writable_library_keeps_its_new_files(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    cfg = Cfg(tmp_path, [str(library)])
    assert new_files.destination(cfg, str(library)) == str(library)
    assert cfg.roots == [str(library)] and cfg.saved == 0
    assert not (tmp_path / "Pictures").exists(), "nothing made when nothing is needed"


def test_a_read_only_library_sends_them_to_the_new_files_folder_once(tmp_path, read_only):
    library = tmp_path / "lib"
    library.mkdir()
    read_only.add(str(library))
    cfg = Cfg(tmp_path, [str(library)])
    home = str((tmp_path / "Pictures" / "Ninaivu").resolve())
    assert new_files.destination(cfg, str(library)) == home
    assert new_files.destination(cfg, str(library)) == home
    assert cfg.roots == [str(library), home], "added to the library folders, once"
    assert cfg.saved == 1


def test_a_change_to_an_existing_file_says_why_it_cannot_be_made(tmp_path, read_only):
    library = tmp_path / "lib"
    library.mkdir()
    assert new_files.read_only_reason(library) is None
    read_only.add(str(library))
    assert "read-only" in new_files.read_only_reason(library)


def test_an_admins_edit_of_a_read_only_library_lands_in_the_same_folder_there(
        as_admin, scanned, tmp_path, read_only):
    cfg, conn, _ = scanned
    cfg.new_files_folder = str(tmp_path / "Pictures" / "Ninaivu")
    target = ids_of(as_admin)[0]
    source = db.get_asset(conn, target)
    original = Path(source["root"]) / source["rel_path"]
    before = original.read_bytes()
    read_only.add(source["root"])

    response = save(as_admin, target)
    assert response.status_code == 201, response.get_json()
    copy = db.get_asset(conn, response.json["id"])
    home = str((tmp_path / "Pictures" / "Ninaivu").resolve())
    assert copy["root"] == home and home in cfg.roots
    assert copy["folder"] == source["folder"]
    assert Path(copy["rel_path"]).parent == Path(source["rel_path"]).parent
    assert (Path(home) / copy["rel_path"]).is_file()
    assert not (original.parent / copy["filename"]).exists(), "nothing written beside it"
    assert original.read_bytes() == before
    assert copy["visibility"] == source["visibility"]
    assert as_admin.get(f"/api/thumb/{copy['id']}").status_code == 200


def test_an_admins_edit_of_a_writable_library_stays_beside_its_source(as_admin, scanned, tmp_path):
    cfg, conn, _ = scanned
    cfg.new_files_folder = str(tmp_path / "Pictures" / "Ninaivu")
    target = ids_of(as_admin)[0]
    source = db.get_asset(conn, target)
    copy = db.get_asset(conn, save(as_admin, target).json["id"])
    assert copy["root"] == source["root"]
    assert not (tmp_path / "Pictures").exists()


def test_an_approved_edit_of_a_read_only_library_lands_in_the_new_files_folder(
        dates, tmp_path, read_only):
    cfg, conn, _, _, family, admin = dates
    cfg.new_files_folder = str(tmp_path / "Pictures" / "Ninaivu")
    target = ids_of(family)[0]
    source = db.get_asset(conn, target)
    pending_id = save(family, target).get_json()["pending_id"]
    read_only.add(source["root"])

    approved = admin.post(f"/api/admin/uploads/{pending_id}/approve", json={})
    assert approved.status_code == 200, approved.get_json()
    copy = db.get_asset(conn, approved.get_json()["item"]["id"])
    home = str((tmp_path / "Pictures" / "Ninaivu").resolve())
    assert copy["root"] == home
    assert copy["folder"] == source["folder"]
    assert copy["visibility"] == source["visibility"]
    assert (Path(home) / copy["rel_path"]).is_file()


# --- changes to files already there, which cannot be made anywhere else -----

def test_deleting_from_a_read_only_library_says_why(as_admin, scanned, read_only):
    _, conn, _ = scanned
    target = ids_of(as_admin)[0]
    read_only.add(db.get_asset(conn, target)["root"])
    body = as_admin.post("/api/delete",
                         json={"ids": [target], "password": ADMIN[1]}).get_json()
    assert body.get("deleted", 0) == 0
    assert any(new_files.READ_ONLY in item["why"] for item in body["failed"]), body
    assert db.get_asset(conn, target)["trashed"] == 0


def test_changing_a_date_in_a_read_only_library_says_why(as_admin, scanned, read_only):
    _, conn, _ = scanned
    target = ids_of(as_admin)[0]
    read_only.add(db.get_asset(conn, target)["root"])
    response = as_admin.patch(f"/api/admin/assets/{target}/creation-date",
                             json={"creation_date": "2001-02-03"})
    assert response.status_code == 409
    assert "read-only" in response.get_json()["error"]


# --- set up at start-up, so it is visible before the first save ---------------

def _services(tmp_path, library):
    from types import SimpleNamespace
    from ninaivu import Services

    cfg = Cfg(tmp_path, [str(library)])
    return SimpleNamespace(cfg=cfg, _prepare=Services._prepare_new_files_folder), cfg


def test_start_up_sets_the_folder_up_for_a_read_only_library(tmp_path, read_only):
    library = tmp_path / "lib"
    library.mkdir()
    read_only.add(str(library))
    services, cfg = _services(tmp_path, library)
    services._prepare(services)
    assert (tmp_path / "Pictures" / "Ninaivu").is_dir()
    assert cfg.roots == [str(library), str((tmp_path / "Pictures" / "Ninaivu").resolve())]


def test_start_up_leaves_a_writable_or_unplugged_library_alone(tmp_path):
    library = tmp_path / "lib"
    library.mkdir()
    for root in (library, tmp_path / "unplugged"):
        services, cfg = _services(tmp_path, root)
        services._prepare(services)
        assert cfg.roots == [str(root)] and cfg.saved == 0
    assert not (tmp_path / "Pictures").exists()
