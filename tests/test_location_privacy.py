"""Keeping home's location at home: the home zone, and copies without a location."""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, login
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.server import auth
from ninaivu.utils import location

HOME = (13.0827, 80.2707)


def jpeg_with_gps(path: Path, lat=HOME[0], lon=HOME[1]) -> None:
    image = Image.new("RGB", (64, 48), (200, 120, 40))
    exif = image.getexif()
    exif[0x0110] = "HomeCam"
    exif[0x9003] = "2024:05:01 10:00:00"
    gps = exif.get_ifd(0x8825)

    def dms(value):
        value = abs(value)
        d = int(value)
        m = int((value - d) * 60)
        return (float(d), float(m), round(((value - d) * 60 - m) * 60, 2))

    gps.update({1: "N", 2: dms(lat), 3: "E", 4: dms(lon)})
    image.save(path, "JPEG", exif=exif, quality=90)


def gps_of(data: bytes) -> dict:
    with Image.open(io.BytesIO(data)) as image:
        return dict(image.getexif().get_ifd(0x8825))


def test_the_gps_is_emptied_and_nothing_else_changes(tmp_path):
    path = tmp_path / "home.jpg"
    jpeg_with_gps(path)
    original = path.read_bytes()
    assert gps_of(original), "the test photograph has a location"
    stripped = location.strip_jpeg(original)
    assert gps_of(stripped) == {}
    assert len(stripped) == len(original), "emptied in place, every offset kept"
    with Image.open(io.BytesIO(stripped)) as a, Image.open(io.BytesIO(original)) as b:
        assert a.tobytes() == b.tobytes(), "the picture itself is untouched"
        assert a.getexif()[0x0110] == "HomeCam"
    start = original.index(b"\xff\xda")
    assert stripped[stripped.index(b"\xff\xda"):] == original[start:]


def test_an_xmp_packet_is_left_out():
    image = Image.new("RGB", (8, 8))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    data = buffer.getvalue()
    xmp = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>exif:GPSLatitude=13,4.9N</x:xmpmeta>"
    segment = b"\xff\xe1" + (len(xmp) + 2).to_bytes(2, "big") + xmp
    with_xmp = data[:2] + segment + data[2:]
    assert b"GPSLatitude" not in location.strip_jpeg(with_xmp)


class Cfg:
    home_lat, home_lon, home_radius_m, strip_location = HOME[0], HOME[1], 300, "home"


@pytest.mark.parametrize("mode,lat,admin,expected", [
    ("home", HOME[0], False, True),
    ("home", HOME[0] + 0.01, False, False),        # about a kilometre away
    ("home", None, False, False),
    ("home", HOME[0], True, False),
    ("all", HOME[0] + 1, False, True),
    ("off", HOME[0], False, False),
])
def test_what_is_stripped(mode, lat, admin, expected):
    cfg = Cfg()
    cfg.strip_location = mode
    row = {"kind": "picture", "ext": "jpg", "gps_lat": lat, "gps_lon": HOME[1]}
    assert location.should_strip(cfg, row, is_admin=admin) is expected
    assert location.should_strip(cfg, {**row, "ext": "gif"}, is_admin=False) is False


@pytest.fixture()
def home(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE filename='shot1.jpg'").fetchone()
    path = Path(row["root"]) / row["rel_path"]
    jpeg_with_gps(path)
    conn.execute("UPDATE assets SET gps_lat=?, gps_lon=?, size=? WHERE id=?",
                 (*HOME, path.stat().st_size, row["id"]))
    conn.commit()
    cfg.home_lat, cfg.home_lon = HOME
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    yield {"cfg": cfg, "conn": conn, "services": services, "id": row["id"], "path": path,
           "family": login(app.test_client(), *FAMILY), "admin": login(app.test_client(), *ADMIN),
           "console": login(create_admin_app(services).test_client(), *ADMIN)}
    services.stop(timeout=5.0)


def test_a_family_download_of_a_home_photograph_has_no_location(home):
    got = home["family"].get(f"/api/download/{home['id']}")
    assert got.status_code == 200 and gps_of(got.data) == {}
    assert home["path"].read_bytes() != got.data, "a copy"
    assert gps_of(home["path"].read_bytes()), "the library's own is untouched"
    copies = list((Path(home["cfg"].state_dir) / "private-copies").glob("*.jpg"))
    assert len(copies) == 1, "kept in the state folder, not the library"
    admin = home["admin"].get(f"/api/download/{home['id']}")
    assert admin.data == home["path"].read_bytes(), "an administrator gets the original"


def test_a_zip_holds_the_copies_too(home):
    got = home["family"].get(f"/api/download/zip?ids={home['id']}")
    with zipfile.ZipFile(io.BytesIO(got.data)) as archive:
        [name] = [n for n in archive.namelist() if n.endswith(".jpg")]
        assert gps_of(archive.read(name)) == {}


def test_with_stripping_off_the_original_goes(home):
    home["cfg"].strip_location = "off"
    got = home["family"].get(f"/api/download/{home['id']}")
    assert got.data == home["path"].read_bytes()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_video_leaves_without_its_location(tmp_path):
    from ninaivu.media import media
    media.FFMPEG = media.FFMPEG or shutil.which("ffmpeg")
    source = tmp_path / "clip.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1",
                    "-metadata", "location=+13.0827+080.2707/", "-c:v", "libx264", "-y",
                    str(source)], check=True)
    assert b"loci" in source.read_bytes(), "MP4 keeps the place in a loci box"
    copies = location.LocationFreeCopies(tmp_path / "state")
    out, name = copies.copy({"id": 1, "kind": "video", "ext": "mp4", "filename": "clip.mp4",
                             "captured_at": 1714557600}, source)
    assert b"loci" not in out.read_bytes() and name == "clip.mp4"


def test_the_console_sets_the_zone_and_suggests_home(home):
    data = home["console"].get("/api/admin/privacy").get_json()
    assert data["home_lat"] == HOME[0] and data["inside"] == 1
    assert data["suggestion"]["lat"] == round(HOME[0], 3)
    saved = home["console"].post("/api/admin/privacy", json={
        "home_lat": "12.5", "home_lon": "77.6", "home_radius_m": 1000, "strip_location": "all"}).get_json()
    assert saved["home_lat"] == 12.5 and saved["strip_location"] == "all" and saved["inside"] == 0
    assert home["console"].post("/api/admin/privacy", json={"home_lat": 99, "home_lon": 0}).status_code == 400
    assert home["console"].post("/api/admin/privacy", json={"strip_location": "some"}).status_code == 400
    cleared = home["console"].post("/api/admin/privacy", json={"clear": True}).get_json()
    assert cleared["home_lat"] is None


def test_opening_it_in_the_viewer_is_the_same_as_downloading_it(home):
    """The original is at /api/file too; leaving the place out of downloads
    alone would leave it a right-click away."""
    got = home["family"].get(f"/api/file/{home['id']}")
    assert got.status_code == 200 and gps_of(got.data) == {}
    admin = home["admin"].get(f"/api/file/{home['id']}")
    assert admin.data == home["path"].read_bytes()
    home["cfg"].strip_location = "off"
    assert home["family"].get(f"/api/file/{home['id']}").data == home["path"].read_bytes()
