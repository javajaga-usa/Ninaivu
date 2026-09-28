"""Where a photograph was taken stays in the house.

A phone writes its GPS position into every photograph, and the gallery already
kept it from guests: their payload has no GPS, no camera, no EXIF at all. But
the image itself was the original file, and the original's bytes carried all of
it — to a guest who saved the picture, and to anybody at all holding a share
link. For a photograph taken at home, that is the family's address.

So a guest and a link holder are given the viewing copy: the picture, upright,
re-encoded with no metadata. Family members and admins still get the original.
"""

import io
from pathlib import Path

import pytest
from PIL import Image, ExifTags

from conftest import GUEST, login
from ninaivu.storage import db

GPS = {1: "N", 2: (51.0, 30.0, 26.0), 3: "W", 4: (0.0, 7.0, 39.0)}


def _located_photo(path: Path, size=(400, 200), orientation=6) -> Path:
    """A photograph as a phone writes one: sideways pixels, an orientation tag
    saying so, a camera name and a position."""
    img = Image.new("RGB", size, (200, 80, 40))
    for x in range(0, size[0], 7):
        img.putpixel((x, x % size[1]), (10, 200, 90))
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x0110] = "TestPhone"
    exif[ExifTags.IFD.GPSInfo] = GPS
    img.save(path, "JPEG", quality=92, exif=exif)
    return path


def _has_location(data: bytes) -> bool:
    with Image.open(io.BytesIO(data)) as img:
        exif = img.getexif()
        return bool(exif.get_ifd(ExifTags.IFD.GPSInfo)) or 0x0110 in exif


@pytest.fixture()
def located(app, people, scanned):
    """A public photograph with a position in it, and a GIF beside it."""
    from ninaivu.media.scanner import Scanner
    cfg, conn, _ = scanned
    library = Path(cfg.active_root)
    (library / "garden").mkdir(exist_ok=True)
    original = _located_photo(library / "garden" / "IMG_4410.jpg")
    frames = [Image.new("RGB", (60, 40), c) for c in ((255, 0, 0), (0, 0, 255))]
    gif = library / "garden" / "wave.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
    Scanner(cfg)._run(library, full=True)

    ids = {}
    for name in ("IMG_4410.jpg", "wave.gif"):
        row = conn.execute("SELECT id FROM assets WHERE filename=?", (name,)).fetchone()
        assert row is not None, f"{name} was not indexed"
        ids[name] = row["id"]
        conn.execute("UPDATE assets SET visibility=0 WHERE id=?", (row["id"],))
    conn.commit()
    return {"jpg": ids["IMG_4410.jpg"], "gif": ids["wave.gif"],
            "original": original.read_bytes(), "gif_bytes": gif.read_bytes(),
            "conn": conn}


def test_the_test_photograph_really_has_a_location(located):
    assert _has_location(located["original"])


def test_a_guest_gets_the_picture_without_its_location(app, located):
    guest = login(app.test_client(), *GUEST)
    response = guest.get(f"/api/file/{located['jpg']}")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    data = response.get_data()
    assert data != located["original"]
    assert not _has_location(data), "a guest was handed the GPS position"
    assert b"Exif" not in data
    # Turned the way the tag said, since the tag is gone: sideways pixels
    # (400 x 200) with orientation 6 are a portrait photograph.
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (200, 400)
    assert "Cookie" in response.headers.get("Vary", "")


def test_open_browsing_without_signing_in_is_a_guest_too(app, located):
    response = app.test_client().get(f"/api/file/{located['jpg']}")
    if response.status_code == 200:
        assert not _has_location(response.get_data())
    else:
        assert response.status_code in (401, 403, 404)


def test_family_members_still_get_the_original(as_family, located):
    response = as_family.get(f"/api/file/{located['jpg']}")
    assert response.status_code == 200
    assert response.get_data() == located["original"]
    assert "Cookie" in response.headers.get("Vary", "")


def test_a_gif_still_moves_for_a_guest(app, located):
    """GIF has nowhere to keep a position, and a copy would stop it moving."""
    guest = login(app.test_client(), *GUEST)
    response = guest.get(f"/api/file/{located['gif']}")
    assert response.status_code == 200
    assert response.get_data() == located["gif_bytes"]


def test_a_share_link_gives_the_picture_without_its_location(app, as_family, located):
    share = as_family.post("/api/shares", json={
        "scope": "asset", "target_id": located["jpg"]}).get_json()
    stranger = app.test_client()
    item = stranger.get(f"/api/share/{share['token']}").get_json()["item"]
    for url in {item["src"], item["view"]}:
        response = stranger.get(url)
        assert response.status_code == 200, url
        assert not _has_location(response.get_data()), f"{url} carried the position"


def test_a_shared_album_gives_every_picture_without_its_location(app, as_family, located):
    made = as_family.post("/api/albums", json={"name": "Garden"}).get_json()
    as_family.post(f"/api/albums/{made['id']}/items", json={"ids": [located["jpg"]]})
    share = as_family.post("/api/shares", json={
        "scope": "album", "target_id": made["id"]}).get_json()
    stranger = app.test_client()
    for item in stranger.get(f"/api/share/{share['token']}").get_json()["items"]:
        assert not _has_location(stranger.get(item["src"]).get_data())


def test_a_photograph_replaced_in_place_is_not_answered_with_the_old_copy(app, located):
    """The copy's tag carries the file's time, so a revalidation after the
    photograph changed gets the new picture rather than a 304."""
    guest = login(app.test_client(), *GUEST)
    first = guest.get(f"/api/file/{located['jpg']}")
    tag = first.headers["ETag"]
    assert guest.get(f"/api/file/{located['jpg']}",
                     headers={"If-None-Match": tag}).status_code == 304

    conn = located["conn"]
    row = db.get_asset(conn, located["jpg"])
    path = Path(row["root"]) / row["rel_path"]
    _located_photo(path, size=(300, 300), orientation=1)
    conn.execute("UPDATE assets SET mtime=? WHERE id=?",
                 (path.stat().st_mtime + 5, located["jpg"]))
    conn.commit()
    again = guest.get(f"/api/file/{located['jpg']}", headers={"If-None-Match": tag})
    assert again.status_code == 200
    with Image.open(io.BytesIO(again.get_data())) as img:
        assert img.size == (300, 300)
