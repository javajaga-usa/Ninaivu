"""The map: every photograph with a location, grouped, listed and opened.

It asked for 1,500 pins and drew one per photograph; this library has 23,776
with a location, so it showed a sixteenth of them. It also read `gps_lat` from
answers that said `lat`, so it drew none. And locations filled in for the
photographs taken close in time to one that has a location.
"""
import pytest

from ninaivu.storage import db, geo

ROOT = "/lib"
DAY = 86400.0


@pytest.fixture()
def conn(tmp_path):
    return db.init_db(tmp_path / "index.db")


def add(conn, rel, *, lat=None, lon=None, date="2019-07-21", when=None, kind="picture",
        visibility=1, city=None, country=None, source="exif", nsfw=0):
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, gps_lat, gps_lon, date_key, "
        "captured_at, date_source, visibility, city, country, thumb, nsfw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ROOT, rel, rel.rsplit("/", 1)[-1], kind, lat, lon, date, when, source, visibility,
         city, country, "ab/" + rel.replace("/", "_"), nsfw))
    conn.commit()
    return conn.execute("SELECT id FROM assets WHERE rel_path=?", (rel,)).fetchone()[0]


@pytest.fixture()
def trip(conn):
    """Ooty, one day: six near each other; Bengaluru, two; one in Frisco."""
    for n in range(6):
        add(conn, f"2019/07/21/ooty{n}.jpg", lat=11.41 + n * 0.001, lon=76.69,
            when=1563700000 + n * 600, city="Ooty", country="IN")
    for n in range(2):
        add(conn, f"2019/07/25/blr{n}.jpg", lat=12.97, lon=77.59, date="2019-07-25",
            when=1564000000 + n * 60, city="Bengaluru", country="IN")
    add(conn, "2021/05/01/frisco.jpg", lat=33.15, lon=-96.82, date="2021-05-01",
        when=1619870000, city="Frisco", country="US")
    add(conn, "2019/07/21/hidden.jpg", lat=11.41, lon=76.69, visibility=2, city="Ooty",
        country="IN")
    return conn


# -- grouped ---------------------------------------------------------------------

def test_the_world_at_a_glance_groups_near_places_and_counts_every_photograph(trip):
    found = geo.clusters(trip, ROOT, zoom=2)
    assert sum(c["count"] for c in found) == 9, "the hidden one is not a family member's"
    frisco = next(c for c in found if c["country"] == "US")
    assert frisco["count"] == 1 and frisco["city"] == "Frisco"


def test_zooming_in_splits_the_groups(trip):
    wide = geo.clusters(trip, ROOT, zoom=3)
    close = geo.clusters(trip, ROOT, zoom=10)
    india = lambda cs: [c for c in cs if c["country"] == "IN"]   # noqa: E731
    assert len(india(wide)) == 1 and india(wide)[0]["count"] == 8
    assert sorted(c["count"] for c in india(close)) == [2, 6]


def test_only_what_is_on_screen_is_asked_for(trip):
    india = geo.clusters(trip, ROOT, zoom=5, bounds=(5, 30, 70, 90))
    assert sum(c["count"] for c in india) == 8


def test_a_map_panned_round_the_world_still_finds_it(trip):
    # Leaflet reports longitudes past 180 once the world repeats.
    assert sum(c["count"] for c in geo.clusters(trip, ROOT, zoom=2,
                                                bounds=(-80, 80, 250, 290))) == 1
    assert sum(c["count"] for c in geo.clusters(trip, ROOT, zoom=1,
                                                bounds=(-80, 80, -300, 300))) == 9


def test_an_admin_sees_the_hidden_ones_too(trip):
    assert sum(c["count"] for c in geo.clusters(trip, ROOT, zoom=2, max_visibility=2)) == 10


def test_a_group_opens_to_its_photographs_and_no_others(trip):
    close = geo.clusters(trip, ROOT, zoom=10)
    blr = next(c for c in close if c["city"] == "Bengaluru")
    ids, total = geo.photo_ids(trip, ROOT, bounds=tuple(blr["bounds"]))
    assert total == 2 and len(ids) == 2


# -- places ------------------------------------------------------------------------

