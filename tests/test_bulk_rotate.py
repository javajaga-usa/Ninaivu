"""Turning the files themselves, in bulk, from the gallery.

The promise being tested is narrow and worth stating: a bulk turn changes the
file on the disk, it never loses a pixel doing it, it keeps a copy of what was
there before, and it refuses — by name, without writing anything — the formats
it cannot turn losslessly.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN
from ninaivu.media import media
from ninaivu.storage import recycle
from ninaivu.server import turn


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def orientation(path: Path) -> int:
    with Image.open(path) as img:
        return int(img.getexif().get(0x0112, 1))


def jpeg(path: Path, size=(400, 200), exif: dict | None = None) -> Path:
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), (x % 256, y % 256, 128))
    if exif is None:
        img.save(path, "JPEG", quality=92)
    else:
        tags = Image.Exif()
        for key, value in exif.items():
            tags[key] = value
        img.save(path, "JPEG", quality=92, exif=tags)
    return path


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------

def test_the_eight_orientations_compose_like_rotations():
    # Turning a right way up photograph a quarter turn makes it orientation 6,
    # and three more quarter turns bring it back.
    value = 1
    for _ in range(4):
        value = turn.compose(value, 90)
    assert value == 1
    assert turn.compose(1, 90) == 6
    assert turn.compose(6, 90) == 3
    assert turn.compose(3, 90) == 8
    assert turn.compose(8, 90) == 1


def test_a_mirrored_photograph_stays_mirrored():
    # Orientations 2, 4, 5 and 7 are the mirrored ones. Turning one must never
    # quietly un-mirror it — that would be Ninaivu changing the photograph.
    mirrored = {2, 4, 5, 7}
    for start in sorted(mirrored):
        assert turn.compose(start, 90) in mirrored
        assert turn.compose(start, 180) in mirrored
        assert turn.compose(start, 270) in mirrored
    for start in (1, 3, 6, 8):
        for step in (90, 180, 270):
            assert turn.compose(start, step) not in mirrored


@pytest.mark.parametrize("value", [0, 90, 180, 270, 360, 450, -90])
def test_quarter_turns_are_accepted(value):
    assert turn.quarter(value) in (0, 90, 180, 270)


@pytest.mark.parametrize("value", [45, 1, "ninety", None, True, 12.5])
def test_anything_that_is_not_a_quarter_turn_is_refused(value):
    assert turn.quarter(value) is None


# ---------------------------------------------------------------------------
# JPEG: the whole point is that the pixels are never touched
# ---------------------------------------------------------------------------

def test_turning_a_jpeg_and_back_restores_it_byte_for_byte(tmp_path):
    path = jpeg(tmp_path / "photo.jpg", exif={0x0112: 6, 0x010F: "Acme"})
    before = digest(path)
    size_before = path.stat().st_size

    assert turn.rotate_original(path, 90).ok
    assert orientation(path) == 3
    # Not one byte more or fewer: only the two bytes of the tag changed.
    assert path.stat().st_size == size_before
    assert digest(path) != before

    assert turn.rotate_original(path, 270).ok
    assert orientation(path) == 6
    assert digest(path) == before


def test_turning_a_jpeg_keeps_every_other_piece_of_exif(tmp_path):
    path = jpeg(tmp_path / "photo.jpg", exif={
        0x0112: 1, 0x010F: "Acme", 0x0110: "Model X",
        0x9003: "2021:05:04 10:11:12",
    })
    assert turn.rotate_original(path, 90).ok
    with Image.open(path) as img:
        tags = img.getexif()
    assert tags.get(0x010F) == "Acme"
    assert tags.get(0x0110) == "Model X"
    assert tags.get(0x9003) == "2021:05:04 10:11:12"
    assert tags.get(0x0112) == 6


def test_a_jpeg_with_exif_but_no_orientation_gets_one(tmp_path):
    # Screenshots and anything re-saved by an editor land here. The tag has to
    # be inserted into a directory that already has entries, which means every
    # offset in the block moves — get that wrong and the camera details, the
    # capture date and the embedded thumbnail all point into the wrong place.
    path = jpeg(tmp_path / "edited.jpg", exif={
        0x010F: "NoOrientCam", 0x0131: "SomeSoftware 1.2",
        0x9003: "2019:01:02 03:04:05",
    })
    with Image.open(path) as img:
        assert img.getexif().get(0x0112) is None

    assert turn.rotate_original(path, 90).ok
    assert orientation(path) == 6
    with Image.open(path) as img:
        tags = img.getexif()
    assert tags.get(0x010F) == "NoOrientCam"
    assert tags.get(0x0131) == "SomeSoftware 1.2"
    assert tags.get(0x9003) == "2019:01:02 03:04:05"


def test_a_jpeg_with_no_exif_at_all_gets_a_fresh_block(tmp_path):
    path = jpeg(tmp_path / "bare.jpg")
    assert turn.rotate_original(path, 180).ok
    assert orientation(path) == 3
    with Image.open(path) as img:
        assert img.size == (400, 200)     # the stored pixels are unchanged


def test_the_stored_pixels_are_never_re_encoded(tmp_path):
    # The compressed scan is the part that would degrade. Whatever else
    # changes, those bytes have to come out identical.
    path = jpeg(tmp_path / "photo.jpg", exif={0x0112: 1, 0x010F: "Acme"})
    before = path.read_bytes()
    scan_before = before[before.index(b"\xff\xda"):]
    assert turn.rotate_original(path, 90).ok
    after = path.read_bytes()
    assert after[after.index(b"\xff\xda"):] == scan_before


# ---------------------------------------------------------------------------
# The formats with no tag, and the ones that are refused
# ---------------------------------------------------------------------------

def test_a_png_has_its_pixels_turned(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (400, 200), (10, 20, 30)).save(path)
    result = turn.rotate_original(path, 90)
    assert result.ok and result.how == "pixels"
    with Image.open(path) as img:
        assert img.size == (200, 400)


@pytest.mark.parametrize("name,fmt", [("anim.gif", "GIF"), ("photo.webp", "WEBP")])
def test_formats_that_would_need_re_encoding_are_refused(tmp_path, name, fmt):
    path = tmp_path / name
    Image.new("RGB", (40, 20), (10, 20, 30)).save(path, fmt)
    before = digest(path)
    result = turn.rotate_original(path, 90)
    assert not result.ok
    assert "re-encoding" in result.why
    assert digest(path) == before        # and it was not touched


def test_a_raw_file_is_never_rewritten(tmp_path):
    path = tmp_path / "frame.cr2"
    path.write_bytes(b"II*\x00" + b"\x00" * 64)
    result = turn.rotate_original(path, 90)
    assert not result.ok
    assert "RAW" in result.why


def test_a_turn_of_zero_changes_nothing(tmp_path):
    path = jpeg(tmp_path / "photo.jpg", exif={0x0112: 6})
    before = digest(path)
    result = turn.rotate_original(path, 0)
    assert result.ok and result.unchanged
    assert digest(path) == before


def test_a_file_that_is_not_there_is_reported_not_crashed(tmp_path):
    result = turn.rotate_original(tmp_path / "gone.jpg", 90)
    assert not result.ok and "not there" in result.why


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not media.FFMPEG, reason="needs ffmpeg")


@pytest.fixture()
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=size=160x120:rate=10:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)
    return path


@needs_ffmpeg
def test_a_video_is_turned_by_rewriting_the_container(clip):
    assert turn.video_rotation(clip) == 0
    result = turn.rotate_original(clip, 90)
    assert result.ok and result.how == "container"
    assert turn.video_rotation(clip) == 90


@needs_ffmpeg
def test_turning_a_video_four_times_brings_it_back(clip):
    for expected in (90, 180, 270, 0):
        assert turn.rotate_original(clip, 90).ok
        assert turn.video_rotation(clip) == expected


@needs_ffmpeg
def test_a_video_turn_copies_the_streams_rather_than_re_encoding(clip, tmp_path):
    def frame_sizes(path):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=size", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True)
        return out.stdout.strip()

    before = frame_sizes(clip)
    assert before
    assert turn.rotate_original(clip, 90).ok
    # Identical compressed frames means not one of them was decoded.
    assert frame_sizes(clip) == before


@needs_ffmpeg
def test_a_container_with_nowhere_to_put_a_rotation_is_refused(clip, tmp_path):
    avi = tmp_path / "old.avi"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip),
                    "-c:v", "mpeg4", str(avi)], check=True, capture_output=True)
    before = digest(avi)
    result = turn.rotate_original(avi, 90)
    assert not result.ok
    assert digest(avi) == before


# ---------------------------------------------------------------------------
# Keeping the original
# ---------------------------------------------------------------------------

def test_the_first_version_is_kept_and_later_ones_are_not(tmp_path):
    root = tmp_path / "lib"
    (root / "2023/05").mkdir(parents=True)
    path = jpeg(root / "2023/05/photo.jpg", exif={0x0112: 1})
    pristine = digest(path)

    kept = recycle.keep_original(root, "2023/05/photo.jpg")
    assert kept is not None and digest(kept) == pristine

    turn.rotate_original(path, 90)
    # What the rotate endpoint does after every write, so the turned file is
    # still recognised as the one this copy came from.
    recycle.note_rewritten(kept, root, "2023/05/photo.jpg")
    # A second turn must not overwrite the copy with the once-turned version:
    # what is worth keeping is what the camera wrote.
    assert recycle.keep_original(root, "2023/05/photo.jpg") is None
    assert recycle.kept_copy_for(root, "2023/05/photo.jpg") == kept
    assert digest(kept) == pristine


def test_a_different_photograph_at_the_same_path_gets_its_own_copy(tmp_path):
    """Camera counters repeat. A new IMG_0001 must not be turned with no net."""
    root = tmp_path / "lib"
    (root / "2023/05").mkdir(parents=True)
    path = jpeg(root / "2023/05/photo.jpg", exif={0x0112: 1})
    first = recycle.keep_original(root, "2023/05/photo.jpg")
    assert first is not None

    path.unlink()
    replacement = jpeg(root / "2023/05/photo.jpg", size=(300, 150), exif={0x0112: 1})
    pristine = digest(replacement)
    second = recycle.keep_original(root, "2023/05/photo.jpg")
    assert second is not None and second != first
    assert digest(second) == pristine
    assert recycle.keep_original(root, "2023/05/photo.jpg") is None


def test_a_replacement_that_reuses_the_file_id_still_gets_its_own_copy(tmp_path):
    """Linux hands a freed inode to the next file made, so a replacement very
    often has the old photograph's id. Rewriting in place forces that case
    here on every filesystem: same path, same id, different photograph."""
    root = tmp_path / "lib"
    (root / "2023/05").mkdir(parents=True)
    path = jpeg(root / "2023/05/photo.jpg", exif={0x0112: 1})
    first = recycle.keep_original(root, "2023/05/photo.jpg")
    inode = path.stat().st_ino

    other = jpeg(tmp_path / "other.jpg", size=(300, 150), exif={0x0112: 1})
    path.write_bytes(other.read_bytes())
    assert path.stat().st_ino == inode
    pristine = digest(path)

    second = recycle.keep_original(root, "2023/05/photo.jpg")
    assert second is not None and second != first
    assert digest(second) == pristine


def test_the_same_bytes_under_a_new_file_id_are_the_same_photograph(tmp_path):
    """A library copied to another disk, or a file a sync tool rewrote with
    identical bytes, is the photograph the copy was taken from."""
    root = tmp_path / "lib"
    (root / "2023/05").mkdir(parents=True)
    path = jpeg(root / "2023/05/photo.jpg", exif={0x0112: 1})
    kept = recycle.keep_original(root, "2023/05/photo.jpg")

    copy = tmp_path / "copy.jpg"
    copy.write_bytes(path.read_bytes())
    copy.replace(path)

    assert recycle.keep_original(root, "2023/05/photo.jpg") is None
    assert recycle.kept_copy_for(root, "2023/05/photo.jpg") == kept


def test_the_kept_original_lives_where_a_person_can_find_it(tmp_path):
    root = tmp_path / "lib"
    (root / "holiday").mkdir(parents=True)
    jpeg(root / "holiday/beach.jpg")
    kept = recycle.keep_original(root, "holiday/beach.jpg")
    assert kept == root / recycle.BIN_NAME / recycle.ORIGINALS / "holiday/beach.jpg"


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def library_ids(client, limit=200):
    return [item["id"] for item in
            client.get(f"/api/assets?limit={limit}").get_json()["items"]]


def test_only_an_admin_may_turn_the_files(as_family, as_guest):
    for client in (as_family, as_guest):
        ids = library_ids(client)
        response = client.post("/api/rotate", json={"ids": ids[:2], "rotation": 90})
        assert response.status_code in (401, 403)


def test_a_bulk_turn_rewrites_every_selected_file(as_admin, scanned):
    cfg, conn, _ = scanned
    ids = library_ids(as_admin)[:3]
    rows = [conn.execute("SELECT root, rel_path FROM assets WHERE id=?", (i,)
                         ).fetchone() for i in ids]
    paths = [Path(r["root"]) / r["rel_path"] for r in rows]
    before = {p: orientation(p) for p in paths}

    response = as_admin.post("/api/rotate", json={"ids": ids, "rotation": 90,
                                                  "password": ADMIN[1]})
    assert response.status_code == 200
    body = response.get_json()
    assert body["rotated"] == 3, body

    for path in paths:
        if path.suffix.lower() in turn.JPEG_EXTS:
            assert orientation(path) == turn.compose(before[path], 90)
        else:
            # PNG has no orientation tag, so the pixels themselves moved.
            with Image.open(path) as img:
                assert img.size[0] < img.size[1] or img.size[0] == img.size[1]


def test_the_index_stops_claiming_a_turn_the_file_now_carries(as_admin, scanned):
    # Both applying it would show the photograph turned twice.
    cfg, conn, _ = scanned
    ids = library_ids(as_admin)[:2]
    as_admin.post("/api/rotate", json={"ids": ids, "rotation": 90,
                                       "password": ADMIN[1]})
    for asset_id in ids:
        row = conn.execute("SELECT rotation, rot_source, width, height "
                           "FROM assets WHERE id=?", (asset_id,)).fetchone()
        assert row["rotation"] == 0
        assert row["rot_source"] in ("exif", "file")


def test_a_bulk_turn_updates_the_recorded_shape(as_admin, scanned):
    cfg, conn, _ = scanned
    asset_id = library_ids(as_admin)[0]
    row = conn.execute("SELECT width, height FROM assets WHERE id=?",
                       (asset_id,)).fetchone()
    was = (row["width"], row["height"])
    as_admin.post("/api/rotate", json={"ids": [asset_id], "rotation": 90,
                                       "password": ADMIN[1]})
    row = conn.execute("SELECT width, height FROM assets WHERE id=?",
                       (asset_id,)).fetchone()
    assert (row["width"], row["height"]) == (was[1], was[0])


def test_the_thumbnails_are_rebuilt_from_the_turned_file(as_admin, scanned):
    cfg, conn, _ = scanned
    asset_id = library_ids(as_admin)[0]
    row = conn.execute("SELECT thumb FROM assets WHERE id=?",
                       (asset_id,)).fetchone()
    thumb = cfg.thumbs_dir / media.thumb_file(row["thumb"], cfg.thumb_sizes[0],
                                              cfg.thumb_format)
    with Image.open(thumb) as img:
        was = img.size
    as_admin.post("/api/rotate", json={"ids": [asset_id], "rotation": 90,
                                       "password": ADMIN[1]})
    with Image.open(thumb) as img:
        assert img.size == (was[1], was[0])


def test_a_bulk_turn_keeps_the_originals_first(as_admin, scanned):
    cfg, conn, _ = scanned
    ids = library_ids(as_admin)[:2]
    rows = [conn.execute("SELECT root, rel_path FROM assets WHERE id=?", (i,)
                         ).fetchone() for i in ids]
    pristine = {r["rel_path"]: digest(Path(r["root"]) / r["rel_path"])
                for r in rows}

    as_admin.post("/api/rotate", json={"ids": ids, "rotation": 90,
                                       "password": ADMIN[1]})

    for row in rows:
        kept = (recycle.bin_path(row["root"]) / recycle.ORIGINALS
                / row["rel_path"])
        assert kept.exists(), f"no copy kept of {row['rel_path']}"
        assert digest(kept) == pristine[row["rel_path"]]


def test_what_could_not_be_turned_is_named_rather_than_silently_dropped(
        as_admin, scanned, tmp_path):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    Image.new("RGB", (40, 20), (1, 2, 3)).save(root / "misc" / "loop.gif", "GIF")

    from ninaivu.media.scanner import Scanner
    Scanner(cfg)._run(root, full=True)

    row = conn.execute("SELECT id FROM assets WHERE filename='loop.gif'"
                       ).fetchone()
    if row is None:
        pytest.skip("the scanner does not index GIFs in this configuration")

    response = as_admin.post("/api/rotate", json={"ids": [row["id"]], "rotation": 90,
                                                  "password": ADMIN[1]})
    body = response.get_json()
    assert body["rotated"] == 0
    assert body["skipped"] and body["skipped"][0]["name"] == "loop.gif"
    assert body["skipped"][0]["why"]


@pytest.mark.parametrize("value", [45, "sideways", None])
def test_the_endpoint_refuses_anything_but_a_quarter_turn(as_admin, value):
    ids = library_ids(as_admin)[:1]
    response = as_admin.post("/api/rotate", json={"ids": ids, "rotation": value,
                                                  "password": ADMIN[1]})
    assert response.status_code == 400


def test_an_id_outside_the_library_reaches_nothing(as_admin):
    response = as_admin.post("/api/rotate", json={"ids": [999999], "rotation": 90,
                                                  "password": ADMIN[1]})
    assert response.get_json()["rotated"] == 0
