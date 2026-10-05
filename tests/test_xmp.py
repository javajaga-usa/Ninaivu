"""XMP sidecars: the household's work, beside the photographs, for other programs."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ninaivu.storage import db
from ninaivu.storage.xmp import XmpWriter, render, sidecar_for

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "stArea": "http://ns.adobe.com/xmp/sType/Area#",
}


def parse(text: str) -> ET.Element:
    body = text.split("?>", 1)[1].rsplit("<?xpacket", 1)[0]
    return ET.fromstring(body).find("rdf:RDF/rdf:Description", NS)


def bag(desc: ET.Element, tag: str) -> list[str]:
    return [li.text for li in desc.findall(f"{tag}/rdf:Bag/rdf:li", NS)]


def test_a_photograph_with_nothing_to_say_gets_no_sidecar():
    assert render({"people": [], "albums": []}) == ""


def test_every_kind_of_fact_is_written_where_other_programs_read_it():
    text = render({
        "people": [{"name": "Maya & Arjun's <gran>", "box": [100, 50, 200, 200]},
                   {"name": "Arjun", "box": None}],
        "albums": ["Holiday 2019"], "favorite": True, "rating": 4,
        "caption": "Cake at the beach", "created": "2019-07-08T10:30:00",
        "city": "Ooty", "country": "India", "lat": 11.4102, "lon": -76.695,
        "width": 1000, "height": 800,
    })
    desc = parse(text)
    attr = desc.attrib
    assert attr["{https://github.com/javajaga-usa/Ninaivu/ns/1.0/}Writer"] == "Ninaivu"
    assert attr[f"{{{NS['xmp']}}}Rating"] == "4"
    assert attr[f"{{{NS['photoshop']}}}DateCreated"] == "2019-07-08T10:30:00"
    assert attr[f"{{{NS['photoshop']}}}City"] == "Ooty"
    assert attr[f"{{{NS['exif']}}}GPSLatitude"].endswith("N")
    assert attr[f"{{{NS['exif']}}}GPSLongitude"].startswith("76,41.") and \
        attr[f"{{{NS['exif']}}}GPSLongitude"].endswith("W")
    names = ["Arjun", "Maya & Arjun's <gran>"]
    assert bag(desc, "Iptc4xmpExt:PersonInImage") == names
    assert "Albums|Holiday 2019" in bag(desc, "lr:hierarchicalSubject")
    assert "Favourites" in bag(desc, "dc:subject")
    assert desc.find("dc:description/rdf:Alt/rdf:li", NS).text == "Cake at the beach"
    regions = desc.findall("mwg-rs:Regions/mwg-rs:RegionList/rdf:Bag/rdf:li/rdf:Description", NS)
    assert len(regions) == 1, "only a face with a box has a region"
    area = regions[0].find("mwg-rs:Area", NS).attrib
    assert area[f"{{{NS['stArea']}}}x"] == "0.20000" and area[f"{{{NS['stArea']}}}w"] == "0.20000"


@pytest.fixture()
def lib(scanned):
    cfg, conn, _ = scanned
    cfg.xmp_sidecars = True
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}
    person = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES('Maya', 0)").lastrowid
    conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                 "VALUES(?, ?, 'confirmed', '[10,10,50,50]', x'00')", (ids["shot1.jpg"], person))
    album = conn.execute("INSERT INTO albums(name, created_at) VALUES('Best', 0)").lastrowid
    conn.execute("INSERT INTO album_items(album_id, asset_id, added_at) VALUES(?, ?, 0)",
                 (album, ids["shot2.jpg"]))
    # What the tagger made is not the family's work.
    conn.execute("UPDATE assets SET tags='[\"beach\", \"sea\", \"sky\"]', caption='beach, sea, sky'")
    conn.commit()
    writer = XmpWriter(cfg, lambda: db.connect(cfg.db_path))
    root = Path(cfg.active_root)
    path = {name: root / conn.execute("SELECT rel_path FROM assets WHERE id=?", (aid,)).fetchone()[0]
            for name, aid in ids.items()}
    return {"cfg": cfg, "conn": conn, "writer": writer, "ids": ids, "path": path, "person": person}


def run(writer, **kw):
    writer.start(**kw)
    writer.join(30)
    return writer.status()


def test_sidecars_are_written_only_where_there_is_something_to_say(lib):
    before = {p: p.read_bytes() for p in lib["path"].values()}
    status = run(lib["writer"])
    assert status["written"] == 2 and status["kept"] == 2
    one = sidecar_for(lib["path"]["shot1.jpg"]).read_text()
    assert "Maya" in one and "beach" not in one, "no AI tags or AI captions"
    assert "Albums|Best" in sidecar_for(lib["path"]["shot2.jpg"]).read_text()
    assert not sidecar_for(lib["path"]["shot3.jpg"]).exists()
    assert all(p.read_bytes() == data for p, data in before.items()), "photographs untouched"
    again = run(lib["writer"])
    assert again["written"] == 0 and again["unchanged"] == 2


def test_a_change_is_written_and_a_sidecar_with_nothing_left_is_removed(lib):
    run(lib["writer"])
    conn = lib["conn"]
    conn.execute("UPDATE people_clusters SET name='Maya Raj' WHERE id=?", (lib["person"],))
    conn.execute("DELETE FROM album_items")
    conn.commit()
    status = run(lib["writer"])
    assert status["written"] == 1 and status["removed"] == 1
    assert "Maya Raj" in sidecar_for(lib["path"]["shot1.jpg"]).read_text()
    assert not sidecar_for(lib["path"]["shot2.jpg"]).exists()


def test_a_sidecar_another_program_wrote_is_never_touched(lib):
    theirs = sidecar_for(lib["path"]["shot1.jpg"])
    theirs.write_text("<x:xmpmeta>Lightroom's</x:xmpmeta>")
    status = run(lib["writer"])
    assert status["left_alone"] == 1
    assert theirs.read_text() == "<x:xmpmeta>Lightroom's</x:xmpmeta>"


def test_a_corrected_date_is_written_but_the_cameras_own_is_not(lib):
    conn = lib["conn"]
    conn.execute("UPDATE assets SET date_source='manual' WHERE id=?", (lib["ids"]["shot3.jpg"],))
    conn.commit()
    run(lib["writer"])
    assert "photoshop:DateCreated" in sidecar_for(lib["path"]["shot3.jpg"]).read_text()
    assert "DateCreated" not in sidecar_for(lib["path"]["shot1.jpg"]).read_text()


def test_the_console_turns_it_on_and_writes(scanned):
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        client = login(create_admin_app(services).test_client(), *ADMIN)
        assert client.get("/api/admin/xmp").get_json()["enabled"] is False
        assert client.post("/api/admin/xmp", json={"write": True}).status_code == 409
        data = client.post("/api/admin/xmp", json={"enabled": True}).get_json()
        assert data["enabled"] is True and cfg.xmp_sidecars is True
        services.xmp.join(30)
        assert client.post("/api/admin/xmp", json={"enabled": "yes"}).status_code == 400
    finally:
        services.stop(timeout=5.0)


def test_it_waits_while_the_household_is_using_ninaivu(lib):
    asked = []

    def hold():
        asked.append(1)
        return "someone is watching a video" if len(asked) < 2 else None

    writer = XmpWriter(lib["cfg"], lambda: db.connect(lib["cfg"].db_path), hold=hold)
    writer._stop.wait = lambda seconds: False            # no real waiting in a test
    status = run(writer)
    assert len(asked) >= 2 and status["written"] == 2


def test_the_index_is_free_for_others_while_it_waits(lib, monkeypatch):
    """The pass held one write transaction from its first sidecar to its last,
    through every pause for the household: every other writer waited the whole
    of busy_timeout behind it and then failed with "database is locked"."""
    import sqlite3
    from ninaivu.storage import xmp

    monkeypatch.setattr(xmp, "BATCH", 1)                 # a pause after every sidecar
    outcomes = []

    def hold():
        # Not status(): on this thread it would reach the writer's connection
        # and, through executescript, commit for it.
        if writer._state["written"] == 0:                # noqa: SLF001
            return None                                  # nothing written yet: not the case
        # Not db.connect(): that hands this thread the writer's own connection.
        other = sqlite3.connect(lib["cfg"].db_path, timeout=0.5)
        try:
            other.execute("INSERT INTO meta(key, value) VALUES('xmp-test', 'x') "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            other.commit()
            outcomes.append("written")
        except sqlite3.OperationalError as exc:
            outcomes.append(str(exc))
        finally:
            other.close()
        return None

    writer = XmpWriter(lib["cfg"], lambda: db.connect(lib["cfg"].db_path), hold=hold)
    status = run(writer)
    assert status["written"] == 2 and outcomes and set(outcomes) == {"written"}, outcomes


def test_only_a_caption_somebody_wrote_goes_in(lib):
    # Hearth guessed: a caption without commas passed for one a person typed,
    # so a generated sentence went into the sidecar as the family's words.
    conn, shot3 = lib["conn"], lib["ids"]["shot3.jpg"]
    conn.execute("UPDATE assets SET caption='A girl standing on a beach', caption_source='auto' "
                 "WHERE id=?", (shot3,))
    conn.commit()
    run(lib["writer"])
    assert not sidecar_for(lib["path"]["shot3.jpg"]).exists()
    conn.execute("UPDATE assets SET caption='Maya at Baga', caption_source='manual' WHERE id=?",
                 (shot3,))
    conn.commit()
    run(lib["writer"])
    assert "Maya at Baga" in sidecar_for(lib["path"]["shot3.jpg"]).read_text()
