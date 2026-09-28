"""Photographs a browser cannot decode, shown in the browser.

A library shot on phones is largely HEIC, and no browser reads it. Ninaivu
could always make a thumbnail — that is why they appear in the grid at all —
but opening one showed the 640-pixel thumbnail with "needs an external viewer"
underneath, and the Photo Studio refused them outright.
"""

from pathlib import Path

import pytest
from PIL import Image, ImageOps

from ninaivu.media import stills


# ---------------------------------------------------------------------------
# What needs converting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext", ["heic", "heif", "cr2", "nef", "arw", "dng", "tif"])
def test_formats_a_browser_cannot_read_need_a_copy(ext):
    assert stills.needs_rendition(ext) is True


@pytest.mark.parametrize("ext", ["jpg", "jpeg", "png", "webp", "gif", "avif"])
def test_formats_it_can_read_are_served_as_they_are(ext):
    assert stills.needs_rendition(ext) is False


def test_a_video_is_never_a_still():
    assert stills.needs_rendition("mkv", "video") is False


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

def _tiff(path: Path, size=(3000, 2000)) -> Path:
    """A stand-in for HEIC: a real format, and one no browser opens either."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (90, 130, 180)).save(path, "TIFF")
    return path


def _orient(path):
    return ImageOps.exif_transpose(Image.open(path))


def test_a_copy_is_built_once_and_read_thereafter(tmp_path):
    source = _tiff(tmp_path / "shot.tif")
    store = stills.StillStore(tmp_path / "state")
    assert store.ready(1) is None

    built = store.build(1, source, orient=_orient)
    assert built and built.exists()
    assert store.ready(1) == built

    # Not the modification time: `ready` deliberately touches the file, because
    # eviction is by least-recently-*used* and reading does not update mtime on
    # its own. The inode is what changes when a copy is genuinely rewritten —
    # a rebuild renames a fresh file into place.
    inode = built.stat().st_ino
    again = store.build(1, source, orient=_orient)
    assert again == built
    assert built.stat().st_ino == inode, "it should not have been rebuilt"


def test_looking_at_a_copy_keeps_it_alive(tmp_path):
    """Eviction is by least-recently-used, so a read has to count as a use."""
    import os
    import time

    store = stills.StillStore(tmp_path / "state")
    built = store.build(9, _tiff(tmp_path / "keep.tif"), orient=_orient)
    old_time = time.time() - 3600
    os.utime(built, (old_time, old_time))
    assert store.ready(9) is not None
    assert built.stat().st_mtime > old_time + 60, "a view should refresh its place in the queue"


def test_the_copy_is_a_jpeg_a_browser_can_open(tmp_path):
    store = stills.StillStore(tmp_path / "state")
    built = store.build(2, _tiff(tmp_path / "s.tif"), orient=_orient)
    with Image.open(built) as out:
        assert out.format == "JPEG"


def test_the_copy_is_capped_rather_than_full_size(tmp_path):
    """A viewing copy, not a second original — or the cache outgrows the library."""
    store = stills.StillStore(tmp_path / "state")
    built = store.build(3, _tiff(tmp_path / "big.tif", (6000, 4000)), orient=_orient)
    with Image.open(built) as out:
        assert max(out.size) == stills.MAX_EDGE
        assert out.size[0] > out.size[1], "the shape must survive"


def test_nothing_is_written_beside_the_photograph(tmp_path):
    """The library folder is the household's. Ninaivu's working files are not."""
    library = tmp_path / "library"
    source = _tiff(library / "shot.tif")
    before = sorted(p.name for p in library.iterdir())
    stills.StillStore(tmp_path / "state").build(4, source, orient=_orient)
    assert sorted(p.name for p in library.iterdir()) == before


def test_an_unreadable_file_is_a_shrug_not_a_crash(tmp_path):
    broken = tmp_path / "broken.tif"
    broken.write_bytes(b"not a tiff at all")
    assert stills.StillStore(tmp_path / "state").build(5, broken, orient=_orient) is None


def test_the_cache_is_bounded(tmp_path):
    """Left alone, full-size copies beside a big library are a second library."""
    store = stills.StillStore(tmp_path / "state", cache_mb=0)
    store.build(6, _tiff(tmp_path / "a.tif"), orient=_orient)
    store.build(7, _tiff(tmp_path / "b.tif"), orient=_orient)
    remaining = list(stills.renditions_dir(tmp_path / "state").glob("*.jpg"))
    assert len(remaining) <= 1, "a zero budget should evict almost everything"


def test_the_version_is_in_the_filename(tmp_path):
    """So changing the encode retires every old copy without a migration."""
    path = stills.rendition_path(tmp_path, 42)
    assert path.name == f"42-v{stills.RENDITION_VERSION}.jpg"


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_an_ordinary_jpeg_is_served_as_itself(as_family):
    """No second copy of something the browser already reads."""
    item = as_family.get("/api/assets?limit=1").get_json()["items"][0]
    assert item["view"] == f"/api/file/{item['id']}"
    assert as_family.get(item["view"]).status_code == 200


def test_every_photograph_says_where_its_viewable_copy_is(as_family):
    for item in as_family.get("/api/assets?limit=20").get_json()["items"]:
        if item["kind"] != "picture":
            continue
        assert item.get("view"), f"{item['name']} has nowhere to be viewed from"


def test_a_guest_may_open_a_photograph_they_can_already_see(as_guest, scanned):
    """The preview sits at thumbnail level, not download level.

    It is a resized, re-encoded, metadata-free copy of a picture the guest is
    already being shown in the grid. Putting it behind the download permission
    would mean a guest could see a photograph and not open it.
    """
    _, conn, _ = scanned
    # The fixture library has no public photograph, so this used to skip on
    # every run and never check anything. Make one public, as an admin would.
    row = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET visibility=0, vis_source='item' WHERE id=?", (row["id"],))
    conn.commit()
    assert as_guest.get(f"/api/preview/{row['id']}").status_code == 200


def test_a_video_has_no_still_preview(as_family, scanned):
    _, conn, _ = scanned
    row = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET kind='video' WHERE id=?", (row["id"],))
    conn.commit()
    assert as_family.get(f"/api/preview/{row['id']}").status_code == 404


def test_a_hidden_photograph_is_not_reachable_through_the_preview(as_guest, scanned):
    """A different URL for the same pixels is still the same pixels."""
    _, conn, _ = scanned
    row = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (row["id"],))
    conn.commit()
    assert as_guest.get(f"/api/preview/{row['id']}").status_code == 404
