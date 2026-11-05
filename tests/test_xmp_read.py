"""Reading back the sidecars Ninaivu wrote, for photographs new to this index.

What a household told one Ninaivu about a photograph (who is in it, its
albums and stars, a description, a corrected date, the place) travels with
the photograph to another, and fills in only what nobody here has said.
"""

import json
import time
from pathlib import Path

from PIL import Image

from ninaivu.media.scanner import Scanner
from ninaivu.storage import db
from ninaivu.storage.xmp import render
from ninaivu.storage.xmp_read import XmpReader, parse

FACTS = {
    "people": [{"name": "Amma", "box": [100.0, 50.0, 80.0, 100.0]},
               {"name": "Thatha", "box": [300.0, 60.0, 70.0, 90.0]}],
    "albums": ["Pongal 2026", "Family"], "favorite": True, "rating": 4,
    "caption": "Pongal at the old house", "created": "1998-01-14T09:30:00",
    "city": "Madurai", "country": "India", "lat": 9.9252, "lon": 78.1198,
    "width": 640, "height": 480,
}


def test_what_ninaivu_writes_it_reads_back():
    info = parse(render(FACTS))
    assert [p["name"] for p in info["people"]] == ["Amma", "Thatha"]
    cx, cy, w, h = info["people"][0]["area"]
    assert (round(cx * 640), round(cy * 480), round(w * 640), round(h * 480)) == (140, 100, 80, 100)
    assert info["albums"] == ["Family", "Pongal 2026"]
    assert info["favorite"] is True and info["rating"] == 4
    assert info["caption"] == "Pongal at the old house"
    assert info["created"] == "1998-01-14T09:30:00"
    assert (info["city"], info["country"]) == ("Madurai", "India")
    assert abs(info["lat"] - 9.9252) < 1e-5 and abs(info["lon"] - 78.1198) < 1e-5


def test_only_ninaivus_own_sidecars_are_read():
    other = render(FACTS).replace('ninaivu:Writer="Ninaivu"', 'ninaivu:Writer="Lightroom"')
    assert parse(other) is None
    assert parse("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>") is None
    assert parse('<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>') is None
    assert parse("not xml at all") is None


def _new_photo(cfg, conn, name="arrived.jpg", sidecar=FACTS):
    root = Path(cfg.active_root)
    folder = root / "2026" / "11"
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 480), (120, 30, 60)).save(folder / name, "JPEG")
    if sidecar is not None:
        (folder / f"{name}.xmp").write_text(render(sidecar), encoding="utf-8")
    Scanner(cfg)._run(root, full=True)
    return conn.execute("SELECT * FROM assets WHERE rel_path = ?", (f"2026/11/{name}",)).fetchone()


def _reader(cfg):
    reader = XmpReader(cfg, lambda: db.connect(cfg.db_path))
    reader.baseline()
    return reader


def test_a_new_photograph_gets_what_its_sidecar_says(scanned):
    cfg, conn, _ = scanned
    from ninaivu.server import auth
    auth.bootstrap_admin(conn, "dad", "a long enough password", "Dad")
    reader = _reader(cfg)
    asset = _new_photo(cfg, conn)
    assert reader.run()["read"] == 1
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset["id"],)).fetchone()
    assert row["caption"] == "Pongal at the old house" and row["caption_source"] == "manual"
    assert row["date_source"] == "manual" and row["date_key"] == "1998-01-14"
    assert row["city"] == "Madurai" and abs(row["gps_lat"] - 9.9252) < 1e-5
    albums = {r["name"] for r in conn.execute(
        "SELECT al.name FROM album_items ai JOIN albums al ON al.id = ai.album_id "
        "WHERE ai.asset_id = ?", (asset["id"],))}
    assert albums == {"Family", "Pongal 2026"}
    stars = conn.execute("SELECT favorite, rating FROM user_assets WHERE asset_id = ?",
                         (asset["id"],)).fetchone()
    assert (stars["favorite"], stars["rating"]) == (1, 4)
    # The names wait for the faces.
    assert conn.execute("SELECT 1 FROM xmp_people_waiting WHERE asset_id = ?",
                        (asset["id"],)).fetchone()


def test_photographs_already_here_are_not_read(scanned):
    cfg, conn, _ = scanned
    first = conn.execute("SELECT root, rel_path FROM assets ORDER BY id LIMIT 1").fetchone()
    (Path(first["root"]) / f"{first['rel_path']}.xmp").write_text(render(FACTS), encoding="utf-8")
    reader = _reader(cfg)
    assert reader.run()["read"] == 0


