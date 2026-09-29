"""Regressions for file-handling defects found in review.

Uploads that declare a vast image, a delete whose index write fails after the
files moved, a turn Windows will not let replace a streamed file, a PNG turn
that dropped its metadata, sidecars listed twice on case-insensitive disks,
and rollbacks that stopped at the first file they could not put back.
"""

from __future__ import annotations

import io
import os
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from ninaivu.media import date_edit, safe_image, upload_review
from ninaivu.server import auth, turn
from ninaivu.server.config import Config
from ninaivu.storage import db, recycle


@pytest.fixture
def base(tmp_path):
    yield tmp_path
    db.close_all()


def config(base):
    cfg = Config()
    cfg.state_dir = base / "state"
    cfg.active_root = str(base / "library")
    cfg.roots = [cfg.active_root]
    cfg.ai_enabled = cfg.watch = cfg.detect_orientation = False
    cfg.quality_scan = False
    cfg.min_media_bytes = 0
    Path(cfg.active_root).mkdir()
    cfg.ensure_dirs()
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    return cfg, conn


def insert(conn, cfg, name, **fields):
    return db.upsert_asset(conn, dict(root=cfg.active_root, rel_path=name,
        filename=name, kind="picture", ext=Path(name).suffix.lstrip("."),
        date_key="2025-01-01", **fields))


def huge_png_header(width=50000, height=50000) -> bytes:
    """A PNG that *declares* a vast image — all a decompression bomb needs.

    2.5 gigapixels by default: well above the library's 512-megapixel limit,
    which is what an upload is held to.
    """
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


class Upload:
    def __init__(self, data: bytes):
        self.data = data

    def save(self, stream):
        stream.write(self.data)


# ---------------------------------------------------------------------------
# 1. An upload that declares a vast image is refused before it is decoded
# ---------------------------------------------------------------------------

def test_an_upload_declaring_a_vast_image_is_refused_unread(base, monkeypatch):
    from ninaivu.media import scanner
    cfg, conn = config(base)

    def never(*_args, **_kwargs):
        raise AssertionError("the picture was handed to the indexer")

    monkeypatch.setattr(scanner, "build_record", never)
    with pytest.raises(ValueError, match="too large"):
        upload_review.stage(conn, cfg, Upload(huge_png_header()), "bomb.png",
                            cfg.active_root, "", None)
    # Nothing is left behind: no row, no staged file, no staging folder.
    assert conn.execute("SELECT COUNT(*) FROM pending_uploads").fetchone()[0] == 0
    staging = Path(cfg.state_dir) / "pending-uploads"
    assert not staging.exists() or not any(staging.iterdir())


def test_an_ordinary_upload_is_still_staged(base):
    cfg, conn = config(base)
    image = io.BytesIO()
    Image.new("RGB", (64, 48), "green").save(image, "PNG")
    staged = upload_review.stage(conn, cfg, Upload(image.getvalue()), "small.png",
                                 cfg.active_root, "", None)
    assert staged["status"] == "pending"


