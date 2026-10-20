"""Then and now: the same place, the same person, years apart.

Off until switched on; the same place is the same small square of the map
that also looks alike to the picture search, and the same view with no place
is held to a stricter likeness; each named person is paired early with late,
and a parent with their child at the same age once both years of birth are
known. Everything goes through the viewer's own limits: a hidden photograph is
never paired, and a guest is told neither who nor where.
"""
from __future__ import annotations

import json
import struct

import pytest

from ninaivu.api import api_then_and_now
from ninaivu.media import then_and_now as pairing
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db, family_tree
from ninaivu.storage import then_and_now as born_store

#: Four directions of a small picture-search space: a temple tank, a beach,
#: a doorstep, and a kitchen.
TANK, BEACH, DOOR, KITCHEN = (1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)


def _vector(direction, wobble=0.0):
    v = [float(x) for x in direction]
    v[(v.index(1.0) + 1) % 4] += wobble
    return struct.pack("4f", *v)


def _date(conn, aid, year, month=6, day=15):
    import calendar
    stamp = calendar.timegm((year, month, day, 12, 0, 0))
    conn.execute("UPDATE assets SET date_key=?, captured_at=? WHERE id=?",
                 (f"{year:04d}-{month:02d}-{day:02d}", stamp, aid))


def _face(conn, aid, person, quality=0.9):
    w, h = conn.execute("SELECT width, height FROM assets WHERE id=?", (aid,)).fetchone()
    box = [w * 0.4, h * 0.3, w * 0.2, h * 0.2]
    eyes = [[w * 0.45, h * 0.38], [w * 0.55, h * 0.38]]
    conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, landmarks, embedding, quality) "
                 "VALUES (?, ?, 'confirmed', ?, ?, x'00', ?)",
                 (aid, person, json.dumps(box), json.dumps(eyes), quality))


@pytest.fixture()
def pairs(app, people):
    """Twelve photographs: the tank in 1994 and 2021 (same square of the map),
    the beach in 1999 and 2001 (too close in years), a scanned doorstep from
    1988 with no place and the doorstep in 2019, a kitchen in 1990 and 2020
    at the tank's place (different view), and Amma (born 1960) and Arun
    (born 1990) in photographs at the same ages."""
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.then_and_now = True
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 ORDER BY id")]
    assert len(ids) >= 12
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET sharpness=200, brightness=0.5, gps_lat=NULL, "
                     "gps_lon=NULL, city=NULL, country=NULL, dup_group=NULL, "
                     "width=1200, height=900")
        conn.execute("DELETE FROM embeddings")
        plan = [
            # id index, year, vector, place
            (0, 1994, _vector(TANK), (9.9195, 78.1193, "Madurai")),
            (1, 2021, _vector(TANK, 0.3), (9.9196, 78.1194, "Madurai")),
            (2, 1999, _vector(BEACH), (13.05, 80.28, "Chennai")),
            (3, 2001, _vector(BEACH, 0.2), (13.05, 80.28, "Chennai")),
            (4, 1988, _vector(DOOR), None),
            (5, 2019, _vector(DOOR, 0.25), (11.0, 77.0, "Coimbatore")),
            (6, 1990, _vector(KITCHEN), (9.9195, 78.1193, "Madurai")),
            (7, 2020, _vector(BEACH, 0.9), (9.9195, 78.1193, "Madurai")),
        ]
        for n, year, vector, place in plan:
            _date(conn, ids[n], year)
            conn.execute("INSERT INTO embeddings(asset_id, model, dim, vector) VALUES (?,?,?,?)",
                         (ids[n], "test", 4, vector))
            if place:
                conn.execute("UPDATE assets SET gps_lat=?, gps_lon=?, city=?, country='India' "
                             "WHERE id=?", (place[0], place[1], place[2], ids[n]))
        amma = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Amma', 0)").lastrowid
        arun = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Arun', 0)").lastrowid
        # Amma at 6 (1966) and at 30 (1990); Arun at 6 (1996) and at 30 (2020).
        for n, year, who in ((8, 1966, amma), (9, 1990, amma), (10, 1996, arun), (11, 2020, arun)):
            _date(conn, ids[n], year)
            _face(conn, ids[n], who)
        conn.commit()
    db.forget_embeddings()
    family_tree.add(conn, amma, arun, "parent", None)
    return {"ids": ids, "conn": conn, "cfg": cfg, "amma": amma, "arun": arun}


