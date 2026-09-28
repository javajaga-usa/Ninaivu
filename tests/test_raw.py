"""RAW files, which used to index as broken tiles.

`.cr2`, `.nef`, `.arw` and friends were already classified as pictures, but
nothing could open one: Pillow refuses them, so the whole picture branch of
the scan threw and the file ended up with no thumbnail, no camera information
and — the part that actually hurts — no capture date, so a weekend's shooting
filed itself under the day the card was copied.

Cameras write a full-size JPEG preview inside the RAW, carrying the same EXIF
as the frame. Pulling that out fixes the picture and the date together,
without decoding a sensor mosaic or taking a dependency.
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.media import media


def jpeg_bytes(size, colour, when: str | None = None) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, colour)
    if when:
        exif = image.getexif()
        exif[0x9003] = when            # DateTimeOriginal
        exif[0x0110] = "EOS 5D"        # Model
        exif[0x010F] = "Canon"         # Make
        image.save(buffer, "JPEG", quality=90, exif=exif)
    else:
        image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def write_raw(path: Path, *, when: str | None = None, big=(1600, 1200)) -> Path:
    """A RAW the way a camera lays one out: a small preview, then a large one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    small = jpeg_bytes((160, 120), (200, 30, 30))
    large = jpeg_bytes(big, (30, 120, 200), when)
    path.write_bytes(b"II*\x00" + b"\x00" * 64 + small
                     + b"\x00" * 128 + large + b"\x00" * 32)
    return path


# --- finding the picture ---------------------------------------------------

def test_the_full_size_preview_is_used_not_the_thumbnail(tmp_path):
    """Most RAWs hold two or three previews and the first one in the file is
    the 160-pixel image for the camera's own grid. Taking the first would give
    every RAW a postage-stamp thumbnail."""
    raw = write_raw(tmp_path / "IMG_0001.cr2")
    image = media.open_raw(raw)
    assert image is not None
    assert image.size == (1600, 1200)


@pytest.mark.parametrize("ext", [".cr2", ".cr3", ".nef", ".arw", ".dng",
                                 ".orf", ".rw2", ".raf", ".srw"])
def test_every_raw_extension_is_recognised(ext, tmp_path):
    assert media.is_raw(tmp_path / f"shot{ext}") is True


def test_ordinary_photographs_do_not_take_the_raw_path(tmp_path):
    path = tmp_path / "holiday.jpg"
    path.write_bytes(jpeg_bytes((800, 600), (10, 200, 10)))
    assert media.is_raw(path) is False
    assert media._open_oriented(path).size == (800, 600)


def test_a_raw_with_no_preview_at_all_is_reported_not_guessed(tmp_path):
    """Without a preview and without rawpy there is nothing to show. Raising
    is right — the caller already treats an unreadable picture as unreadable,
    and inventing a blank tile would hide the problem."""
    raw = tmp_path / "IMG_0002.cr2"
    raw.write_bytes(b"II*\x00" + b"\x00" * 4096)
    assert media.embedded_jpeg(raw) is None
    with pytest.raises(OSError):
        media._open_oriented(raw)


def test_an_absurdly_large_file_is_not_read_into_memory(tmp_path, monkeypatch):
    """The search reads the file whole, so it needs a ceiling: a mislabelled
    disk image must not be pulled into RAM looking for a JPEG."""
    monkeypatch.setattr(media, "RAW_MAX_SCAN_BYTES", 1024)
    raw = write_raw(tmp_path / "huge.cr2")
    assert media.embedded_jpeg(raw) is None


# --- the date, which is the point ------------------------------------------

def test_the_capture_date_survives(tmp_path):
    raw = write_raw(tmp_path / "IMG_0003.cr2", when="2019:07:04 11:30:00")
    image = media.open_raw(raw)
    exif = media.read_exif(image)
    assert exif.get("camera") or exif.get("model") or exif.get("make")
    assert exif.get("captured_at")
    taken = datetime.fromtimestamp(exif["captured_at"])
    assert (taken.year, taken.month, taken.day) == (2019, 7, 4)


def test_a_raw_indexes_with_a_thumbnail_and_the_right_date(cfg, tmp_path):
    """End to end: the failure this fixes was silent, so the test is the whole
    scan rather than the opener alone."""
    from ninaivu.server import auth
    from ninaivu.storage import db
    from ninaivu.media.scanner import Scanner

    library = Path(cfg.active_root)
    write_raw(library / "shoot" / "IMG_0004.cr2", when="2018:03:09 08:15:00")

    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    Scanner(cfg)._run(library, full=True)

    row = conn.execute(
        "SELECT * FROM assets WHERE filename='IMG_0004.cr2'").fetchone()
    assert row is not None, "the RAW was not indexed at all"
    assert row["kind"] == "picture"
    assert row["thumb"], "no thumbnail was produced"
    assert row["width"] == 1600 and row["height"] == 1200
    assert row["date_key"] == "2018-03-09", (
        f"expected the EXIF date, got {row['date_key']} from {row['date_source']}")
    assert row["date_source"] == "exif"



def test_a_raw_preview_is_decoded_small_for_indexing_but_keeps_its_size(tmp_path):
    raw = write_raw(tmp_path / "IMG_0003.cr2", big=(4000, 3000))
    image, size = media.open_for_index(raw, 640)
    assert size == (4000, 3000)
    assert max(image.size) < 4000


def test_a_small_decode_does_not_mistake_the_preview_for_the_grid_thumbnail(tmp_path):
    """The "is this the 160-pixel grid image" check must read the real size.

    A 1600-pixel preview decoded at an eighth is 200 pixels across — which,
    judged by what was decoded, looks exactly like the grid thumbnail.
    """
    raw = write_raw(tmp_path / "IMG_0004.cr2", big=(1600, 1200))
    image = media.open_raw(raw, edge=100)
    assert image is not None
    assert image.info[media.FULL_SIZE] == (1600, 1200)