def test_the_file_check_passes_what_pillow_cannot_open(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    assert safe_image.check_untrusted_file(clip) is None
    small = tmp_path / "small.png"
    Image.new("RGB", (8, 8)).save(small)
    assert safe_image.check_untrusted_file(small) is None
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(huge_png_header())
    with pytest.raises(ValueError):
        safe_image.check_untrusted_file(bomb)


def test_the_file_check_accepts_a_big_real_photograph(tmp_path):
    # A 200-megapixel phone picture or a large scan is not a bomb. The check
    # uses the library's own limit, which importing media sets.
    from ninaivu.media import media                    # noqa: F401
    big = tmp_path / "scan.png"
    big.write_bytes(huge_png_header(20000, 10000))
    assert safe_image.check_untrusted_file(big) is None
    over = tmp_path / "over.png"
    over.write_bytes(huge_png_header(30000, 20000))     # 600 MP, past 512 MP
    with pytest.raises(ValueError):
        safe_image.check_untrusted_file(over)


# ---------------------------------------------------------------------------
# 2. A delete the index refuses puts the files back
# ---------------------------------------------------------------------------

def test_a_delete_the_index_refuses_puts_every_file_back(base):
    cfg, conn = config(base)
    root = Path(cfg.active_root)
    ids = []
    for name in ("one.jpg", "two.jpg"):
        Image.new("RGB", (16, 16), "red").save(root / name)
        ids.append(insert(conn, cfg, name))
    conn.execute("CREATE TRIGGER refuse_bin BEFORE INSERT ON recycled "
                 "BEGIN SELECT RAISE(ABORT, 'simulated database failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        recycle.recycle(conn, ids)
    assert (root / "one.jpg").is_file() and (root / "two.jpg").is_file()
    assert not [p for p in recycle.bin_path(root).rglob("*") if p.is_file()]
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# 3. A video Windows will not let go of is refused, not a crash
# ---------------------------------------------------------------------------

def test_a_video_that_cannot_be_replaced_is_refused(tmp_path, monkeypatch):
    from ninaivu.media import media
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"original video")

    def remux(source, target, wanted, style, flip):
        target.write_bytes(b"turned video")
        return True

    def locked(*_args, **_kwargs):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(turn, "video_rotation", lambda _path: 0)
    monkeypatch.setattr(turn, "_remux", remux)
    monkeypatch.setattr(turn.os, "replace", locked)
    result = turn.rotate_original(path, 90, "video")
    assert not result.ok and "could not be written" in result.why
    assert path.read_bytes() == b"original video"
    assert not list(tmp_path.glob(".ninaivu-turn-*"))


def test_an_unexpected_disk_error_is_one_files_refusal(tmp_path, monkeypatch):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (16, 16)).save(path)

    def locked(*_args, **_kwargs):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(turn, "_rotate_tagged", locked)
    result = turn.rotate_original(path, 90)
    assert not result.ok and "locked" in result.why


# ---------------------------------------------------------------------------
# 4. Turning a PNG keeps what the PNG carried
# ---------------------------------------------------------------------------

def png_with_metadata(path: Path, size=(40, 20), orientation=None) -> Path:
    exif = Image.Exif()
    exif[0x010F] = "Acme"
    if orientation:
        exif[0x0112] = orientation
    chunks = PngImagePlugin.PngInfo()
    chunks.add_text("Description", "Birthday")
    chunks.add_itxt("Title", "Paati's birthday", "en", "Title")
    Image.new("RGB", size, (10, 20, 30)).save(
        path, "PNG", exif=exif, icc_profile=b"synthetic-profile" * 8,
        dpi=(300, 150), pnginfo=chunks)
    return path


def test_a_png_turn_keeps_exif_profile_resolution_and_text(tmp_path):
    path = png_with_metadata(tmp_path / "shot.png")
    result = turn.rotate_original(path, 90)
    assert result.ok and result.how == "pixels"
    with Image.open(path) as img:
        img.load()
        assert img.size == (20, 40)
        assert img.getexif().get(0x010F) == "Acme"
        assert img.info.get("icc_profile") == b"synthetic-profile" * 8
        assert img.info["dpi"][0] == pytest.approx(150, abs=0.5)
        assert img.info["dpi"][1] == pytest.approx(300, abs=0.5)
        assert img.text["Description"] == "Birthday"
        assert img.text["Title"] == "Paati's birthday"


def test_a_png_that_claims_an_orientation_is_not_turned_twice(tmp_path):
    # Stored 40x20 but tagged "rotate 90", so it is shown 20x40. A further
    # quarter turn shows it 40x20, and the file must not still carry the tag.
    path = png_with_metadata(tmp_path / "tagged.png", orientation=6)
    result = turn.rotate_original(path, 90)
    assert result.ok
    with Image.open(path) as img:
        assert img.size == (40, 20)
        assert int(img.getexif().get(0x0112, 1)) == 1
        assert img.getexif().get(0x010F) == "Acme"


def test_a_png_that_does_not_read_back_is_restored(tmp_path, monkeypatch):
    path = png_with_metadata(tmp_path / "shot.png")
    before = path.read_bytes()
    monkeypatch.setattr(turn, "_still_a_picture", lambda *_args: False)
    result = turn.rotate_original(path, 90)
    assert not result.ok and "restored" in result.why
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# 5. One sidecar, two spellings, on a case-insensitive disk
# ---------------------------------------------------------------------------

