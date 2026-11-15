"""One search for who, where and when, typed in Tamil or English:
"Amma at the temple, 2019" and "அம்மாவுடன் கோவிலில், 2019ல்" find the same
photographs (utils/tamil_words.py, api.py's phrase reading)."""

from __future__ import annotations

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu import build_services, create_home_app
from ninaivu.server import auth
from ninaivu.utils import tamil_words


# -- the tables ----------------------------------------------------------------

def test_a_family_word_stands_for_the_one_person_named_by_it():
    spelled = tamil_words.aliases([(1, "Amma"), (2, "Ravi")])
    assert (1, "அம்மா") in spelled and (1, "mom") in spelled
    assert all(pid == 1 for pid, _ in spelled), "nobody else gets a family word"


def test_two_people_named_by_one_family_word_get_no_alias():
    assert tamil_words.aliases([(1, "Amma"), (2, "Mom")]) == []


def test_an_alias_never_takes_another_persons_whole_name():
    spelled = tamil_words.aliases([(1, "Paati"), (2, "Granny")])
    assert spelled == [], "Granny is somebody else, so Paati's group has two owners"


@pytest.mark.parametrize(("word", "stem"), [
    ("கோவிலில்", "கோவில்"), ("கோயிலுக்கு", "கோயில்"), ("கடற்கரையில்", "கடற்கரை"),
    ("திருமணத்தில்", "திருமணம்"), ("கல்யாணத்துக்கு", "கல்யாணம்"),
    ("பிறந்தநாளில்", "பிறந்தநாள்"), ("தீபாவளிக்கு", "தீபாவளி"), ("பூ", "பூ"),
])
def test_a_scene_word_loses_its_ending(word, stem):
    assert tamil_words.for_words(word) == stem


@pytest.mark.parametrize("word", ["பூமி", "கார்த்திக்", "மலர்விழி"])
def test_a_word_that_only_starts_like_a_scene_word_is_left_alone(word):
    """பூ (flower) starts பூமி (the earth); கார் (car) starts the name Karthik."""
    assert tamil_words.for_words(word) == word
    assert tamil_words.for_pictures(word) == word


def test_the_picture_search_is_given_english():
    assert tamil_words.for_pictures("கோவிலில் பூக்கள்") == "temple flowers"
    assert tamil_words.for_pictures("red car") == "red car", "English is left as it is"


# -- through the gallery ----------------------------------------------------------

@pytest.fixture()
def home(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    conn.execute("UPDATE assets SET visibility=2 WHERE folder='private'")
    conn.execute("UPDATE assets SET visibility=0 WHERE folder LIKE 'shared%'")
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}

    def person(name):
        return conn.execute("INSERT INTO people_clusters(name, created_at) VALUES(?, 0)",
                            (name,)).lastrowid

    def face(asset, who):
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                     "VALUES(?, ?, 'confirmed', '[0,0,10,10]', x'00')", (ids[asset], who))

    amma, paati = person("Amma"), person("பாட்டி")
    face("shot1.jpg", amma)
    face("shot2.jpg", amma)
    face("shot2.jpg", paati)
    conn.execute("UPDATE assets SET city='மதுரை', country='India' WHERE filename='shot2.jpg'")
    conn.commit()
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    yield {"ids": ids, "family": login(app.test_client(), *FAMILY),
           "admin": login(app.test_client(), *ADMIN)}
    services.stop(timeout=5.0)


def found(client, q, **extra):
    data = client.get("/api/segments", query_string={"q": q, **extra}).get_json()
    ids = {item[0] for seg in data["segments"] for item in seg["items"]}
    return ids, data


def chips(data):
    return {(c["type"], c["label"]) for c in data["understood"] or []}


@pytest.mark.parametrize("q", ["Amma", "அம்மா", "அம்மாவுடன்", "mom"])
def test_amma_is_found_however_she_is_written(home, q):
    ids, data = found(home["family"], q)
    assert ids == {home["ids"]["shot1.jpg"], home["ids"]["shot2.jpg"]}
    # The chip says the name the family gave, not the spelling typed.
    assert chips(data) == {("person", "Amma")}


def test_a_person_named_in_tamil_is_found_in_english_too(home):
    ids, data = found(home["family"], "grandma")
    assert ids == {home["ids"]["shot2.jpg"]}
    assert chips(data) == {("person", "பாட்டி")}


def test_people_place_and_year_in_one_tamil_search(home):
    ids, data = found(home["family"], "அம்மாவுடன் பாட்டி மதுரையில், 2019ல்")
    assert chips(data) == {("person", "Amma"), ("person", "பாட்டி"), ("place", "மதுரை")}
    assert data["dates"]["from"] == "2019-01-01" and data["dates"]["to"] == "2019-12-31"
    assert ids <= {home["ids"]["shot2.jpg"]}, "only what has all three"


def test_a_tamil_month_and_year_is_a_date(home):
    _, data = found(home["family"], "அம்மா மே 2019")
    assert data["dates"]["from"] == "2019-05-01" and data["dates"]["to"] == "2019-05-31"
    assert chips(data) == {("person", "Amma")}


def test_the_english_search_still_reads_the_same(home):
    _, data = found(home["family"], "Amma at the temple, 2019")
    assert chips(data) == {("person", "Amma")}
    assert data["dates"]["from"] == "2019-01-01"


def test_words_typed_as_words_are_left_alone(home):
    _, data = found(home["family"], "அம்மா", plain="1")
    assert data["understood"] is None
