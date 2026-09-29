"""Google Takeout: what the sidecars say and the albums come through.

The export keeps the location and the description beside each photograph in
a JSON sidecar (often the only place the location survives), and its albums
as folders with a metadata.json. Importing one used to keep the dates and
lose the rest.
"""

import json
import os
import random
from pathlib import Path

import pytest
from PIL import Image

from ninaivu import archive
from ninaivu.archive import database as adb
from ninaivu.archive import takeout
from ninaivu.archive.scanner import ArchiveJob
from ninaivu.storage import db


def jpeg(path: Path, colour="red"):
    """A JPEG big enough for the import, which leaves files under 60 KB
    alone (icons and thumbnails are not photographs); noise does not
    compress, so a small picture of it is plenty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(colour)
    img = Image.frombytes("RGB", (320, 240), bytes(rng.getrandbits(8) for _ in range(320 * 240 * 3)))
    img.save(path, "JPEG", quality=95)
    return path


def sidecar(path: Path, **fields):
    body = {"title": path.name, "photoTakenTime": {"timestamp": "1563183000"}}
    body.update(fields)
    (path.parent / f"{path.name}.supplemental-metadata.json").write_text(json.dumps(body), encoding="utf-8")


def takeout_export(root: Path) -> Path:
    """A small export: a year folder and one album, sharing a photograph."""
    photos = root / "Takeout" / "Google Photos"
    year = photos / "Photos from 2019"
    beach = jpeg(year / "IMG_0001.jpg", "blue")
    sidecar(beach, geoData={"latitude": 48.8566, "longitude": 2.3522, "altitude": 35.0},
            description="The Seine at dusk")
    pier = jpeg(year / "IMG_0002.jpg", "green")
    sidecar(pier, geoData={"latitude": 0.0, "longitude": 0.0})           # Google's "no location"
    album = photos / "Paris 2019"
    (album / "metadata.json").parent.mkdir(parents=True)
    (album / "metadata.json").write_text(json.dumps({"title": "Paris 2019", "date": {}}), encoding="utf-8")
    jpeg(album / "IMG_0001.jpg", "blue")                                  # the same bytes as in the year folder
    sidecar(album / "IMG_0001.jpg", geoData={"latitude": 48.8566, "longitude": 2.3522},
            description="The Seine at dusk")                               # Google repeats it per copy
    return photos


def test_the_sidecar_gives_a_location_and_a_description(tmp_path):
    photos = takeout_export(tmp_path)
    extra = takeout.extras(photos / "Photos from 2019" / "IMG_0001.jpg")
    assert extra == {"lat": 48.8566, "lon": 2.3522, "description": "The Seine at dusk"}
    assert takeout.extras(photos / "Photos from 2019" / "IMG_0002.jpg") == {}, "0,0 is no location"


def test_the_albums_are_found_and_the_year_folders_are_not(tmp_path):
    photos = takeout_export(tmp_path)
    (photos / "Photos from 2019" / "metadata.json").write_text(
        json.dumps({"title": "Photos from 2019"}), encoding="utf-8")
    found = takeout.albums_in([str(photos)])
    assert [(a["title"], len(a["files"])) for a in found] == [("Paris 2019", 1)]


def test_the_index_takes_the_location_from_the_sidecar(tmp_path):
    from ninaivu.media.scanner import build_record
    from ninaivu.server.config import Config
    photos = takeout_export(tmp_path)
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.ensure_dirs()
    root = photos / "Photos from 2019"
    record = build_record(root, "IMG_0001.jpg", os.stat(root / "IMG_0001.jpg"), cfg)
    assert (record.get("gps_lat"), record.get("gps_lon")) == (48.8566, 2.3522)
    assert record.get("caption") == "The Seine at dusk"
    plain = build_record(root, "IMG_0002.jpg", os.stat(root / "IMG_0002.jpg"), cfg)
    assert plain.get("gps_lat") is None


@pytest.fixture()
def archive_state(tmp_path):
    archive.configure(tmp_path / "archive-state")
    adb.close_db()
    adb.init_db()
    yield
    adb.close_db()


def test_albums_come_back_after_an_import(tmp_path, archive_state):
    photos = takeout_export(tmp_path)
    dest = tmp_path / "library"
    job = ArchiveJob([str(photos)], str(dest))
    job.run()
    rows = {r["source_path"]: r["status"] for r in adb.get_recent_files(50)}
    assert set(rows.values()) == {"verified", "duplicate"}, rows
    assert list(rows.values()).count("duplicate") == 1, "the album's copy is the year's photograph"

    # The library indexes the archive; then the albums are made from it.
    from ninaivu.media.scanner import Scanner
    from ninaivu.server.config import Config
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(dest)]
    cfg.active_root = str(dest)
    cfg.ensure_dirs()
    conn = db.init_db(cfg.db_path)                # the same index the scan writes to
    from ninaivu.server import auth
    auth.init_auth_schema(conn)                   # the scan reads the folder rules
    Scanner(cfg)._run(dest, full=True)

    result = takeout.recreate(conn, [str(dest)], [str(photos)])
    assert [(a["title"], a["added"]) for a in result["albums"]] == [("Paris 2019", 1)]
    assert result["unmatched"] == 0
    album = conn.execute("SELECT id FROM albums WHERE name='Paris 2019'").fetchone()
    member = conn.execute("SELECT a.filename, a.gps_lat, a.caption FROM album_items i "
                          "JOIN assets a ON a.id=i.asset_id WHERE i.album_id=?", (album["id"],)).fetchone()
    assert member["filename"] == "IMG_0001.jpg"
    assert member["gps_lat"] == 48.8566 and member["caption"] == "The Seine at dusk"

    # Running it again adds nothing twice.
    again = takeout.recreate(conn, [str(dest)], [str(photos)])
    assert again["albums"][0]["id"] == album["id"]
    assert conn.execute("SELECT COUNT(*) FROM album_items").fetchone()[0] == 1


def test_the_console_offers_the_albums_and_makes_them(tmp_path):
    """The Import page, end to end: import the export, add the archive to the
    library, let the index catch up, then one call makes the albums."""
    import time

    from conftest import ADMIN, login
    from test_archive_console import run_and_wait
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth
    from ninaivu.server.config import Config

    photos = takeout_export(tmp_path)
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(tmp_path / "empty")]
    cfg.active_root = str(tmp_path / "empty")
    cfg.watch = False
    (tmp_path / "empty").mkdir()
    cfg.ensure_dirs()
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    archive.configure(tmp_path / "archive-state")
    adb.close_db()
    adb.init_db()
    services = build_services(cfg)
    services.scanner.stop()
    client = login(create_admin_app(services).test_client(), *ADMIN)
    try:
        dest = tmp_path / "library"
        run_and_wait(client, [photos], dest)
        offered = client.get("/api/archive/takeout-albums").get_json()["albums"]
        assert offered == [{"title": "Paris 2019", "files": 1}]

        assert client.post("/api/archive/adopt", json={}).status_code == 200
        deadline = time.time() + 30
        while time.time() < deadline and (services.scanner.running or services.scanner.deferred is not None):
            time.sleep(0.2)
        services.scanner.stop(join=True)

        made = client.post("/api/archive/takeout-albums", json={}).get_json()
        assert [(a["title"], a["added"]) for a in made["albums"]] == [("Paris 2019", 1)]
        assert made["unmatched"] == 0
    finally:
        services.scanner.stop(join=True)
        adb.close_db()
