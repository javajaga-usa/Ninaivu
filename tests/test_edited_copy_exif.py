"""An edited copy keeps where, when and with what its photograph was taken."""
import io
import json
from pathlib import Path

import pytest
from PIL import Image, TiffImagePlugin

from conftest import ids_of
from test_date_access import dates as dates
from ninaivu.storage import db
from ninaivu.media import media, upload_review


def _rational(num, den=1):
    return TiffImagePlugin.IFDRational(num, den)


SHOT = {
    "camera": "Canon EOS R6", "lens": "RF50mm F1.8 STM", "iso": 400,
    "f_number": 2.8, "exposure": "1/250s", "focal_length": 50.0,
    "gps_lat": 13.075042, "gps_lon": 80.266667,
}


def _rich_source(path: Path, orientation: int = 6) -> None:
    """Rewrite *path* as a JPEG with a camera, lens, exposure, GPS and a thumbnail's worth of junk."""
    img = Image.new("RGB", (80, 60), (120, 140, 160))
    exif = img.getexif()
    exif[0x010F] = "Canon"
    exif[0x0110] = "Canon EOS R6"
    exif[0x0112] = orientation
    exif[0x8298] = "The family"
    shot = exif.get_ifd(0x8769)
    shot[0x9003] = "2020:01:02 03:04:05"
    shot[0x829A] = _rational(1, 250)
    shot[0x829D] = _rational(28, 10)
    shot[0x8827] = 400
    shot[0x920A] = _rational(50)
    shot[0xA434] = "RF50mm F1.8 STM"
    shot[0x927C] = b"private maker notes"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[3] = "N", "E"
    gps[2] = (_rational(13), _rational(4), _rational(3015, 100))
    gps[4] = (_rational(80), _rational(16), _rational(0))
    img.save(path, "JPEG", exif=exif)


def _prepare(conn, asset_id, **shot):
    """Give a library photograph a rich file and the row a scan would have made of it."""
    asset = db.get_asset(conn, asset_id)
    _rich_source(Path(asset["root"]) / asset["rel_path"], **shot)
    db.update_asset(conn, asset_id, country="India", city="Chennai", **SHOT)
    return db.get_asset(conn, asset_id)


def _encoded(fmt):
    stream = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 120, 90)).save(stream, fmt)
    return stream.getvalue()


def _assert_shot(info):
    for key, value in SHOT.items():
        assert info.get(key) == value, (key, info)


def _assert_row(row):
    for key, value in {**SHOT, "country": "India", "city": "Chennai"}.items():
        assert row[key] == value, (key, row[key])


@pytest.mark.parametrize("mimetype,fmt", [
    ("image/jpeg", "JPEG"), ("image/webp", "WEBP"), ("image/png", "PNG"),
])
def test_an_admins_copy_carries_camera_lens_exposure_and_gps(as_admin, scanned, mimetype, fmt):
    _, conn, _ = scanned
    target = ids_of(as_admin)[0]
    source = _prepare(conn, target)
    response = as_admin.post(f"/api/asset/{target}/edited-copy",
                             data=_encoded(fmt), content_type=mimetype)
    assert response.status_code == 201, response.get_json()
    copy = db.get_asset(conn, response.get_json()["id"])
    _assert_row(copy)
    with Image.open(Path(copy["root"]) / copy["rel_path"]) as image:
        assert image.format == fmt
        info = media.read_exif(image)
        exif = image.getexif()
        _assert_shot(info)
        # The editor bakes the turn into the pixels, so the copy must not be turned again.
        assert info["orientation"] == 1
        assert exif[0x0131] == "Ninaivu Photo Studio"
        assert exif[0x8298] == "The family"
        assert 0x927C not in exif.get_ifd(0x8769)
        assert abs(info["captured_at"] - source["captured_at"]) < 1


def test_a_source_without_exif_still_saves(as_admin, scanned):
    _, conn, _ = scanned
    target = ids_of(as_admin)[0]
    source = db.get_asset(conn, target)
    Image.new("RGB", (80, 60), (10, 20, 30)).save(Path(source["root"]) / source["rel_path"], "JPEG")
    response = as_admin.post(f"/api/asset/{target}/edited-copy",
                             data=_encoded("JPEG"), content_type="image/jpeg")
    assert response.status_code == 201, response.get_json()
    copy = db.get_asset(conn, response.get_json()["id"])
    with Image.open(Path(copy["root"]) / copy["rel_path"]) as image:
        info = media.read_exif(image)
        assert info["orientation"] == 1
        assert "camera" not in info and "gps_lat" not in info
        assert image.getexif()[0x0131] == "Ninaivu Photo Studio"
        assert abs(info["captured_at"] - source["captured_at"]) < 1


def test_an_unreadable_source_still_saves(as_admin, scanned):
    """A source Pillow cannot open costs the copy its camera, not the edit."""
    _, conn, _ = scanned
    target = ids_of(as_admin)[0]
    source = db.get_asset(conn, target)
    (Path(source["root"]) / source["rel_path"]).write_bytes(b"not a picture at all")
    response = as_admin.post(f"/api/asset/{target}/edited-copy",
                             data=_encoded("WEBP"), content_type="image/webp")
    assert response.status_code == 201, response.get_json()


def test_a_family_members_edit_carries_the_same_fields_through_approval(dates):
    cfg, conn, _, _, family, admin = dates
    target = ids_of(family)[0]
    _prepare(conn, target)
    response = family.post(f"/api/asset/{target}/edited-copy",
                           data=_encoded("JPEG"), content_type="image/jpeg")
    assert response.status_code == 202, response.get_json()
    pending_id = response.get_json()["pending_id"]

    pending = upload_review.get(conn, pending_id)
    _assert_row(json.loads(pending["record"]))
    with Image.open(upload_review.source_path(cfg, pending)) as image:
        info = media.read_exif(image)
        _assert_shot(info)
        assert info["orientation"] == 1

    approved = admin.post(f"/api/admin/uploads/{pending_id}/approve", json={})
    assert approved.status_code == 200, approved.get_json()
    copy = db.get_asset(conn, approved.get_json()["item"]["id"])
    _assert_row(copy)
    with Image.open(Path(copy["root"]) / copy["rel_path"]) as image:
        _assert_shot(media.read_exif(image))
