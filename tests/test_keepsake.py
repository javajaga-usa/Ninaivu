"""The family archive: the library on a drive, readable with nothing but a browser."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.server import auth
from ninaivu.storage import db
from ninaivu.storage.keepsake import FOLDER, Keepsake


@pytest.fixture()
def lib(scanned, tmp_path):
    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET visibility=2 WHERE rel_path LIKE 'private/%'")
    person = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES('Maya', 0)").lastrowid
    shot = conn.execute("SELECT id FROM assets WHERE kind='picture' AND visibility < 2 "
                        "ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                 "VALUES(?, ?, 'confirmed', '[0,0,9,9]', x'00')", (shot, person))
    album = conn.execute("INSERT INTO albums(name, created_at) VALUES('Holiday', 0)").lastrowid
    conn.execute("INSERT INTO album_items(album_id, asset_id, added_at) VALUES(?, ?, 0)", (album, shot))
    conn.execute("UPDATE assets SET city='Ooty' WHERE id=?", (shot,))
    conn.commit()
    drive = tmp_path / "USB"
    drive.mkdir()
    keeper = Keepsake(cfg, lambda: db.connect(cfg.db_path))
    return {"cfg": cfg, "conn": conn, "keeper": keeper, "drive": drive, "shot": shot}


def make(keeper, drive, **kw):
    keeper.start(str(drive), **kw)
    keeper.join(60)
    return keeper.status()


def data_of(base: Path) -> dict:
    text = (base / "data.js").read_text(encoding="utf-8")
    return json.loads(re.match(r"window\.NINAIVU_ARCHIVE = (.*);\s*$", text, re.S).group(1))


def test_the_archive_opens_with_nothing_but_a_browser(lib):
    status = make(lib["keeper"], lib["drive"], letter="Ask Aunt Priya.\n\n<b>Love</b>, the family")
    assert not status["error"], status
    assert status["last_made"] > 0 and status["last_folder"] == str(lib["drive"])
    base = lib["drive"] / FOLDER
    assert (base / "Open me.html").is_file() and (base / "Read me first.html").is_file()
    data = data_of(base)
    visible = lib["conn"].execute(
        "SELECT COUNT(*) FROM assets WHERE trashed=0 AND nsfw=0 AND live_clip=0 AND visibility<=1 "
        "AND kind IN ('picture', 'video')").fetchone()[0]
    assert visible and len(data["items"]) == visible, "hidden photographs stay out unless asked for"
    assert not any("secret" in i["f"] for i in data["items"])
    shot = next(i for i in data["items"] if i["id"] == lib["shot"])
    assert data["people"][shot["p"][0]] == "Maya" and data["albums"][shot["a"][0]] == "Holiday"
    assert shot["place"] == "Ooty"
    rel = lib["conn"].execute("SELECT root, rel_path FROM assets WHERE id=?", (lib["shot"],)).fetchone()
    original = Path(rel["root"], rel["rel_path"]).read_bytes()
    assert (base / shot["f"]).read_bytes() == original, "the original, as it is"
    with Image.open(base / shot["t"]) as thumb:
        assert thumb.format == "JPEG"
    letter = (base / "Read me first.html").read_text(encoding="utf-8")
    assert "Ask Aunt Priya." in letter and "&lt;b&gt;Love&lt;/b&gt;" in letter, "the letter, escaped"
    assert not list(base.rglob("*.part")), "nothing half-written is left behind"


def test_made_again_only_what_is_new_or_changed_is_copied(lib):
    make(lib["keeper"], lib["drive"])
    again = make(lib["keeper"], lib["drive"])
    assert again["copied"] == 0
    rel = lib["conn"].execute("SELECT root, rel_path, mtime FROM assets WHERE id=?", (lib["shot"],)).fetchone()
    source = Path(rel["root"], rel["rel_path"])
    Image.new("RGB", (64, 64), (0, 0, 255)).save(source, "JPEG")
    stamp = float(rel["mtime"]) + 100
    os.utime(source, (stamp, stamp))
    lib["conn"].execute("UPDATE assets SET mtime=?, size=? WHERE id=?",
                        (stamp, source.stat().st_size, lib["shot"]))
    lib["conn"].commit()
    third = make(lib["keeper"], lib["drive"])
    assert third["copied"] == 1, "a changed photograph goes again"


def test_hidden_ones_when_asked_and_smaller_copies_for_a_small_drive(lib):
    status = make(lib["keeper"], lib["drive"], everything=True, small=True)
    assert not status["error"], status
    base = lib["drive"] / FOLDER
    data = data_of(base)
    assert any("secret" in i["f"] for i in data["items"])
    pictures = [i for i in data["items"] if i["k"] == "p"]
    assert pictures and all(i["f"].lower().endswith((".jpg", ".jpeg")) for i in pictures)
    assert status["copied"] == len(data["items"])
    again = make(lib["keeper"], lib["drive"], everything=True, small=True)
    assert again["copied"] == 0, "smaller copies are not made twice"


def test_smaller_copies_of_a_png_and_a_jpg_of_one_name_stay_apart(lib):
    item = {"root": "/r", "rel_path": "2020/a.png", "kind": "picture"}
    other = {"root": "/r", "rel_path": "2020/a.jpg", "kind": "picture"}
    base = lib["drive"]
    assert Keepsake._target(base, item, True, False) != Keepsake._target(base, other, True, False)


def test_a_stopped_run_keeps_the_page_of_the_last_whole_one(lib):
    make(lib["keeper"], lib["drive"])
    base = lib["drive"] / FOLDER
    before = (base / "data.js").read_text(encoding="utf-8")
    keeper = lib["keeper"]
    keeper._stop.set()
    original_clear = keeper._stop.clear
    keeper._stop.clear = lambda: None                      # stopped before the first file
    try:
        status = make(keeper, lib["drive"])
    finally:
        keeper._stop.clear = original_clear
    assert "Stopped" in status["message"]
    assert (base / "data.js").read_text(encoding="utf-8") == before


def test_not_in_the_library_and_not_on_a_missing_drive(lib, tmp_path):
    with pytest.raises(ValueError, match="another drive"):
        lib["keeper"].start(str(Path(lib["cfg"].active_root) / "misc"))
    with pytest.raises(ValueError, match="plugged in"):
        lib["keeper"].start(str(tmp_path / "not-there"))
    with pytest.raises(ValueError, match="Choose"):
        lib["keeper"].start("  ")


def test_a_drive_too_small_says_so(lib, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(1, 1, 1))
    status = make(lib["keeper"], lib["drive"])
    assert "GB free" in status["error"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_script_is_valid(lib, tmp_path):
    make(lib["keeper"], lib["drive"])
    page = (lib["drive"] / FOLDER / "Open me.html").read_text(encoding="utf-8")
    script = page.split("<script>")[1].split("</script>")[0]
    target = tmp_path / "viewer.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# -- the console's routes, and the handover sheet --------------------------------


@pytest.fixture()
def house(scanned, tmp_path):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    drive = tmp_path / "USB"
    drive.mkdir()
    client = login(create_admin_app(services).test_client(), *ADMIN)
    return {"cfg": cfg, "conn": conn, "services": services, "client": client, "drive": drive}


def test_the_console_makes_it_and_the_handover_sheet_names_it(house):
    client = house["client"]
    assert client.get("/api/admin/keepsake").get_json()["last_made"] == 0
    saved = client.post("/api/admin/keepsake", json={"letter": "Ask Raman."}).get_json()
    assert saved["letter"] == "Ask Raman." and not saved["running"]
    for bad in ({"letter": 5}, {"letter": "x" * 20001}, {"folder": 3},
                {"folder": str(house["drive"]), "small": "yes"}):
        assert client.post("/api/admin/keepsake", json=bad).status_code == 400, bad
    inside = client.post("/api/admin/keepsake", json={"folder": house["cfg"].active_root})
    assert inside.status_code == 409
    started = client.post("/api/admin/keepsake", json={"folder": str(house["drive"])})
    assert started.status_code == 200
    house["services"].keepsake.join(60)
    status = client.get("/api/admin/keepsake").get_json()
    assert not status["error"] and status["last_made"] > 0
    assert (house["drive"] / FOLDER / "Open me.html").is_file()
    page = client.get("/api/admin/handover").get_json()
    kinds = {d["kind"]: d for d in page["facts"]["destinations"]}
    assert kinds["keepsake"]["where"] == str(house["drive"])
    assert any("Open me.html" in step for step in page["facts"]["restore_steps"])
    actions = [r["action"] for r in house["conn"].execute("SELECT action FROM audit")]
    assert "keepsake" in actions


def test_only_a_signed_in_administrator_may_make_it(house):
    stranger = create_admin_app(house["services"]).test_client()
    assert stranger.get("/api/admin/keepsake").status_code in (401, 403)
    assert stranger.post("/api/admin/keepsake",
                         json={"folder": str(house["drive"])}).status_code in (401, 403)
    assert not house["services"].keepsake.running
