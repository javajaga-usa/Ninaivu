"""A photograph turned in the file itself, and the second copy.

"Rotate the files themselves" changes a few bytes of the file (the JPEG's
orientation tag, a video's display matrix, a PNG's pixels). The copy of it
already on the second copy's disk, from before the turn, is then the same
picture turned the other way, not another photograph: it is set aside as
"(before …)" and the turned one takes the library's name, instead of going
beside it as "name (2)". A file that is really different is still left
exactly where it is.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN
from ninaivu import build_services
from ninaivu.media import media
from ninaivu.server import auth, turn
from ninaivu.storage import mirror as mirror_mod


def _jpeg(path: Path, shade: int = 128) -> Path:
    img = Image.new("RGB", (64, 32))
    for x in range(64):
        for y in range(32):
            img.putpixel((x, y), (x * 4 % 256, y * 8 % 256, shade))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    return path


def _png(path: Path) -> Path:
    img = Image.new("RGB", (20, 10))
    for x in range(20):
        for y in range(10):
            img.putpixel((x, y), (x * 12, y * 25, 7))
    img.save(path, "PNG")
    return path


# -- telling a turned file from another one ----------------------------------

@pytest.mark.parametrize("step", [90, 180, 270])
def test_a_turned_jpeg_is_known_for_what_it_is(tmp_path, step):
    old = _jpeg(tmp_path / "a" / "x.jpg")
    new = tmp_path / "b" / "x.jpg"
    new.parent.mkdir()
    shutil.copyfile(old, new)
    assert turn.rotate_original(new, step).ok
    assert turn.only_turned(old, new)


def test_a_jpeg_turned_twice_is_still_the_same_picture(tmp_path):
    old = _jpeg(tmp_path / "a" / "x.jpg")
    new = tmp_path / "b" / "x.jpg"
    new.parent.mkdir()
    shutil.copyfile(old, new)
    assert turn.rotate_original(new, 90).ok and turn.rotate_original(new, 90).ok
    assert turn.only_turned(old, new)


def test_another_photograph_or_the_same_file_is_not_a_turn(tmp_path):
    old = _jpeg(tmp_path / "a" / "x.jpg")
    other = _jpeg(tmp_path / "b" / "x.jpg", shade=40)
    assert not turn.only_turned(old, other)
    same = tmp_path / "c" / "x.jpg"
    same.parent.mkdir()
    shutil.copyfile(old, same)
    assert not turn.only_turned(old, same)
    renamed = tmp_path / "c" / "x.png"
    shutil.copyfile(old, renamed)
    assert not turn.only_turned(old, renamed)


def test_a_turned_png_is_known_by_its_pixels(tmp_path):
    old = _png(tmp_path / "x.png")
    (tmp_path / "b").mkdir()
    new = tmp_path / "b" / "x.png"
    shutil.copyfile(old, new)
    assert turn.rotate_original(new, 90).ok
    assert turn.only_turned(old, new)
    Image.new("RGB", (10, 20), (1, 2, 3)).save(new)
    assert not turn.only_turned(old, new)


@pytest.mark.skipif(not media.FFMPEG, reason="needs ffmpeg")
def test_a_turned_video_is_known_by_its_streams(tmp_path):
    old = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=160x120:rate=10:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(old)], check=True, capture_output=True)
    (tmp_path / "b").mkdir()
    new = tmp_path / "b" / "clip.mp4"
    shutil.copyfile(old, new)
    assert turn.rotate_original(new, 90).ok
    assert turn.only_turned(old, new)
    assert not turn.only_turned(old, old)


# -- the second copy ---------------------------------------------------------

REL = "2019/07/14/turned.jpg"


@pytest.fixture()
def house(scanned, tmp_path: Path):
    cfg, conn, scanner = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    library = Path(cfg.active_root)
    _jpeg(library / REL)
    scanner._run(library, full=True)                                   # noqa: SLF001
    services = build_services(cfg)
    services.scanner.stop()
    disk = tmp_path / "share"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    return {"conn": conn, "mirror": services.mirror, "disk": disk, "library": library,
            "scanner": scanner}


def _run(house):
    assert house["mirror"].start() == {"started": True}
    house["mirror"].join(30)
    status = house["mirror"].status()
    assert not status["error"], status["error"]
    return status


def _turn_in_the_library(house, step=90):
    path = house["library"] / REL
    assert turn.rotate_original(path, step).ok
    house["conn"].execute("UPDATE assets SET size=?, mtime=? WHERE rel_path=?",
                          (path.stat().st_size, path.stat().st_mtime, REL))
    house["conn"].commit()


def _beside(house):
    return sorted(p.name for p in (house["disk"] / REL).parent.iterdir())


def test_a_copy_made_by_hand_before_the_turn_is_set_aside_not_doubled(house):
    there = house["disk"] / REL
    there.parent.mkdir(parents=True)
    shutil.copyfile(house["library"] / REL, there)
    _turn_in_the_library(house)
    status = _run(house)
    assert status["owed"] == 0
    assert there.read_bytes() == (house["library"] / REL).read_bytes()
    names = _beside(house)
    assert not [n for n in names if "(2)" in n], names
    kept = [n for n in names if n.startswith("turned (before ")]
    assert len(kept) == 1 and turn.only_turned(there.parent / kept[0], there)


def test_a_found_copy_turned_later_is_set_aside_not_doubled(house):
    there = house["disk"] / REL
    there.parent.mkdir(parents=True)
    shutil.copyfile(house["library"] / REL, there)
    first = _run(house)
    assert first["already_there"] >= 1
    _turn_in_the_library(house, 180)
    _run(house)
    assert there.read_bytes() == (house["library"] / REL).read_bytes()
    names = _beside(house)
    assert not [n for n in names if "(2)" in n], names
    assert len([n for n in names if n.startswith("turned (before ")]) == 1
    row = house["conn"].execute("SELECT origin, stored_as FROM mirror_copies WHERE rel_path=?",
                                (REL,)).fetchone()
    assert row["origin"] == mirror_mod.COPIED and row["stored_as"] == ""


def test_a_different_photograph_at_its_place_is_still_left_alone(house):
    there = _jpeg(house["disk"] / REL, shade=10)
    before = there.read_bytes()
    _run(house)
    assert there.read_bytes() == before
    assert "turned (2).jpg" in _beside(house)
    assert not [n for n in _beside(house) if "(before" in n]
    assert os.path.exists(house["disk"] / "2019/07/14/turned (2).jpg")
