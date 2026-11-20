"""Event albums (features/event_albums): trips and festivals found from dates
and places, offered to an administrator, made into albums only on a yes."""

from __future__ import annotations

from datetime import datetime

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu import build_services, create_home_app
from ninaivu.features.event_albums import media, storage
from ninaivu.server import auth

DAY = 86400.0
NOW = datetime(2026, 11, 15, 12).timestamp()


def occasion(oid, place, start_day, days=1, count=8):
    started = datetime(2023, 5, start_day, 9).timestamp()
    return {"id": oid, "key": f"e{int(started)}", "place": place, "started_at": started,
            "ended_at": started + (days - 1) * DAY + 8 * 3600, "count": count}


# -- finding trips -----------------------------------------------------------------

def test_occasions_away_from_home_a_night_apart_are_one_trip():
    rows = [occasion(1, "Chennai", 1, count=200),
            occasion(2, "Ooty", 12), occasion(3, "Ooty", 13), occasion(4, "Coonoor", 14),
            occasion(5, "Chennai", 20, count=50)]
    found = media.trips(rows, now=NOW)
    assert len(found) == 1
    trip = found[0]
    assert trip["kind"] == "trip" and trip["occasions"] == [2, 3, 4]
    assert trip["title"] == "Ooty and Coonoor, 12–14 May 2023"
    assert trip["days"] == 3 and trip["count"] == 24
    assert trip["key"] == f"trip-{rows[1]['key']}"
    assert trip["from"] == "2023-05-12" and trip["to"] == "2023-05-14"


def test_home_is_where_most_of_the_library_was_taken():
    rows = [occasion(1, "Chennai", 1, count=500), occasion(2, "Ooty", 3, days=3, count=40)]
    assert media.home_place(rows) == "Chennai"
    assert [t["occasions"] for t in media.trips(rows, now=NOW)] == [[2]]


def test_a_day_out_is_not_a_trip_nor_is_a_handful_of_photos():
    rows = [occasion(1, "Chennai", 1, count=500), occasion(2, "Mahabalipuram", 5, count=40),
            occasion(3, "Ooty", 10, days=3, count=5)]
    assert media.trips(rows, now=NOW) == []


def test_two_trips_with_days_at_home_between_are_two():
    rows = [occasion(1, "Chennai", 1, count=500), occasion(2, "Ooty", 3, days=2, count=12),
            occasion(3, "Chennai", 6), occasion(4, "Madurai", 8, days=2, count=12)]
    assert [t["places"] for t in media.trips(rows, now=NOW)] == [["Ooty"], ["Madurai"]]


def test_an_occasion_with_no_place_is_never_a_trip():
    rows = [occasion(1, "Chennai", 1, count=500), occasion(2, None, 3, days=4, count=90)]
    assert media.trips(rows, now=NOW) == []


def test_a_trip_still_going_on_waits():
    start = NOW - 2 * DAY
    rows = [{"id": 1, "key": "e1", "place": "Chennai", "started_at": start - 90 * DAY,
             "ended_at": start - 89 * DAY, "count": 300},
            {"id": 2, "key": "e2", "place": "Ooty", "started_at": start,
             "ended_at": NOW - 3600, "count": 40}]
    assert media.trips(rows, now=NOW) == []
    assert len(media.trips(rows, now=NOW + 2 * DAY)) == 1


def test_a_festival_year_with_enough_photographs_is_offered():
    found = {"festivals": [{"key": "deepavali", "name": "Deepavali", "years": [
        {"year": 2019, "from": "2019-10-26", "to": "2019-10-28", "ids": list(range(1, 9)),
         "photos": [{"id": 3}, {"id": 5}], "checked": True},
        {"year": 2020, "from": "2020-11-13", "to": "2020-11-15", "ids": [1, 2], "photos": []}]}]}
    offered = media.festivals(found)
    assert [o["title"] for o in offered] == ["Deepavali 2019"]
    assert offered[0]["key"] == "festival-deepavali-2019" and offered[0]["cover_ids"] == [3, 5]


def test_answered_suggestions_are_not_offered_again_and_the_rest_are_newest_first():
    items = [{"key": "a", "from": "2019-01-01"}, {"key": "b", "from": "2023-05-12"},
             {"key": "c", "from": "2021-03-01"}]
    assert [c["key"] for c in media.pending(items, {"c": {}})] == ["b", "a"]


# -- through the family app -----------------------------------------------------------