def test_off_by_default_and_under_advanced():
    assert Config().then_and_now is False
    assert "then_and_now" in GROUPS["Advanced"]


def test_every_route_is_404_until_switched_on(as_family, app):
    assert as_family.get("/api/then-and-now").status_code == 404
    assert as_family.get("/api/then-and-now/today").status_code == 404
    assert as_family.put("/api/then-and-now/born/1", json={"year": 1960}).status_code == 404
    assert as_family.post("/api/then-and-now/film", json={}).status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["then_now"] is False


def test_the_same_place_and_the_same_view(as_family, pairs):
    ids = pairs["ids"]
    got = as_family.get("/api/then-and-now").get_json()
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["then_now"] is True
    found = {(p["then"]["id"], p["now"]["id"]): p for p in got["places"]}
    tank = found[(ids[0], ids[1])]
    assert tank["kind"] == "place" and tank["years"] == 27 and tank["place"] == "Madurai, India"
    assert tank["then"]["date"] == "1994-06-15" and tank["then"]["thumb_v"]
    door = found[(ids[4], ids[5])]
    assert door["kind"] == "view" and door["years"] == 31
    assert (ids[2], ids[3]) not in found, "two years apart is not then and now"
    assert not any(ids[6] in key or ids[7] in key for key in found), \
        "the same square of the map, but not the same view"
    assert got["films"] is False


def test_a_hidden_photograph_is_never_paired(as_family, pairs):
    conn, ids = pairs["conn"], pairs["ids"]
    conn.execute("UPDATE assets SET visibility=? WHERE id=?", (auth.VIS_HIDDEN, ids[1]))
    conn.commit()
    got = as_family.get("/api/then-and-now").get_json()
    assert all(ids[1] not in (p["then"]["id"], p["now"]["id"]) for p in got["places"])


def test_people_early_and_late(as_family, pairs):
    got = as_family.get("/api/then-and-now").get_json()
    amma = next(p for p in got["people"] if p["person"]["name"] == "Amma")
    pair = amma["pairs"][0]
    assert pair["kind"] == "person" and pair["years"] == 24
    assert pair["then"]["eyes"] and pair["now"]["eyes"]


def test_parent_and_child_at_the_same_age_once_their_years_are_known(as_family, pairs):
    ids = pairs["ids"]
    got = as_family.get("/api/then-and-now").get_json()
    assert got["same_age"] == []
    assert {p["name"] for p in got["ask_born"]} == {"Amma", "Arun"}
    assert as_family.put(f"/api/then-and-now/born/{pairs['amma']}", json={"year": 1960}).status_code == 200
    assert as_family.put(f"/api/then-and-now/born/{pairs['arun']}", json={"year": "1990"}).status_code == 400
    assert as_family.put(f"/api/then-and-now/born/{pairs['arun']}", json={"year": 1990}).status_code == 200
    got = as_family.get("/api/then-and-now").get_json()
    assert got["ask_born"] == []
    family = got["same_age"][0]
    assert family["parent"]["name"] == "Amma" and family["child"]["name"] == "Arun"
    assert [(p["age"], p["then"]["id"], p["now"]["id"]) for p in family["pairs"]] == [
        (6, ids[8], ids[10]), (30, ids[9], ids[11])]
    assert as_family.put(f"/api/then-and-now/born/{pairs['arun']}", json={"year": None}).status_code == 200
    assert as_family.get("/api/then-and-now").get_json()["same_age"] == []