def test_places_are_listed_most_photographed_first_with_the_years(trip):
    answer = geo.places(trip, ROOT)
    assert [(p["country"], p["city"], p["count"]) for p in answer["places"]] == [
        ("IN", "Ooty", 6), ("IN", "Bengaluru", 2), ("US", "Frisco", 1)]
    assert answer["years"] == [2021, 2019] and answer["total"] == 9
    assert answer["bounds"][0] == pytest.approx(11.41) and answer["bounds"][3] == pytest.approx(77.59)


def test_a_place_opens_by_name(trip):
    ids, total = geo.photo_ids(trip, ROOT, place=("IN", "Ooty"))
    assert total == 6


def test_a_year_narrows_everything_but_the_list_of_years(trip):
    answer = geo.places(trip, ROOT, year=2021)
    assert [p["city"] for p in answer["places"]] == ["Frisco"]
    assert answer["years"] == [2021, 2019], "so another year can still be chosen"


def test_a_person_narrows_the_map(trip):
    ooty = geo.photo_ids(trip, ROOT, place=("IN", "Ooty"))[0][0]
    trip.execute("PRAGMA foreign_keys = OFF")          # no person record needed here
    trip.execute("INSERT INTO faces(asset_id, person_id, bbox, embedding) "
                 "VALUES (?, 7, '[0,0,1,1]', x'00')",
                 (ooty,))
    trip.commit()
    assert geo.places(trip, ROOT, person=7)["total"] == 1


# -- trips -------------------------------------------------------------------------

def test_a_year_is_a_route_of_stops(trip):
    stops = geo.trail(trip, ROOT, year=2019)
    assert [(s["city"], s["count"]) for s in stops] == [("Ooty", 6), ("Bengaluru", 2)]
    assert stops[0]["first"] == "2019-07-21" and stops[1]["first"] == "2019-07-25"


# -- locations filled in ------------------------------------------------------------

def test_a_photograph_taken_within_the_hour_is_given_the_place(trip):
    near = add(trip, "2019/07/21/camera1.jpg", when=1563700000 + 1800)      # 30 min later
    later = add(trip, "2019/07/21/camera2.jpg", when=1563700000 + 3 * 3600)  # hours later
    guessed = add(trip, "2019/07/21/scan.jpg", when=1563700000 + 60, source="mtime")
    found = geo.location_suggestions(trip, ROOT, max_hours=1)
    assert found["total"] == 1
    group = found["groups"][0]
    assert (group["city"], group["ids"]) == ("Ooty", [near])
    assert geo.apply_locations(trip, ROOT, max_hours=1) == 1
    row = trip.execute("SELECT gps_lat, city, location_inferred FROM assets WHERE id=?",
                       (near,)).fetchone()
    assert row[0] == pytest.approx(11.41 + 0.003, abs=0.002) and row[1] == "Ooty" and row[2] == 1
    for untouched in (later, guessed):
        assert trip.execute("SELECT gps_lat FROM assets WHERE id=?", (untouched,)).fetchone()[0] is None


def test_only_the_chosen_ones_are_given_it(trip):
    one = add(trip, "2019/07/21/a.jpg", when=1563700000 + 60)
    two = add(trip, "2019/07/25/b.jpg", date="2019-07-25", when=1564000000 + 120)
    assert geo.apply_locations(trip, ROOT, max_hours=1, ids=[two]) == 1
    assert trip.execute("SELECT gps_lat FROM assets WHERE id=?", (one,)).fetchone()[0] is None


def test_a_filled_in_location_never_suggests_another(trip):
    # The last photograph with a real location was taken at +3000 s.
    first = add(trip, "2019/07/21/a.jpg", when=1563700000 + 6000)       # 50 min after it
    assert geo.apply_locations(trip, ROOT, max_hours=1) == 1
    chained = add(trip, "2019/07/21/b.jpg", when=1563700000 + 9000)     # 50 min after `first`
    ids = [i for g in geo.location_suggestions(trip, ROOT, max_hours=1)["groups"] for i in g["ids"]]
    assert first not in ids and chained not in ids


