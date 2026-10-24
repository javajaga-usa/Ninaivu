"""Picasa's notes brought in after an import (ninaivu/archive/picasa.py).

The import copies an old drive's photographs and leaves the folder's
``.picasa.ini`` behind: the stars, the named faces, the albums, the turns and
the hidden photographs a family gave Picasa over the years. These tests run a
real import of a small Picasa folder, index it, and check that what the file
said lands on the library's photographs, once, and that the drive is only read.
"""

import hashlib
import json
import random
import time
from pathlib import Path

import pytest
from PIL import Image

from ninaivu import archive
from ninaivu.archive import database as adb
from ninaivu.archive import picasa
from ninaivu.archive.scanner import ArchiveJob
from ninaivu.storage import db


def jpeg(path: Path, seed: str, *, orientation: int | None = None, when="2008:10:28 18:30:00"):
    """A noise JPEG big enough for the import (it leaves files under 60 KB alone)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    img = Image.frombytes("RGB", (320, 240), bytes(rng.getrandbits(8) for _ in range(320 * 240 * 3)))
    exif = img.getexif()
    exif[0x0132] = when
    exif.get_ifd(0x8769)[0x9003] = when
    if orientation:
        exif[0x0112] = orientation
    img.save(path, "JPEG", quality=95, exif=exif)
    return path


INI = """[Picasa]
name=Diwali 2008
[Contacts2]
8e62398ebda8c1a5=Amma;;
[.album:a1b2c3]
name=Diwali at home
token=a1b2c3
[IMG_0001.JPG]
star=yes
albums=a1b2c3
faces=rect64(4000400080008000),8e62398ebda8c1a5;rect64(10001000200020),ffffffffffffffff
[IMG_0002.JPG]
rotate=rotate(1)
caption=Paati's kolam
[IMG_0003.JPG]
hidden=yes
"""


def picasa_drive(root: Path) -> Path:
    """An old drive with one Picasa folder: three photographs, its notes, and
    the original Picasa set aside when an edit of IMG_0001 was saved."""
    folder = root / "OldDrive" / "Pictures" / "Diwali 2008"
    jpeg(folder / "IMG_0001.JPG", "edited", orientation=6)
    jpeg(folder / "IMG_0002.JPG", "kolam")
    jpeg(folder / "IMG_0003.JPG", "private")
    jpeg(folder / ".picasaoriginals" / "IMG_0001.JPG", "original", orientation=6)
    (folder / ".picasa.ini").write_text(INI, encoding="utf-8")
    return root / "OldDrive"


def fingerprint(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def test_the_file_is_read_as_picasa_wrote_it():
    folder = picasa.parse(INI)
    first = folder.entries["img_0001.jpg"]
    assert first.star and first.albums == ["a1b2c3"]
    assert len(first.faces) == 1, "a face Picasa was told to ignore is not a person"
    (box, contact), = first.faces
    assert contact == "8e62398ebda8c1a5"
    assert box == pytest.approx((0.25, 0.25, 0.5, 0.5), abs=1e-3)
    assert folder.entries["img_0002.jpg"].rotate == 1
    assert folder.entries["img_0002.jpg"].caption == "Paati's kolam"
    assert folder.entries["img_0003.jpg"].hidden
    assert folder.albums == {"a1b2c3": "Diwali at home"}
    assert folder.contacts == {"8e62398ebda8c1a5": "Amma"}


def test_a_box_at_the_edge_keeps_its_place_without_its_leading_zeros():
    # Picasa drops leading zeros: "10001000200020", zero-filled to sixteen
    # digits, is 0010 0010 0020 0020.
    assert picasa.rect64("rect64(10001000200020)") == pytest.approx(
        (0x10 / 65535, 0x10 / 65535, 0x20 / 65535, 0x20 / 65535))
    assert picasa.rect64("rect64(80008000400040)") is None, "right of left, below top"
    assert picasa.rect64("rect64(zz)") is None


def test_an_old_windows_file_is_read_in_its_code_page(tmp_path):
    (tmp_path / "Picasa.ini").write_bytes(
        "[Contacts2]\nabc=Sébastien;;\n[a.jpg]\nstar=yes\n".encode("cp1252"))
    folder = picasa.read_folder(str(tmp_path))
    assert folder.contacts == {"abc": "Sébastien"}
    assert folder.entries["a.jpg"].star


def test_a_folder_without_picasa_is_none(tmp_path):
    assert picasa.read_folder(str(tmp_path)) is None


def test_a_face_box_is_found_under_the_photographs_turn():
    # Stored 320 x 240, shown turned a quarter clockwise (EXIF 6): 240 x 320.
    hints = [((0.25, 0.25, 0.5, 0.5), "Amma")]
    face = {"bbox": json.dumps([120, 80, 60, 80])}
    other = {"bbox": json.dumps([10, 10, 40, 40])}
    placed = picasa.place_faces(hints, [other, face], (320, 240), 90)
    assert [(k, j) for k, j, _ in placed] == [(0, 1)]
    assert placed[0][2] == pytest.approx(1.0)
    # The same box is still found when the photograph's tag was lost on the
    # way and the turn expected is wrong.
    assert [(k, j) for k, j, _ in picasa.place_faces(hints, [other, face], (320, 240), 0)] == [(0, 1)]
    assert picasa.place_faces(hints, [other], (320, 240), 90) == []


# ---------------------------------------------------------------------------
# After a real import
# ---------------------------------------------------------------------------

@pytest.fixture()
def imported(tmp_path):
    """A Picasa drive imported into a library and indexed; an administrator."""
    from ninaivu.media.scanner import Scanner
    from ninaivu.server import auth
    from ninaivu.server.config import Config

    dest = tmp_path / "library"
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(dest)]
    cfg.active_root = str(dest)
    cfg.ensure_dirs()
    # Where the console keeps the import's record too.
    archive.configure(cfg.state_dir)
    adb.close_db()
    adb.init_db()
    drive = picasa_drive(tmp_path)
    before = fingerprint(drive)
    ArchiveJob([str(drive)], str(dest)).run()

    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    admin = auth.bootstrap_admin(conn, "dad", "correcthorse1", "Dad")
    Scanner(cfg)._run(dest, full=True)
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 4
    yield cfg, conn, drive, before, admin.id
    adb.close_db()


def asset(conn, drive: Path, rel: str) -> dict:
    """The library photograph the import made of *rel* on the drive."""
    dest = adb.get_db().execute("SELECT destination_path FROM files WHERE source_path=?",
                                (str(drive / rel),)).fetchone()[0]
    row = conn.execute("SELECT * FROM assets WHERE filename=?", (Path(dest).name,)).fetchone()
    return dict(row)


def test_a_look_counts_what_the_drive_holds(imported):
    cfg, conn, drive, before, _ = imported
    seen = picasa.summary(picasa.find(conn))
    assert seen["folders"] == 1
    assert seen["photos"] == 3 and seen["not_in_library"] == 0
    assert (seen["stars"], seen["faces"], seen["turned"], seen["hidden"], seen["captions"]) == (1, 1, 1, 1, 1)
    assert (seen["people"], seen["albums"], seen["originals"]) == (1, 1, 1)
    assert seen["unreachable"] == []
    assert fingerprint(drive) == before, "the drive is only read"


def test_bringing_in_puts_picasas_notes_on_the_photographs(imported):
    cfg, conn, drive, before, admin_id = imported
    edited = asset(conn, drive, "Pictures/Diwali 2008/IMG_0001.JPG")
    original = asset(conn, drive, "Pictures/Diwali 2008/.picasaoriginals/IMG_0001.JPG")
    kolam = asset(conn, drive, "Pictures/Diwali 2008/IMG_0002.JPG")
    private = asset(conn, drive, "Pictures/Diwali 2008/IMG_0003.JPG")
    # Ninaivu found Amma's face in the edit already, where Picasa had her.
    conn.execute("INSERT INTO faces(asset_id, bbox, embedding, created_at) VALUES (?,?,?,?)",
                 (edited["id"], json.dumps([120, 80, 60, 80]), b"\0" * 16, time.time()))
    # Both are alike enough to stack (the scan's own groups decide that).
    conn.execute("UPDATE assets SET dup_group=7 WHERE id IN (?,?)", (edited["id"], original["id"]))
    conn.commit()
    refreshed = []

    result = picasa.bring_in(conn, cfg, picasa.find(conn), user_id=admin_id,
                             refresh_person=refreshed.append)

    favourites = {r[0] for r in conn.execute(
        "SELECT asset_id FROM user_assets WHERE user_id=? AND favorite=1", (admin_id,))}
    assert favourites == {edited["id"], original["id"]}, "the original is the same photograph"
    album = conn.execute("SELECT id FROM albums WHERE name='Diwali at home'").fetchone()
    members = {r[0] for r in conn.execute("SELECT asset_id FROM album_items WHERE album_id=?",
                                          (album["id"],))}
    assert members == {edited["id"], original["id"]}
    turned = dict(conn.execute("SELECT rotation, rot_source, caption, caption_source FROM assets "
                               "WHERE id=?", (kolam["id"],)).fetchone())
    assert turned == {"rotation": 90, "rot_source": "manual",
                      "caption": "Paati's kolam", "caption_source": "manual"}
    assert conn.execute("SELECT visibility FROM assets WHERE id=?",
                        (private["id"],)).fetchone()[0] == 2
    face = conn.execute("SELECT f.source, p.name FROM faces f JOIN people_clusters p "
                        "ON p.id=f.person_id WHERE f.asset_id=?", (edited["id"],)).fetchone()
    assert tuple(face) == ("confirmed", "Amma")
    assert len(refreshed) == 1
    choice = conn.execute("SELECT cover FROM stack_choices").fetchone()
    assert choice["cover"] == edited["id"], "the edit on top, as the family saw it in Picasa"
    assert result["faces"] == 1 and result["stacked"] == 1 and result["hidden"] == 1
    # The original's faces wait for the face search to look at it.
    assert result["faces_waiting"] == 1
    assert fingerprint(drive) == before, "the drive is only read"


def test_a_second_run_does_not_undo_what_somebody_changed(imported):
    cfg, conn, drive, _, admin_id = imported
    edited = asset(conn, drive, "Pictures/Diwali 2008/IMG_0001.JPG")
    picasa.bring_in(conn, cfg, picasa.find(conn), user_id=admin_id)
    # Somebody takes the star off and takes it out of the album.
    db.set_user_asset(conn, admin_id, edited["id"], favorite=0)
    conn.execute("DELETE FROM album_items WHERE asset_id=?", (edited["id"],))
    conn.commit()

    again = picasa.bring_in(conn, cfg, picasa.find(conn), user_id=admin_id)

    assert again["favourites"] == 0 and again["album_photos"] == 0 and again["turned"] == 0
    assert conn.execute("SELECT COUNT(*) FROM user_assets WHERE user_id=? AND asset_id=? "
                        "AND favorite=1", (admin_id, edited["id"])).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM album_items WHERE asset_id=?",
                        (edited["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 1


def test_a_name_somebody_gave_here_is_not_replaced(imported):
    cfg, conn, drive, _, admin_id = imported
    edited = asset(conn, drive, "Pictures/Diwali 2008/IMG_0001.JPG")
    person = db.create_person_cluster(conn, "Lakshmi")
    conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, created_at) "
                 "VALUES (?,?,?,?,?,?)", (edited["id"], person["id"], "confirmed",
                                          json.dumps([120, 80, 60, 80]), b"\0" * 16, time.time()))
    conn.commit()

    result = picasa.bring_in(conn, cfg, picasa.find(conn), user_id=admin_id)

    assert result["faces"] == 0
    assert conn.execute("SELECT person_id FROM faces WHERE asset_id=?",
                        (edited["id"],)).fetchone()[0] == person["id"]


def test_a_drive_that_is_not_plugged_in_is_named(imported, tmp_path):
    cfg, conn, drive, _, _ = imported
    drive.rename(tmp_path / "elsewhere")
    seen = picasa.summary(picasa.find(conn))
    assert seen["folders"] == 0
    assert seen["unreachable"] == [str(drive)]


def test_the_console_looks_and_brings_in(tmp_path, imported):
    from conftest import login
    from ninaivu import build_services, create_admin_app, create_home_app

    cfg, conn, drive, _, admin_id = imported
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    client = login(create_admin_app(services).test_client(), "dad", "correcthorse1")

    def wait():
        deadline = time.time() + 30
        while time.time() < deadline:
            state = client.get("/api/archive/picasa").get_json()
            if not state["busy"]:
                return state
            time.sleep(0.1)
        raise AssertionError("the Picasa run did not finish")

    assert client.post("/api/archive/picasa/look").status_code == 202
    state = wait()
    assert state["phase"] == "looked" and state["found"]["stars"] == 1
    assert client.post("/api/archive/picasa/bring").status_code == 202
    state = wait()
    assert state["phase"] == "brought"
    assert state["brought"]["favourites"] == 2
    assert client.post("/api/archive/picasa/elsewhere").status_code == 404
    # The family's port has no such door.
    family = create_home_app(services).test_client()
    assert family.get("/api/archive/picasa").status_code in (401, 403, 404)
