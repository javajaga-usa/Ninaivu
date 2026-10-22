"""Smart albums: a search kept under a name, that fills itself."""

from __future__ import annotations

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu import build_services, create_home_app
from ninaivu.server import auth
from ninaivu.storage import smart


def test_rules_keep_only_what_a_rule_may_say():
    assert smart.clean_rules({"people": [3, 1, 3], "kinds": ["video"], "text": " cake ",
                              "date_from": "2023-01-01"}) == {
        "people": [1, 3], "kinds": ["video"], "text": "cake", "date_from": "2023-01-01"}
    for bad in ({}, {"people": ["x"]}, {"kinds": ["film"]}, {"date_from": "last year"},
                {"favorites": "yes"}, {"people": [True]}):
        with pytest.raises(ValueError):
            smart.clean_rules(bad)


@pytest.fixture()
def home(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                     role=auth.ROLE_GUEST, created_by=admin.id)
    auth.create_user(conn, "cousin", "cousin-password-1", display_name="Cousin",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    conn.execute("UPDATE assets SET visibility=2 WHERE folder='private'")
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}
    arjun = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES('Arjun', 0)").lastrowid

    def face(asset):
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                     "VALUES(?, ?, 'confirmed', '[0,0,10,10]', x'00')", (ids[asset], arjun))
        conn.commit()

    face("shot1.jpg")
    face("shot2.jpg")
    face("secret0.jpg")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    yield {"ids": ids, "face": face, "conn": conn, "app": app,
           "family": login(app.test_client(), *FAMILY),
           "admin": login(app.test_client(), *ADMIN),
           "guest": login(app.test_client(), *GUEST)}
    services.stop(timeout=5.0)


def seen(client, album_id):
    data = client.get("/api/segments", query_string={"smart": album_id}).get_json()
    return {item[0] for seg in data["segments"] for item in seg["items"]}


def test_a_search_is_kept_and_fills_itself(home):
    made = home["admin"].post("/api/smart-albums?q=arjun+photos", json={"name": "Arjun"})
    assert made.status_code == 201
    album = made.get_json()
    assert album["rules"]["people_names"] == ["Arjun"] and album["rules"]["kinds"] == ["picture"]
    assert album["n"] == 3, "the admin sees the hidden one too"
    assert seen(home["family"], album["id"]) == {home["ids"]["shot1.jpg"], home["ids"]["shot2.jpg"]}
    home["face"]("shot3.jpg")
    assert home["ids"]["shot3.jpg"] in seen(home["family"], album["id"]), "new photographs join"
    listed = home["family"].get("/api/smart-albums").get_json()["albums"]
    assert [(a["name"], a["n"], a["can_change"]) for a in listed] == [("Arjun", 3, False)]


def test_one_not_shared_is_its_makers_alone(home):
    album = home["family"].post("/api/smart-albums?q=arjun",
                                json={"name": "Mine", "shared": False}).get_json()
    assert [a["name"] for a in home["family"].get("/api/smart-albums").get_json()["albums"]] == ["Mine"]
    cousin = login(home["app"].test_client(), "cousin", "cousin-password-1")
    assert cousin.get("/api/smart-albums").get_json()["albums"] == []
    assert cousin.get(f"/api/segments?smart={album['id']}").status_code == 404
    # An administrator can see every album, the way they can every hand-made one.
    assert home["admin"].get(f"/api/segments?smart={album['id']}").status_code == 200
    assert home["family"].delete(f"/api/smart-albums/{album['id']}").status_code == 200


def test_somebody_elses_album_is_theirs_to_change(home):
    album = home["admin"].post("/api/smart-albums?q=arjun", json={"name": "Dad's"}).get_json()
    assert home["family"].patch(f"/api/smart-albums/{album['id']}", json={"name": "x"}).status_code == 403
    assert home["family"].delete(f"/api/smart-albums/{album['id']}").status_code == 403
    renamed = home["admin"].patch(f"/api/smart-albums/{album['id']}", json={"name": "Arjun"}).get_json()
    assert renamed["name"] == "Arjun"


def test_a_guest_cannot_open_one(home):
    album = home["admin"].post("/api/smart-albums?q=arjun", json={"name": "Arjun"}).get_json()
    assert home["guest"].get(f"/api/segments?smart={album['id']}").status_code == 404
    assert home["guest"].get("/api/smart-albums").status_code in (401, 403)


def test_an_album_of_nothing_is_refused_and_a_missing_one_is_not_found(home):
    assert home["family"].post("/api/smart-albums", json={"name": "Everything"}).status_code == 400
    assert home["family"].post("/api/smart-albums?q=arjun", json={"name": " "}).status_code == 400
    assert home["family"].get("/api/segments?smart=999").status_code == 404


def test_the_dates_a_phrase_meant_are_kept_as_dates(home):
    album = home["family"].post("/api/smart-albums?q=arjun+2023", json={"name": "2023"}).get_json()
    rules = album["rules"]
    assert rules["date_from"] == "2023-01-01" and rules["date_to"] == "2023-12-31"
    assert "text" not in rules
