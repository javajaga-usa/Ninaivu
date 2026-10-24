"""Display copies on the Primary: smaller, and otherwise the original exactly."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageCms

from ninaivu.mugil import display, pairing
from ninaivu.mugil.courier import sent_record


def _jpeg(path: Path, size=(4000, 3000)) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 6                       # turned a quarter: kept as a tag, never applied
    exif[0x010F] = "Canon"
    exif[0x0132] = "2009:07:14 18:22:05"
    picture = Image.radial_gradient("L").resize(size).convert("RGB")
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    picture.save(path, quality=95, exif=exif.tobytes(), icc_profile=icc)
    os.utime(path, (1247595725, 1247595725))
    return path.read_bytes()


def test_each_kind_of_file_has_its_plan():
    assert display.plan("JPG", "picture", "Canon EOS") == "photo"
    assert display.plan("cr2", "picture", "Canon EOS") == "photo"
    assert display.plan("png", "picture", None) == "photo"
    assert display.plan("heic", "picture", "Apple iPhone 13") == "keep"
    assert display.plan("mov", "video", "Apple iPhone 13") == "keep"
    assert display.plan("avi", "video", None) == "video"
    assert display.plan("mp4", "video", "Sony") == "video"
    assert display.plan("mp3", "audio", None) == "keep"


def test_a_photo_copy_is_smaller_and_keeps_every_property(tmp_path):
    source = tmp_path / "IMG_0001.JPG"
    before = _jpeg(source)
    with Image.open(source) as original:
        exif, icc = original.info["exif"], original.info["icc_profile"]
    target = tmp_path / "copy.avif"
    made = display.make_photo(source, target)
    assert made == target.stat().st_size < len(before)
    with Image.open(target) as copy:
        assert copy.format == "AVIF"
        assert max(copy.size) == display.PHOTO_EDGE
        assert copy.size == (2560, 1920)          # not turned: the tag says how
        assert copy.info["exif"] == exif
        assert copy.info["icc_profile"] == icc
        assert copy.getexif()[0x0112] == 6
    assert target.stat().st_mtime == source.stat().st_mtime
    # The original is as it was.
    assert source.read_bytes() == before
    assert source.stat().st_mtime == 1247595725


def test_a_copy_that_is_no_smaller_is_not_kept(tmp_path, monkeypatch):
    source = tmp_path / "tiny.png"
    Image.new("RGB", (8, 8), (200, 10, 10)).save(source)
    state = tmp_path / "state"
    outcome = display.make(state, source, how="photo", algo="blake3", digest="ab" * 32)
    assert outcome == "larger"
    assert display.find(state, "ab" * 32) is None
    assert not list(display.folder(state).rglob("*.avif*"))


def _video(path: Path, rotate: int = 90) -> None:
    plain = path.with_name("plain-" + path.name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=1920x1440:rate=30", "-f", "lavfi", "-i", "sine=frequency=440",
                    "-t", "3", "-c:v", "libx264", "-crf", "8", "-c:a", "aac",
                    "-metadata", "creation_time=2014-02-03T04:05:06Z",
                    "-metadata", "location=+12.9700+077.5900/",
                    "-movflags", "use_metadata_tags", str(plain)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", str(rotate),
                    "-i", str(plain), "-c", "copy", "-map_metadata", "0",
                    "-movflags", "use_metadata_tags", str(path)], check=True)
    plain.unlink()
    os.utime(path, (1391400306, 1391400306))


needs_hevc = pytest.mark.skipif(not shutil.which("ffmpeg") or display.hevc_encoder() is None,
                                reason="no ffmpeg with an HEVC encoder")


@needs_hevc
def test_a_video_copy_is_hevc_and_keeps_its_tags_and_rotation(tmp_path):
    source = tmp_path / "clip.mp4"
    _video(source)
    before = source.read_bytes()
    target = tmp_path / "copy.mp4"
    display.make_video(source, target)
    probe = display._probe(target)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "hevc"
    assert min(video["width"], video["height"]) <= display.VIDEO_LINES
    original = display._probe(source)
    assert display._tags(probe) == display._tags(original)
    assert display._tags(probe)["creation_time"].startswith("2014-02-03T04:05:06")
    assert display._rotation(probe) == display._rotation(original) != 0
    assert target.stat().st_mtime == source.stat().st_mtime
    assert source.read_bytes() == before


# --- the worker ------------------------------------------------------------------

@pytest.fixture()
def primary(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    cfg = SimpleNamespace(state_dir=state, mugil_role="primary", roots=[str(root)])
    pairing.save(state, {"key": "00" * 32, "role": "primary"})
    index = tmp_path / "index.db"
    conn = sqlite3.connect(index)
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, root TEXT, rel_path TEXT, "
                 "ext TEXT, kind TEXT, camera TEXT, trashed INTEGER DEFAULT 0)")
    conn.commit()
    conn.close()

    def connect():
        c = sqlite3.connect(index)
        c.row_factory = sqlite3.Row
        return c
    return cfg, root, connect


def _add(cfg, root, connect, rel, kind="picture", camera="Canon", digest=None):
    path = root / rel
    with connect() as conn:
        conn.execute("INSERT INTO assets (root, rel_path, ext, kind, camera) VALUES (?,?,?,?,?)",
                     (str(root), rel, path.suffix.lstrip("."), kind, camera))
    digest = digest or (rel.encode().hex() * 8)[:64]
    with sent_record(cfg.state_dir) as rec:
        rec.execute("INSERT INTO sent VALUES (?,?,?,?,?,?,?)",
                    (str(root), rel, path.stat().st_size, path.stat().st_mtime, "blake3",
                     digest, 1.0))
    return digest


def test_the_primary_makes_copies_of_what_the_secondary_keeps(primary):
    cfg, root, connect = primary
    _jpeg(root / "a.jpg")
    (root / "song.mp3").write_bytes(b"ID3" + b"\0" * 2000)
    a = _add(cfg, root, connect, "a.jpg")
    _add(cfg, root, connect, "song.mp3", kind="audio")
    maker = display.DisplayCopies(cfg, connect)
    assert maker.wanted() and maker.owed_count() == 2
    assert maker.run_once() == 2
    assert maker.owed_count() == 0
    copy = display.find(cfg.state_dir, a)
    assert copy is not None and copy.suffix == ".avif"
    status = maker.status()
    assert status["made"] == 1 and status["kept"] == 1
    assert status["bytes"] < status["original_bytes"]
    assert maker.run_once() == 0


def test_only_originals_the_secondary_confirmed_are_copied(primary):
    cfg, root, connect = primary
    _jpeg(root / "a.jpg")
    maker = display.DisplayCopies(cfg, connect)
    assert maker.owed_count() == 0 and maker.run_once() == 0
    assert not display.folder(cfg.state_dir).exists()


def test_an_original_changed_since_it_was_sent_waits(primary):
    cfg, root, connect = primary
    _jpeg(root / "a.jpg")
    _jpeg(root / "b.jpg")
    _add(cfg, root, connect, "a.jpg")
    b = _add(cfg, root, connect, "b.jpg")
    with open(root / "a.jpg", "ab") as handle:
        handle.write(b"more")
    maker = display.DisplayCopies(cfg, connect)
    maker._passed = set()
    assert maker.run_once() == 2
    assert display.find(cfg.state_dir, b) is not None
    # a.jpg is passed over this round, and owed again once sent again.
    assert maker.owed_count() == 1
    assert maker.run_once() == 0


def test_one_computer_makes_no_display_copies(primary):
    cfg, _root, connect = primary
    cfg.mugil_role = "single"
    assert display.DisplayCopies(cfg, connect).wanted() is False
    cfg.mugil_role = "primary"
    pairing.forget(cfg.state_dir)
    assert display.DisplayCopies(cfg, connect).wanted() is False
