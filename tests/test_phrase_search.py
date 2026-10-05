"""Searching in plain words: people, places, "videos" and "favourites"."""

from __future__ import annotations

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu import build_services, create_home_app
from ninaivu.server import auth
from ninaivu.utils import phrase

PEOPLE = [(1, "Maya Raj"), (2, "Arjun"), (3, "Maya Iyer"), (4, "Grandma")]
PLACES = ["Ooty", "India", "New York", "Chennai"]


def read(text, **kw):
    return phrase.parse(text, people=PEOPLE, places=PLACES, **kw)


def test_names_places_and_kinds_come_out_and_the_rest_is_left():
    found = read("Arjun and Grandma at the beach in Ooty videos")
    assert sorted(n for _, n in found.people) == ["Arjun", "Grandma"]
    assert found.place == "Ooty" and found.kind == "video"
    assert found.text == "beach"


def test_a_first_name_two_people_share_is_not_a_guess():
    found = read("Maya birthday")
    assert found.people == [] and found.text == "Maya birthday"
    assert [n for _, n in read("maya raj birthday").people] == ["Maya Raj"]


def test_a_unique_first_name_is_enough():
    people = [(1, "Arjun Kumar"), (2, "Maya Raj")]
    found = phrase.parse("arjun cake", people=people)
    assert found.people == [(1, "Arjun Kumar")] and found.text == "cake"


def test_the_longest_place_wins_and_only_one_place():
    found = read("new york chennai")
    assert found.place == "New York" and found.text == "chennai"


def test_what_the_page_chose_is_left_alone():
    found = read("Arjun videos", person_set=True, kind_set=True)
    assert not found.matched and found.text == "Arjun videos"


def test_nothing_recognised_changes_nothing():
    found = read("red car at night")
    assert not found.matched and found.text == "red car at night"
    assert found.chips() == []


def test_joining_words_alone_are_not_a_search():
    found = read("Arjun and Grandma")
    assert found.text == "" and len(found.people) == 2
    assert read("favourites from Chennai").favorites is True
    assert read("favourites from Chennai").text == ""


def test_part_of_a_word_is_not_a_name():
    found = phrase.parse("arjunaa", people=[(2, "Arjun")])
    assert not found.matched


# -- through the gallery ------------------------------------------------------

@pytest.fixture()
def home(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                     role=auth.ROLE_GUEST, created_by=admin.id)
    conn.execute("UPDATE assets SET visibility=2 WHERE folder='private'")
    conn.execute("UPDATE assets SET visibility=0 WHERE folder LIKE 'shared%'")
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}

    def person(name):
        return conn.execute("INSERT INTO people_clusters(name, created_at) VALUES(?, 0)",
                            (name,)).lastrowid

    def face(asset, who):
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                     "VALUES(?, ?, 'confirmed', '[0,0,10,10]', x'00')", (ids[asset], who))

    arjun, maya, secret = person("Arjun"), person("Maya Raj"), person("Hidden Uncle")
    face("shot1.jpg", arjun)
    face("shot1.jpg", maya)
    face("shot2.jpg", arjun)
    face("beach0.jpg", maya)
    face("secret0.jpg", secret)
    conn.execute("UPDATE assets SET city='Ooty', country='India' WHERE filename IN "
                 "('shot2.jpg', 'shot3.jpg')")
    conn.execute("UPDATE assets SET city='Secret Town' WHERE filename='secret1.jpg'")
    conn.commit()
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    yield {"ids": ids, "family": login(app.test_client(), *FAMILY),
           "admin": login(app.test_client(), *ADMIN),
           "guest": login(app.test_client(), *GUEST)}
    services.stop(timeout=5.0)


def found(client, q, **extra):
    data = client.get("/api/segments", query_string={"q": q, **extra}).get_json()
    ids = {item[0] for seg in data["segments"] for item in seg["items"]}
    return ids, data


def test_two_names_find_the_photographs_with_both(home):
    ids, data = found(home["family"], "Maya Raj and Arjun")
    assert ids == {home["ids"]["shot1.jpg"]}
    assert {c["label"] for c in data["understood"]} == {"Maya Raj", "Arjun"}


def test_a_name_and_a_place_together(home):
    ids, data = found(home["family"], "arjun in ooty")
    assert ids == {home["ids"]["shot2.jpg"]}
    assert {c["type"] for c in data["understood"]} == {"person", "place"}
    ids, _ = found(home["family"], "India")
    assert ids == {home["ids"]["shot2.jpg"], home["ids"]["shot3.jpg"]}


def test_words_searched_as_words_when_asked(home):
    _, data = found(home["family"], "arjun", plain="1")
    assert data["understood"] is None


def test_nobody_is_found_by_a_name_only_hidden_photographs_have(home):
    _, data = found(home["family"], "Hidden Uncle")
    assert data["understood"] is None
    _, data = found(home["family"], "Secret Town")
    assert data["understood"] is None
    ids, data = found(home["admin"], "Hidden Uncle")
    assert ids == {home["ids"]["secret0.jpg"]} and data["understood"]


def test_a_guest_cannot_search_for_people_or_places(home):
    _, data = found(home["guest"], "Maya Raj")
    assert data["understood"] is None
    _, data = found(home["guest"], "Ooty")
    assert data["understood"] is None


def test_kind_words_narrow_the_kind(home):
    ids, data = found(home["family"], "arjun photos")
    assert ids == {home["ids"]["shot1.jpg"], home["ids"]["shot2.jpg"]}
    assert any(c["type"] == "kind" for c in data["understood"])
    ids, _ = found(home["family"], "arjun videos")
    assert ids == set()


def test_the_full_listing_says_what_it_understood_too(home):
    data = home["family"].get("/api/assets", query_string={"q": "Arjun last year"}).get_json()
    assert data["understood"][0]["label"] == "Arjun"
    assert "dates" in data