def test_the_same_file_under_two_names_is_one_file(tmp_path):
    first = tmp_path / "IMG.xmp"
    first.write_text("metadata")
    os.link(first, tmp_path / "IMG.XMP")
    assert date_edit._identity(first) == date_edit._identity(tmp_path / "IMG.XMP")
    other = tmp_path / "other.xmp"
    other.write_text("metadata")
    assert date_edit._identity(first) != date_edit._identity(other)


def test_a_sidecar_seen_under_both_spellings_moves_once(base, monkeypatch):
    cfg, conn = config(base)
    root = Path(cfg.active_root)
    source = root / "IMG.jpg"
    Image.new("RGB", (16, 16), "red").save(source)
    (root / "IMG.xmp").write_text("metadata")
    asset = insert(conn, cfg, "IMG.jpg")

    # Pretend the library is on a case-insensitive disk: every spelling of a
    # name that exists is found, and is the same file.
    real_is_file = Path.is_file

    def is_file(self):
        if real_is_file(self):
            return True
        try:
            return any(p.name.casefold() == self.name.casefold() and real_is_file(p)
                       for p in self.parent.iterdir())
        except OSError:
            return False

    def identity(path):
        return str(path).casefold()

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(date_edit, "_identity", identity)
    moved = date_edit.relocate(conn, asset, date_edit.parse_date("2014-01-01"), [cfg.active_root])
    assert moved["rel_path"] == "2014/01/01/IMG.jpg"
    assert (root / "2014/01/01/IMG.xmp").read_text() == "metadata"


# ---------------------------------------------------------------------------
# 6. A rollback puts back every file it can, and reports the real failure
# ---------------------------------------------------------------------------

def test_a_date_rollback_keeps_going_past_a_file_it_cannot_restore(base, monkeypatch):
    cfg, conn = config(base)
    root = Path(cfg.active_root)
    source = root / "IMG.jpg"
    Image.new("RGB", (16, 16), "red").save(source)
    sidecar = root / "IMG.xmp"
    sidecar.write_text("metadata")
    asset = insert(conn, cfg, "IMG.jpg")
    # Fails after both files have moved.
    conn.execute("CREATE TRIGGER refuse_date BEFORE UPDATE OF captured_at ON assets "
                 "BEGIN SELECT RAISE(ABORT, 'simulated database failure'); END")
    conn.commit()
    real_move = date_edit._move

    def move(src, dst):
        if Path(src).name == "IMG.xmp" and Path(dst) == sidecar:
            raise OSError("simulated: the sidecar cannot be put back")
        real_move(src, dst)

    monkeypatch.setattr(date_edit, "_move", move)
    with pytest.raises(sqlite3.IntegrityError):
        date_edit.relocate(conn, asset, date_edit.parse_date("2014-01-01"), [cfg.active_root])
    # The photograph went back even though its sidecar, restored first, did not.
    assert source.is_file()
    assert db.get_asset(conn, asset)["rel_path"] == "IMG.jpg"


def test_an_approval_rollback_reports_the_real_failure(base, monkeypatch):
    cfg, conn = config(base)
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(image, "JPEG")
    staged = upload_review.stage(conn, cfg, Upload(image.getvalue()), "held.jpg",
                                 cfg.active_root, "", None)
    conn.execute("CREATE TRIGGER refuse_review BEFORE UPDATE ON pending_uploads "
                 "BEGIN SELECT RAISE(ABORT, 'simulated database failure'); END")
    conn.commit()
    real_move = date_edit._move
    calls = []

    def move(src, dst):
        calls.append((src, dst))
        if len(calls) > 1:
            raise OSError("simulated: the file cannot be put back")
        real_move(src, dst)

    monkeypatch.setattr(upload_review, "_same_device", lambda *_: True)
    monkeypatch.setattr(date_edit, "_move", move)
    with pytest.raises(sqlite3.IntegrityError):
        upload_review.approve(conn, cfg, staged["id"], None, "2020-01-02")
    assert len(calls) == 2
    assert upload_review.get(conn, staged["id"])["status"] == "pending"