def test_what_a_person_here_said_is_never_overwritten(scanned):
    cfg, conn, _ = scanned
    from ninaivu.server import auth
    auth.bootstrap_admin(conn, "dad", "a long enough password", "Dad")
    reader = _reader(cfg)
    asset = _new_photo(cfg, conn, sidecar=None)
    conn.execute("UPDATE assets SET caption = 'Ours', caption_source = 'manual', "
                 "date_source = 'manual', date_key = '2001-02-03', city = 'Chennai' WHERE id = ?",
                 (asset["id"],))
    admin = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO user_assets (user_id, asset_id, favorite, rating) VALUES (?, ?, 0, 2)",
                 (admin, asset["id"]))
    conn.commit()
    reader.apply(conn, int(asset["id"]), parse(render(FACTS)))
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset["id"],)).fetchone()
    assert (row["caption"], row["date_key"], row["city"]) == ("Ours", "2001-02-03", "Chennai")
    assert conn.execute("SELECT rating FROM user_assets WHERE asset_id = ?",
                        (asset["id"],)).fetchone()["rating"] == 2


def _face(conn, asset_id, box, person=None, source="none"):
    cur = conn.execute("INSERT INTO faces (asset_id, person_id, source, bbox, embedding) "
                       "VALUES (?, ?, ?, ?, ?)", (asset_id, person, source, json.dumps(box), b"\0" * 16))
    conn.commit()
    return int(cur.lastrowid)


def test_names_go_to_the_faces_where_the_sidecar_says(scanned):
    cfg, conn, _ = scanned
    reader = _reader(cfg)
    asset = _new_photo(cfg, conn)
    reader.run()
    amma = _face(conn, asset["id"], [102.0, 48.0, 79.0, 103.0])
    nobody = _face(conn, asset["id"], [500.0, 300.0, 40.0, 40.0])
    assert reader.run()["named"] == 1           # Thatha's face was not found here
    face = conn.execute("SELECT f.source, p.name FROM faces f JOIN people_clusters p "
                        "ON p.id = f.person_id WHERE f.id = ?", (amma,)).fetchone()
    assert (face["source"], face["name"]) == ("confirmed", "Amma")
    assert conn.execute("SELECT person_id FROM faces WHERE id = ?", (nobody,)).fetchone()[0] is None
    assert not conn.execute("SELECT 1 FROM xmp_people_waiting").fetchone()


def test_a_face_somebody_here_confirmed_is_not_renamed(scanned):
    cfg, conn, _ = scanned
    reader = _reader(cfg)
    asset = _new_photo(cfg, conn)
    reader.run()
    mine = db.create_or_update_person_cluster(conn, "Paati")
    face = _face(conn, asset["id"], [100.0, 50.0, 80.0, 100.0], person=mine["id"], source="confirmed")
    reader.run()
    assert conn.execute("SELECT person_id FROM faces WHERE id = ?", (face,)).fetchone()[0] == mine["id"]


def test_names_wait_for_faces_and_are_let_go_in_the_end(scanned, monkeypatch):
    cfg, conn, _ = scanned
    reader = _reader(cfg)
    asset = _new_photo(cfg, conn)
    reader._face_version = lambda: 999                 # its faces not looked for yet
    reader.run()
    assert conn.execute("SELECT 1 FROM xmp_people_waiting WHERE asset_id = ?", (asset["id"],)).fetchone()
    conn.execute("UPDATE xmp_people_waiting SET since = ?", (time.time() - 40 * 86400,))
    conn.commit()
    reader.run()
    assert not conn.execute("SELECT 1 FROM xmp_people_waiting").fetchone()


def test_the_reader_is_woken_by_a_scan_that_added_photographs(app):
    services = app.config["MV_SERVICES"]
    woken = []
    services.xmp_reader.wake = lambda: woken.append(1)
    services._scan_found({"phase": "done", "added": 2})
    services._scan_found({"phase": "done", "added": 0, "updated": 3})
    assert woken == [1]


def test_building_the_services_starts_no_timer(scanned):
    """Only starting them does: services built and never started (as much of
    the suite does) must leave nothing running that would keep them alive."""
    import threading
    from ninaivu import build_services
    cfg, _, _ = scanned
    before = {t.ident for t in threading.enumerate()}
    services = build_services(cfg)
    started = [t.name for t in threading.enumerate() if t.ident not in before]
    assert not any("xmp-read" in name for name in started), started
    services.xmp_reader.schedule()
    assert any(t.name == "ninaivu-xmp-read-timer" for t in threading.enumerate())
    services.xmp_reader.stop(join=True)