@pytest.fixture()
def home(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.event_albums = True
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, root FROM assets WHERE trashed=0 ORDER BY id")]
    root = rows[0]["root"]
    base = datetime(2023, 5, 12, 9).timestamp()
    home_o = conn.execute(
        "INSERT INTO occasions(root, key, title, place, started_at, ended_at, days, count) "
        "VALUES (?, 'e1', 'Chennai', 'Chennai', ?, ?, 1, 500)",
        (root, base - 60 * DAY, base - 60 * DAY + 3600)).lastrowid
    ooty = []
    for day in range(3):
        start = base + day * DAY
        ooty.append(conn.execute(
            "INSERT INTO occasions(root, key, title, place, started_at, ended_at, days, count) "
            "VALUES (?, ?, 'Ooty', 'Ooty', ?, ?, 1, 6)",
            (root, f"e{int(start)}", start, start + 8 * 3600)).lastrowid)
    trip_ids = [r["id"] for r in rows[:6]]
    for n, asset_id in enumerate(trip_ids):
        conn.execute("UPDATE assets SET occasion_id=?, captured_at=? WHERE id=?",
                     (ooty[n % 3], base + (n % 3) * DAY + n, asset_id))
    # One trip photograph hidden, one explicit, one in the bin.
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (trip_ids[0],))
    conn.execute("UPDATE assets SET nsfw=1 WHERE id=?", (trip_ids[1],))
    conn.execute("UPDATE assets SET trashed=1 WHERE id=?", (trip_ids[2],))
    conn.execute("UPDATE assets SET occasion_id=? WHERE id=?", (home_o, rows[6]["id"]))
    conn.commit()
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    yield {"cfg": cfg, "conn": conn, "trip_ids": trip_ids,
           "admin": login(app.test_client(), *ADMIN),
           "family": login(app.test_client(), *FAMILY)}
    services.stop(timeout=5.0)


def suggestions(client):
    answer = client.get("/api/event-albums")
    assert answer.status_code == 200, answer.get_json()
    return answer.get_json()["suggestions"]


def trip(client):
    return next(s for s in suggestions(client) if s["kind"] == "trip")


def test_only_an_administrator_is_offered_them(home):
    assert home["family"].get("/api/event-albums").status_code in (401, 403)
    offered = trip(home["admin"])
    assert offered["title"].startswith("Ooty, 12–14 May 2023")
    assert offered["count"] == 18 and offered["days"] == 3
    assert home["family"].post(f"/api/event-albums/{offered['key']}/approve",
                               json={}).status_code in (401, 403)


def test_nothing_appears_to_the_family_until_it_is_approved(home):
    before = {a["name"] for a in home["family"].get("/api/albums").get_json()["albums"]}
    offered = trip(home["admin"])
    assert offered["title"] not in before
    made = home["admin"].post(f"/api/event-albums/{offered['key']}/approve", json={}).get_json()
    assert made["ok"] and made["count"] == 4, "the bin and explicit photographs stay out"
    held = {r[0] for r in home["conn"].execute(
        "SELECT asset_id FROM album_items WHERE album_id=?", (made["album_id"],))}
    assert held == set(home["trip_ids"]) - {home["trip_ids"][1], home["trip_ids"][2]}
    family = {a["name"]: a for a in home["family"].get("/api/albums").get_json()["albums"]}
    assert offered["title"] in family
    assert family[offered["title"]]["n"] == 3, "the family sees the album without the hidden photograph"
    assert all(s["key"] != offered["key"] for s in suggestions(home["admin"]))


def test_a_declined_suggestion_is_not_offered_again_until_asked(home):
    offered = trip(home["admin"])
    assert home["admin"].post(f"/api/event-albums/{offered['key']}/decline", json={}).status_code == 200
    assert all(s["key"] != offered["key"] for s in suggestions(home["admin"]))
    assert storage.decided(home["conn"])[offered["key"]]["state"] == storage.DECLINED
    again = home["admin"].post(f"/api/event-albums/{offered['key']}/approve", json={})
    assert again.status_code == 409
    assert home["admin"].post(f"/api/event-albums/{offered['key']}/again", json={}).status_code == 200
    assert any(s["key"] == offered["key"] for s in suggestions(home["admin"]))


def test_a_new_name_is_used_and_an_existing_album_is_never_filled(home):
    offered = trip(home["admin"])
    home["admin"].post("/api/albums", json={"name": "Ooty"})
    made = home["admin"].post(f"/api/event-albums/{offered['key']}/approve",
                              json={"title": "Ooty"}).get_json()
    names = {a["id"]: a["name"] for a in home["admin"].get("/api/albums").get_json()["albums"]}
    assert names[made["album_id"]] == "Ooty (2)"
    bad = home["admin"].post("/api/event-albums/nope/approve", json={})
    assert bad.status_code == 404


def test_switched_off_it_is_not_there(home):
    home["cfg"].event_albums = False
    assert home["admin"].get("/api/event-albums").status_code == 404


def test_a_festival_year_with_no_cover_photographs_still_lists(tmp_path):
    """A festival year whose answer named no cover photographs took the whole
    list down with a KeyError (a 500 on /api/event-albums)."""
    import sqlite3
    from ninaivu.features.event_albums import routes

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, thumb TEXT, trashed INT, "
                 "nsfw INT, sharpness REAL, occasion_id INT)")
    conn.executemany("INSERT INTO assets VALUES (?, ?, 0, ?, ?, NULL)",
                     [(1, "a", 0, 5.0), (2, "b", 0, 9.0), (3, "c", 1, 99.0),
                      (4, None, 0, 99.0), (5, "e", 0, 1.0)])
    festival = {"kind": "festival", "ids": [1, 2, 3, 4, 5], "cover_ids": []}
    assert routes._covers(conn, festival) == [2, 1, 5]          # not the bin, not thumbless
    assert routes._covers(conn, {"kind": "festival", "ids": []}) == []
