"""The timeline is when a photograph was taken, not when a file was touched.

A library that has been copied between drives, restored from a backup or
opened in an editor carries modification times that have nothing to do with
when the picture was made. Measured across one real library, the photographs
that carry both an EXIF date and a modification time are a median of ten years
apart. So the order of preference matters, and so does never quietly
presenting a file's last-edited time as the day something happened.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from ninaivu.media import media, scanner as scanner_mod
from ninaivu.server.config import media_kind


# ---------------------------------------------------------------------------
# Which timestamp
# ---------------------------------------------------------------------------

class _Stat:
    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


def test_an_edited_photograph_keeps_the_date_it_was_created():
    """Editing bumps the modification time and leaves creation alone."""
    st = _Stat(st_mtime=2_000_000_000.0, st_ctime=1_000_000_000.0)
    assert media.created_at(st) == 1_000_000_000.0


def test_a_copied_photograph_keeps_the_date_it_was_taken():
    """Copying does the opposite: a new creation time, the old mtime kept.

    This is exactly what the Archive tab does to every file it consolidates,
    so getting it backwards would re-date the whole archive to the day it ran.
    """
    st = _Stat(st_mtime=1_000_000_000.0, st_ctime=2_000_000_000.0)
    assert media.created_at(st) == 1_000_000_000.0


def test_birthtime_is_used_where_the_filesystem_offers_it():
    st = _Stat(st_mtime=2_000_000_000.0, st_ctime=2_000_000_000.0,
               st_birthtime=1_500_000_000.0)
    assert media.created_at(st) == 1_500_000_000.0


def test_a_missing_or_zero_timestamp_never_wins():
    st = _Stat(st_mtime=1_700_000_000.0, st_ctime=0.0)
    assert media.created_at(st) == 1_700_000_000.0


def test_the_answer_can_only_ever_move_a_photograph_earlier(tmp_path):
    """The guarantee that makes this safe to apply to a whole library."""
    for mtime, ctime in ((10.0, 20.0), (20.0, 10.0), (15.0, 15.0)):
        st = _Stat(st_mtime=mtime, st_ctime=ctime)
        assert media.created_at(st) <= mtime


# ---------------------------------------------------------------------------
# …and how the scanner uses it
# ---------------------------------------------------------------------------

def _photo(path: Path, when: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (60, 40), (90, 120, 160))
    if when:
        exif = img.getexif()
        exif.get_ifd(0x8769)[36867] = when
        img.save(path, exif=exif)
    else:
        img.save(path)
    return path


def test_a_capture_date_in_the_file_always_wins(tmp_path, cfg):
    """Nothing about the filesystem is consulted when the photograph knows."""
    photo = _photo(tmp_path / "lib" / "shot.jpg", "2014:11:23 16:45:00")
    os.utime(photo, (time.time(), time.time()))     # "touched today"
    record = scanner_mod.build_record(
        tmp_path / "lib", "shot.jpg", photo.stat(), cfg)
    assert record["date_source"] == "exif"
    assert record["date_key"] == "2014-11-23"


def test_a_photograph_with_no_date_is_not_filed_under_today(tmp_path, cfg):
    """The complaint this whole module exists to answer."""
    photo = _photo(tmp_path / "lib" / "stripped.jpg")
    old = time.time() - 365 * 86400 * 5
    os.utime(photo, (old, old))
    record = scanner_mod.build_record(
        tmp_path / "lib", "stripped.jpg", photo.stat(), cfg)
    assert record["date_source"] in {"created", "mtime"}
    filed = datetime.fromtimestamp(record["captured_at"])
    assert filed.year == datetime.fromtimestamp(old).year, (
        "a photograph with no date of its own must not land under this month")


def test_the_date_source_is_recorded_so_a_guess_is_visible(tmp_path, cfg):
    """A fabricated date that cannot be told from a real one is the worst kind."""
    dated = _photo(tmp_path / "lib" / "dated.jpg", "2019:04:01 09:00:00")
    plain = _photo(tmp_path / "lib" / "plain.jpg")
    a = scanner_mod.build_record(tmp_path / "lib", "dated.jpg", dated.stat(), cfg)
    b = scanner_mod.build_record(tmp_path / "lib", "plain.jpg", plain.stat(), cfg)
    assert a["date_source"] == "exif"
    assert b["date_source"] != "exif"


# ---------------------------------------------------------------------------
# What counts as a photograph at all
# ---------------------------------------------------------------------------

def test_a_typescript_file_is_not_a_video(tmp_path):
    """`.ts` is MPEG transport stream — and it is also TypeScript.

    Eighty-two `index.d.ts` files were indexed as videos in a family photo
    library because the extension was taken at its word.
    """
    source = tmp_path / "index.d.ts"
    source.write_text("export declare function hello(): void;\n", encoding="utf-8")
    assert media_kind(source) == "unknown"

    module = tmp_path / "binding.d.mts"
    module.write_text("export {};\n", encoding="utf-8")
    assert media_kind(module) == "unknown"


def test_a_real_transport_stream_still_is_one(tmp_path):
    stream = tmp_path / "recording.ts"
    packet = bytes([0x47]) + b"\x00" * 187
    stream.write_bytes(packet * 4)
    assert media_kind(stream) == "video"


def test_a_camcorder_clip_is_a_video(tmp_path):
    """AVCHD `.MTS` packets are 192 bytes: a 4-byte timestamp, then the sync byte.

    Checking only the 188-byte layout dismissed every camcorder clip as unknown.
    """
    clip = tmp_path / "00012.MTS"
    packet = b"\x00\x01\x02\x03" + bytes([0x47]) + b"\x00" * 187
    clip.write_bytes(packet * 4)
    assert media_kind(clip) == "video"


def test_an_unreadable_file_is_not_assumed_to_be_media(tmp_path):
    assert media_kind(tmp_path / "gone.ts") == "unknown"


def test_ordinary_extensions_are_not_slowed_down_by_a_file_read(tmp_path):
    """Only the ambiguous ones are opened; everything else answers from its name."""
    assert media_kind(tmp_path / "never-created.jpg") == "picture"
    assert media_kind(tmp_path / "never-created.mp4") == "video"
    assert media_kind(tmp_path / "never-created.mp3") == "audio"
