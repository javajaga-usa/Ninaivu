"""Searching by where a photograph was taken, combined with who is in it.

There is no geocoder in Ninaivu, so none of this produces place *names*. What
it does produce is the filter underneath them: photographs taken near this
one. That is the half that needs no bundled dataset, and it composes with the
face index — "Maya, near here" — which was the point of the roadmap item.
"""


import pytest

from ninaivu.storage import db

# Two points about 1.5 km apart, and a third in another country.
HOME = (51.5007, -0.1246)
NEARBY = (51.5100, -0.1300)
ABROAD = (48.8584, 2.2945)


@pytest.fixture()
def located(scanned):
    """Give three of the indexed photographs coordinates."""
    cfg, conn, _ = scanned
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' ORDER BY id LIMIT 3")]
    for asset_id, (lat, lon) in zip(ids, (HOME, NEARBY, ABROAD)):
        conn.execute("UPDATE assets SET gps_lat=?, gps_lon=? WHERE id=?",
                     (lat, lon, asset_id))
    conn.commit()
    return cfg, conn, ids


# ---------------------------------------------------------------------------
# The distance function itself
# ---------------------------------------------------------------------------

def test_the_database_can_measure_distance(scanned):
    _, conn, _ = scanned
    km = conn.execute("SELECT ninaivu_km(?,?,?,?) d",
                      (*HOME, *ABROAD)).fetchone()["d"]
    assert 330 < km < 350, "London to Paris"


def test_a_photograph_with_no_location_is_unknown_not_far_away(scanned):
    """NULL, so a comparison excludes it rather than ranking it last."""
    _, conn, _ = scanned
    assert conn.execute(
        "SELECT ninaivu_km(?,?,NULL,NULL) d", HOME).fetchone()["d"] is None


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------

def test_only_photographs_within_the_radius_come_back(located):
    cfg, conn, ids = located
    rows, _ = db.query_assets(conn, [cfg.active_root], near=HOME,
                              radius_km=5.0, limit=100)
    found = {r["id"] for r in rows}
    assert ids[0] in found and ids[1] in found
    assert ids[2] not in found, "Paris is not within five kilometres of London"


def test_a_tight_radius_excludes_the_neighbour(located):
    cfg, conn, ids = located
    rows, _ = db.query_assets(conn, [cfg.active_root], near=HOME,
                              radius_km=0.5, limit=100)
    assert {r["id"] for r in rows} == {ids[0]}


def test_a_wide_radius_reaches_the_other_country(located):
    cfg, conn, ids = located
    rows, _ = db.query_assets(conn, [cfg.active_root], near=HOME,
                              radius_km=400.0, limit=100)
    assert ids[2] in {r["id"] for r in rows}


def test_photographs_without_coordinates_are_never_matched(located):
    cfg, conn, ids = located
    rows, _ = db.query_assets(conn, [cfg.active_root], near=HOME,
                              radius_km=20000.0, limit=500)
    assert len(rows) == 3, "only the three that have a location"


def test_the_count_matches_what_is_returned(located):
    """Pagination is done in SQL, so the filter has to be too."""
    cfg, conn, _ = located
    rows, total = db.query_assets(conn, [cfg.active_root], near=HOME,
                                  radius_km=5.0, limit=100)
    assert total == len(rows)


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

def test_browsing_near_a_photograph(as_family, located):
    _, _, ids = located
    body = as_family.get(f"/api/assets?near={ids[0]}&km=5&limit=100").get_json()
    returned = {item["id"] for item in body["items"]}
    assert ids[1] in returned and ids[2] not in returned


def test_the_radius_defaults_to_a_neighbourhood(as_family, located):
    _, _, ids = located
    body = as_family.get(f"/api/assets?near={ids[0]}&limit=100").get_json()
    assert ids[2] not in {item["id"] for item in body["items"]}


def test_an_absurd_radius_is_capped(as_family, located):
    _, _, ids = located
    body = as_family.get(
        f"/api/assets?near={ids[0]}&km=99999&limit=100").get_json()
    assert body["total"] <= 3


def test_anchoring_on_a_photograph_with_no_location_is_refused(as_family, located):
    cfg, conn, ids = located
    bare = conn.execute(
        "SELECT id FROM assets WHERE gps_lat IS NULL LIMIT 1").fetchone()["id"]
    assert as_family.get(f"/api/assets?near={bare}").status_code == 400


def test_an_unreachable_anchor_is_a_404(as_family):
    """The parameter must not become a way to probe the library by id."""
    assert as_family.get("/api/assets?near=999999").status_code == 404


def test_place_and_person_narrow_together(as_family, located, scanned):
    """The point of the roadmap item: who, and where, in one query."""
    cfg, conn, ids = located
    conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Maya', 0)")
    person_id = conn.execute(
        "SELECT id FROM people_clusters WHERE name='Maya'").fetchone()["id"]
    conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
        "VALUES (?,?,'confirmed','[0,0,1,1]',X'00')", (ids[1], person_id))
    conn.commit()

    both = as_family.get(
        f"/api/assets?near={ids[0]}&km=5&person={person_id}&limit=100"
    ).get_json()
    assert [item["id"] for item in both["items"]] == [ids[1]]