def test_somebody_not_on_the_people_page_cannot_be_given_a_year(as_family, pairs):
    conn, ids = pairs["conn"], pairs["ids"]
    conn.execute("UPDATE assets SET visibility=? WHERE id IN (?, ?)",
                 (auth.VIS_HIDDEN, ids[10], ids[11]))
    conn.commit()
    assert as_family.put(f"/api/then-and-now/born/{pairs['arun']}", json={"year": 1990}).status_code == 404
    assert as_family.put("/api/then-and-now/born/999999", json={"year": 1990}).status_code == 404


def test_a_merge_keeps_the_year(pairs):
    conn = pairs["conn"]
    born_store.set_born(conn, pairs["arun"], 1990)
    other = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Arun K', 0)").lastrowid
    conn.commit()
    db.merge_people(conn, pairs["arun"], other)
    assert born_store.born(conn) .get(other) == 1990


def test_a_guest_is_told_neither_who_nor_where(as_guest, pairs):
    pairs["conn"].execute("UPDATE assets SET visibility=?", (auth.VIS_PUBLIC,))
    pairs["conn"].commit()
    got = as_guest.get("/api/then-and-now").get_json()
    assert got["places"] and all("place" not in p for p in got["places"])
    assert got["people"] == [] and got["same_age"] == [] and got["ask_born"] == []
    assert as_guest.put(f"/api/then-and-now/born/{pairs['amma']}", json={"year": 1960}).status_code in (401, 403)


def test_this_week_years_ago(as_family, pairs, monkeypatch):
    monkeypatch.setattr(api_then_and_now, "_today", lambda: (6, 18))
    got = as_family.get("/api/then-and-now/today").get_json()
    assert got["pairs"] and got["pairs"][0]["years"] >= got["pairs"][-1]["years"]
    assert all(p["then"]["date"][5:7] == "06" for p in got["pairs"])
    monkeypatch.setattr(api_then_and_now, "_today", lambda: (1, 2))
    assert as_family.get("/api/then-and-now/today").get_json()["pairs"] == []


def test_around_today_wraps_the_year():
    pair = {"then": {"id": 1, "date_key": "1990-12-30"}, "now": {"id": 2}, "years": 30}
    assert pairing.around_today([pair], 1, 2) == [pair]
    assert pairing.around_today([pair], 1, 10) == []


def test_a_film_needs_memory_films_and_takes_only_what_may_be_seen(as_family, pairs, monkeypatch):
    ids = pairs["ids"]
    body = {"pairs": [{"then": {"id": ids[0]}, "now": {"id": ids[1]}}]}
    assert as_family.post("/api/then-and-now/film", json=body).status_code == 404
    pairs["cfg"].memory_films = True
    started = {}
    monkeypatch.setattr(api_then_and_now.maker, "start",
                        lambda film_id, spec, **kw: started.update(spec=spec, id=film_id))
    assert as_family.post("/api/then-and-now/film", json={"pairs": []}).status_code == 400
    assert as_family.post("/api/then-and-now/film",
                          json={"pairs": [{"then": {"id": "x"}, "now": {"id": 1}}]}).status_code == 400
    made = as_family.post("/api/then-and-now/film", json={
        **body, "pairs": [*body["pairs"],
                          {"then": {"id": ids[8], "person": pairs["amma"]},
                           "now": {"id": ids[9], "person": pairs["amma"]}}]})
    assert made.status_code == 202, made.get_json()
    items = started["spec"]["items"]
    assert started["spec"]["style"] == "then_and_now" and len(items) == 2
    assert items[0]["kind"] == "pair" and items[0]["then"]["eyes"] is None
    assert items[0]["then"]["label"] == "1994" and items[0]["now"]["label"] == "2021"
    assert [pytest.approx(eye) for eye in items[1]["then"]["eyes"]] == [[0.45, 0.38], [0.55, 0.38]]
    pairs["conn"].execute("UPDATE assets SET visibility=? WHERE id IN (?, ?)",
                          (auth.VIS_HIDDEN, ids[0], ids[1]))
    pairs["conn"].commit()
    assert as_family.post("/api/then-and-now/film", json=body).status_code == 409
