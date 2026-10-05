"""Bringing in a Google Photos or iCloud export.

What an export says beside its photographs — when, where, favourite, albums,
deleted — is the reason to import it rather than copy it, so each of those is
checked; and so are the two promises: nothing already here is copied again,
and the export itself is only read.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.media.scanner import Scanner
from ninaivu.server import auth
from ninaivu.storage import importer as importer_mod
from ninaivu.storage.importer import Importer, _google_sidecar, _icloud_date

JULY_4_2019 = int(datetime(2019, 7, 4, 10, 0, tzinfo=timezone.utc).timestamp())


def _jpeg(colour) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (120, 80), colour).save(out, "JPEG")
    return out.getvalue()


def _sidecar(title, when, **more) -> bytes:
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": str(when)}, **more}).encode()


@pytest.fixture()
def house(scanned, tmp_path: Path):
    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    library = Path(cfg.active_root)
    downloads = tmp_path / "Downloads" / "exports"
    downloads.mkdir(parents=True)

    goa = _jpeg((10, 120, 200))
    with zipfile.ZipFile(downloads / "takeout-20260930-001.zip", "w") as z:
        base = "Takeout/Google Photos"
        z.writestr(f"{base}/Photos from 2019/IMG_0101.jpg", goa)
        z.writestr(f"{base}/Photos from 2019/IMG_0101.jpg.supplemental-metadata.json",
                   _sidecar("IMG_0101.jpg", JULY_4_2019, description="Baga beach",
                            favorited=True, geoData={"latitude": 15.55, "longitude": 73.75}))
        # The same photograph again, in its album, as Takeout always does.
        z.writestr(f"{base}/Goa trip/IMG_0101.jpg", goa)
        z.writestr(f"{base}/Goa trip/IMG_0101.jpg.supplemental-metadata.json",
                   _sidecar("IMG_0101.jpg", JULY_4_2019))
        z.writestr(f"{base}/Goa trip/metadata.json", json.dumps({"title": "Goa trip 2019"}))
        # A photograph whose sidecar is in the other zip.
        z.writestr(f"{base}/Photos from 2018/saved-from-chat.jpg", _jpeg((200, 30, 30)))
        # Already in the library, byte for byte.
        z.writestr(f"{base}/Photos from 2023/shot1.jpg",
                   (library / "2023/06/11/shot1.jpg").read_bytes())
    with zipfile.ZipFile(downloads / "takeout-20260930-002.zip", "w") as z:
        z.writestr("Takeout/Google Photos/Photos from 2018/saved-from-chat.jpg.json",
                   _sidecar("saved-from-chat.jpg", JULY_4_2019 - 365 * 86400))
    with zipfile.ZipFile(downloads / "iCloud Photos Part 1 of 1.zip", "w") as z:
        part = "iCloud Photos Part 1 of 1"
        z.writestr(f"{part}/Photos/IMG_0002.JPG", _jpeg((30, 200, 30)))
        z.writestr(f"{part}/Photos/IMG_0003.JPG", _jpeg((90, 90, 90)))
        z.writestr(f"{part}/Photos/IMG_0004.JPG", _jpeg((250, 250, 10)))
        z.writestr(f"{part}/Photos/Photo Details.csv",
                   "imgName,fileChecksum,favorite,hidden,deleted,originalCreationDate,viewCount,importDate\n"
                   "IMG_0002.JPG,x,yes,no,no,\"Tuesday December 17,2019 5:31 PM GMT\",1,\n"
                   "IMG_0003.JPG,x,no,no,yes,\"Tuesday December 17,2019 5:32 PM GMT\",1,\n"
                   "IMG_0004.JPG,x,no,yes,no,\"Monday January 6,2020 9:00 AM GMT\",1,\n")
        z.writestr(f"{part}/Albums/Family.csv", "Images\nIMG_0002.JPG\n")
    before = {p.name: p.stat().st_mtime_ns for p in downloads.iterdir()}
    importer = Importer(cfg, lambda: __import__("ninaivu.storage.db", fromlist=["db"]).connect(cfg.db_path))
    return {"cfg": cfg, "conn": conn, "services": services, "library": library,
            "downloads": downloads, "before": before, "importer": importer, "admin_id": admin.id}


def _run(house):
    importer = house["importer"]
    assert importer.start(str(house["downloads"]), house["cfg"].active_root,
                          house["admin_id"]) == {"started": True}
    importer.join(60)
    Scanner(house["cfg"])._run(Path(house["cfg"].active_root), full=False)
    importer.apply_hints()
    return importer.status()


def _asset(conn, name):
    return conn.execute("SELECT * FROM assets WHERE filename=?", (name,)).fetchone()


def test_takeout_sidecar_names_are_matched_the_way_google_writes_them():
    names = {n.lower(): n for n in (
        "IMG_1.jpg.json", "IMG_2.jpg.supplemental-metadata.json",
        "IMG_3.jpg(1).json", "a-very-long-name-that-google-cuts-short-2019.jpg.supplemental-meta.json")}
    assert _google_sidecar(names, "IMG_1.jpg") == "IMG_1.jpg.json"
    assert _google_sidecar(names, "IMG_2.jpg") == "IMG_2.jpg.supplemental-metadata.json"
    assert _google_sidecar(names, "IMG_3(1).jpg") == "IMG_3.jpg(1).json"
    assert _google_sidecar(names, "IMG_1-edited.jpg") == "IMG_1.jpg.json"
    assert _google_sidecar(names, "a-very-long-name-that-google-cuts-short-2019.jpg") \
        == "a-very-long-name-that-google-cuts-short-2019.jpg.supplemental-meta.json"
    assert _google_sidecar(names, "nothing.jpg") is None


def test_icloud_dates_are_read():
    assert _icloud_date("Tuesday December 17,2019 5:31 PM GMT") == datetime(
        2019, 12, 17, 17, 31, tzinfo=timezone.utc).timestamp()
    assert _icloud_date("not a date") is None


def test_looking_copies_nothing_and_says_what_is_there(house):
    found = importer_mod.look(house["downloads"])
    assert found["media"] == 7 and found["google"] and found["icloud"]
    assert not list(house["library"].rglob("IMG_0101*"))


def test_an_export_is_filed_by_its_own_dates_and_nothing_twice(house):
    status = _run(house)
    assert status["copied"] == 4 and status["duplicates"] == 2 and status["skipped"] == 1
    assert status["failed"] == 0
    goa = list(house["library"].rglob("IMG_0101.jpg"))
    assert len(goa) == 1, "the album copy and the year copy arrive once"
    assert goa[0].parent.relative_to(house["library"]).as_posix() == time.strftime(
        "%Y/%m/%d", time.localtime(JULY_4_2019))
    chat = list(house["library"].rglob("saved-from-chat.jpg"))
    assert chat and chat[0].parent.parts[-3] == "2018", "its sidecar was in the other zip"
    assert not list(house["library"].rglob("IMG_0003*")), "deleted in iCloud stays deleted"
    assert len(list(house["library"].rglob("shot1*.jpg"))) == 1, "already here: not copied"
    assert not list(house["library"].rglob(importer_mod.STAGING))


def test_what_the_export_said_is_applied_once_indexed(house):
    _run(house)
    conn = house["conn"]
    goa = _asset(conn, "IMG_0101.jpg")
    assert (goa["gps_lat"], goa["gps_lon"]) == (15.55, 73.75)
    assert goa["caption"] == "Baga beach"
    assert goa["caption_source"] == "manual", "typed by a person: no automatic caption replaces it"
    favourite = conn.execute("SELECT favorite FROM user_assets WHERE asset_id=? AND user_id=?",
                             (goa["id"], house["admin_id"])).fetchone()
    assert favourite and favourite["favorite"] == 1
    albums = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM albums")}
    assert {"Goa trip 2019", "Family"} <= set(albums)
    in_goa = {r[0] for r in conn.execute("SELECT asset_id FROM album_items WHERE album_id=?",
                                         (albums["Goa trip 2019"],))}
    assert goa["id"] in in_goa
    family = _asset(conn, "IMG_0002.JPG")
    assert family["id"] in {r[0] for r in conn.execute(
        "SELECT asset_id FROM album_items WHERE album_id=?", (albums["Family"],))}
    assert _asset(conn, "IMG_0004.JPG")["visibility"] == 2, "hidden in iCloud, hidden here"
    assert family["date_key"] == "2019-12-17"


def test_the_export_is_only_read(house):
    _run(house)
    assert {p.name: p.stat().st_mtime_ns for p in house["downloads"].iterdir()} == house["before"]


def test_a_second_run_copies_nothing_new(house):
    _run(house)
    files = sorted(str(p) for p in house["library"].rglob("*") if p.is_file())
    status = _run(house)
    assert status["copied"] == 0
    assert sorted(str(p) for p in house["library"].rglob("*") if p.is_file()) == files


def test_a_name_already_taken_is_never_written_over(house):
    day = time.strftime("%Y/%m/%d", time.localtime(JULY_4_2019))
    (house["library"] / day).mkdir(parents=True, exist_ok=True)
    stranger = house["library"] / day / "IMG_0101.jpg"
    stranger.write_bytes(b"somebody else's photograph")
    _run(house)
    assert stranger.read_bytes() == b"somebody else's photograph"
    assert (house["library"] / day / "IMG_0101 (2).jpg").is_file()


def test_the_console_runs_it_and_refuses_the_library_itself(house):
    client = login(create_admin_app(house["services"]).test_client(), *ADMIN)
    seen = client.post("/api/import/look", json={"folder": str(house["downloads"])}).get_json()
    assert seen["media"] == 7
    refused = client.post("/api/import/look", json={"folder": house["cfg"].active_root})
    assert refused.status_code == 400 and "already uses" in refused.get_json()["error"]
    assert client.post("/api/import/look", json={"folder": "relative"}).status_code == 400
    started = client.post("/api/import/start", json={"folder": str(house["downloads"])})
    assert started.status_code == 200, started.get_json()
    house["services"].importer.join(60)
    assert client.get("/api/import/status").get_json()["copied"] == 4
    home = login(create_home_app(house["services"]).test_client(), *ADMIN)
    assert home.post("/api/import/look", json={"folder": str(house["downloads"])}).status_code == 404


def test_whatsapp_chats_become_albums_and_a_forward_arrives_once(house):
    downloads = house["downloads"]
    for path in list(downloads.iterdir()):
        path.unlink()
    picture = _jpeg((7, 77, 177))
    with zipfile.ZipFile(downloads / "WhatsApp Chat with Amma.zip", "w") as z:
        z.writestr("_chat.txt", "[04/07/2019, 10:15:22] Amma: <attached: 00000012-PHOTO-2019-07-04-10-15-22.jpg>\n")
        z.writestr("00000012-PHOTO-2019-07-04-10-15-22.jpg", picture)
    chats = downloads / "Family group"
    chats.mkdir()
    (chats / "WhatsApp Chat with Family group.txt").write_text("04/07/2019, 10:20 - Ravi: IMG-20190704-WA0012.jpg (file attached)\n")
    (chats / "IMG-20190704-WA0012.jpg").write_bytes(picture)          # the same, forwarded
    status = _run(house)
    assert status["copied"] == 1 and status["duplicates"] == 1
    placed = list(house["library"].rglob("*PHOTO-2019-07-04*.jpg"))
    assert len(placed) == 1 and "2019/07/04" in placed[0].as_posix()
    conn = house["conn"]
    albums = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM albums")}
    asset = conn.execute("SELECT id FROM assets WHERE filename=?", (placed[0].name,)).fetchone()["id"]
    for name in ("WhatsApp: Amma", "WhatsApp: Family group"):
        assert asset in {r[0] for r in conn.execute(
            "SELECT asset_id FROM album_items WHERE album_id=?", (albums[name],))}, name