def test_undo_takes_back_every_filled_in_location_and_nothing_else(trip):
    add(trip, "2019/07/21/a.jpg", when=1563700000 + 60)
    geo.apply_locations(trip, ROOT, max_hours=1)
    assert geo.undo_locations(trip, ROOT) == 1
    assert trip.execute("SELECT COUNT(*) FROM assets WHERE gps_lat IS NOT NULL").fetchone()[0] == 10
    assert geo.location_suggestions(trip, ROOT, max_hours=1)["inferred"] == 0


# -- through the app -----------------------------------------------------------------

@pytest.fixture()
def located(app, scanned):
    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET gps_lat = 11.41, gps_lon = 76.69, city = 'Ooty', "
                 "country = 'IN', visibility = 0")
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM assets WHERE trashed = 0").fetchone()[0]


def test_the_map_answers_a_family_member(as_family, located):
    places = as_family.get("/api/geo/places").get_json()
    assert places["places"][0]["city"] == "Ooty" and places["total"] == located
    clusters = as_family.get("/api/geo/clusters?zoom=4").get_json()
    assert clusters["total"] == located
    group = clusters["clusters"][0]["bounds"]
    photos = as_family.get("/api/geo/photos?south={}&north={}&west={}&east={}".format(*group))
    assert photos.get_json()["total"] == located
    by_name = as_family.get("/api/geo/photos?country=IN&city=Ooty").get_json()
    assert by_name["total"] == located


def test_a_guest_is_never_told_where(anon, as_guest, located):
    """A guest's payload has no GPS; the map would have given it back."""
    for client in (anon, as_guest):
        for url in ("/api/geo/points", "/api/geo/places", "/api/geo/clusters?zoom=3",
                    "/api/geo/photos?country=IN&city=Ooty", "/api/geo/trail?year=2019"):
            assert client.get(url).status_code in (401, 403), url


def test_near_a_photograph_is_not_a_guests_question(anon, as_family, located, scanned):
    _, conn, _ = scanned
    anchor, far = (r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE trashed = 0 ORDER BY id LIMIT 2"))
    # One photograph a long way off, so "near" visibly narrows the list. With
    # every photograph in Ooty, near and not-near gave the same answer and the
    # guest's check could not fail.
    conn.execute("UPDATE assets SET gps_lat = 12.97, gps_lon = 77.59, city = 'Bengaluru' "
                 "WHERE id = ?", (far,))
    conn.commit()
    family = as_family.get(f"/api/assets?near={anchor}&limit=500")
    guest = anon.get(f"/api/assets?near={anchor}&limit=500")
    assert family.get_json()["total"] == located - 1
    assert guest.status_code == 200, "ignored, not refused"
    assert guest.get_json()["total"] == located, "a guest's near narrowed the list"


def test_a_trip_needs_a_year(as_family, located):
    assert as_family.get("/api/geo/trail").status_code == 400


def test_the_console_suggests_applies_and_undoes(as_admin, scanned):
    _, conn, _ = scanned
    first, second = [r[0] for r in conn.execute("SELECT id FROM assets ORDER BY id LIMIT 2")]
    conn.execute("UPDATE assets SET gps_lat = NULL, gps_lon = NULL, captured_at = NULL")
    conn.execute("UPDATE assets SET gps_lat = 11.41, gps_lon = 76.69, city = 'Ooty', country = 'IN', "
                 "captured_at = 1563700000, date_source = 'exif' WHERE id = ?", (first,))
    conn.execute("UPDATE assets SET captured_at = 1563701000, date_source = 'exif' WHERE id = ?",
                 (second,))
    conn.commit()
    found = as_admin.get("/api/admin/locations?hours=1").get_json()
    assert found["total"] == 1 and found["groups"][0]["ids"] == [second]
    assert as_admin.get("/api/admin/locations?hours=5").status_code == 400
    applied = as_admin.post("/api/admin/locations/apply", json={"hours": 1, "all": True})
    assert applied.get_json()["applied"] == 1
    assert as_admin.post("/api/admin/locations/undo").get_json()["undone"] == 1


def test_only_an_admin_fills_in_locations(as_family):
    assert as_family.get("/api/admin/locations").status_code in (401, 403, 404)
