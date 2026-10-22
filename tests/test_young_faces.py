"""Young faces, older names: who an unnamed child in an old photograph may be.

The matching on vectors alone: a person is held to their youngest named
faces; the further back a photograph, the more allowance, never below the
recogniser's same-person line; nobody before they were born, nobody already
named in the photograph, nobody somebody said no to; a parent or brother
named beside them is a reason; sisters both offered. Then the family app's
routes: off until switched on, family only, read through the viewer's own
limits (hidden photographs for administrators only), and yes and no.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from ninaivu.media import facematch, young_faces as guess
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db, family_tree, then_and_now as born_store

DIM = 128
SAME = facematch.SFACE.same


def _basis(n: int) -> np.ndarray:
    v = np.zeros(DIM, dtype="float32")
    v[n] = 1.0
    return v


def _like(n: int, cos: float, other: int = 100) -> np.ndarray:
    """A unit vector *cos* away from basis vector *n*."""
    return (cos * _basis(n) + math.sqrt(1 - cos * cos) * _basis(other)).astype("float32")


def _person(pid, born=None, young=0, adult=1):
    """Named as a child (vector *young*) in 1975-1978 and as an adult (vector
    *adult*) from 1990."""
    years = [1975, 1976, 1978, 1990, 1995, 2000, 2010]
    vectors = [_basis(young)] * 3 + [_basis(adult)] * 4
    return guess.Known(pid, vectors, years, list(range(pid * 100, pid * 100 + 7)), born)


def _one(face_id=1, asset_id=10, cos=0.45, year=1955, n=0):
    return guess.Unnamed(face_id, asset_id, _like(n, cos), year, 0.9)


# --- the matching ----------------------------------------------------------------------------------

def test_a_person_is_held_to_their_youngest_named_faces():
    (ref,) = guess.references([_person(7)])
    assert ref.earliest == 1975 and ref.young == 3 and ref.face_id == 700
    assert float(ref.centroid @ _basis(0)) == pytest.approx(1.0)
    # Too few dated faces: nobody to compare with.
    few = guess.Known(8, [_basis(0)] * 5, [0, 0, 0, 1990, 1991], [1, 2, 3, 4, 5])
    assert guess.references([few]) == []


def test_the_ordinary_matcher_misses_her_and_this_does_not():
    """Her average face is the woman, so a girl like her young self is far
    from it; held to her young faces she is found."""
    known = _person(7)
    centre = facematch.centroid(known.vectors)
    girl = _like(0, 0.45)
    assert float(centre @ girl) < SAME
    got = guess.suggest([_one(cos=0.45, year=1955)], guess.references([known]))
    assert [(s["face_id"], s["person_id"], s["year"], s["gap"]) for s in got] == [(1, 7, 1955, 20)]


def test_the_further_back_the_more_allowance_never_below_the_line():
    refs = guess.references([_person(7)])
    just_above = SAME + 0.01
    # Twenty years back needs only the same-person line.
    assert guess.suggest([_one(cos=just_above, year=1955)], refs)
    # Five years back needs more than that.
    assert guess.suggest([_one(cos=just_above, year=1970)], refs) == []
    assert guess.suggest([_one(cos=SAME + 0.08, year=1970)], refs)
    # However far back, nothing under the line.
    assert guess.suggest([_one(cos=SAME - 0.01, year=1900)], refs) == []
    # Within MIN_GAP years of the earliest named photograph: the review queue's.
    assert guess.suggest([_one(cos=0.9, year=1974)], refs) == []


def test_an_undated_print_gets_some_allowance():
    refs = guess.references([_person(7)])
    floor = SAME + guess.BASE - guess.UNDATED_ALLOWANCE
    assert guess.suggest([_one(cos=floor + 0.01, year=0)], refs)[0]["gap"] == 0
    assert guess.suggest([_one(cos=floor - 0.01, year=0)], refs) == []


def test_nobody_before_they_were_born_and_ages_are_told():
    refs = guess.references([_person(7, born=1950)])
    assert guess.suggest([_one(cos=0.6, year=1948)], refs) == []
    (got,) = guess.suggest([_one(cos=0.6, year=1958)], refs)
    assert got["age"] == 8


def test_not_somebody_already_named_there_nor_somebody_said_no_to():
    refs = guess.references([_person(7)])
    face = _one(cos=0.6)
    assert guess.suggest([face], refs, present={10: {7}}) == []
    assert guess.suggest([face], refs, blocked={(1, 7)}) == []
    assert guess.suggest([face], refs, blocked={(1, 8)})


def test_a_parent_named_beside_them_is_a_reason_and_an_allowance():
    refs = guess.references([_person(7)])
    floor = SAME + guess.BASE - guess.PER_YEAR * 6         # 1969: six years back
    face = _one(cos=floor - 0.01, year=1969)
    assert guess.suggest([face], refs, present={10: {3}}) == []

    def relation(pid, other):
        return "parent" if (pid, other) == (7, 3) else ""
    (got,) = guess.suggest([face], refs, present={10: {3}}, relation=relation)
    assert got["family"] == [{"id": 3, "relation": "parent"}]

    def child(pid, other):
        return "child"
    assert guess.suggest([face], refs, present={10: {3}}, relation=child) == []


def test_sisters_are_both_offered():
    a = _person(7, young=0)
    b = _person(8, young=2, adult=3)
    near_both = (0.5 * _basis(0) + 0.48 * _basis(2) + 0.72 * _basis(100)).astype("float32")
    face = guess.Unnamed(1, 10, near_both, 1955, 0.9)
    (got,) = guess.suggest([face], guess.references([a, b]))
    assert got["person_id"] == 7 and got["also"] == 8
    # Clearly one of them: no second name.
    (got,) = guess.suggest([_one(cos=0.6)], guess.references([a, b]))
    assert got["also"] is None


def test_one_face_per_photograph_and_person():
    refs = guess.references([_person(7)])
    faces = [_one(face_id=1, cos=0.5), _one(face_id=2, cos=0.6), _one(face_id=3, asset_id=11, cos=0.55)]
    got = guess.suggest(faces, refs)
    assert sorted(s["face_id"] for s in got) == [2, 3]


def test_near_keeps_only_faces_that_could_be_anybody():
    refs = guess.references([_person(7)])
    vectors = np.vstack([_like(0, 0.5), _like(0, 0.1)])
    assert list(guess.near(vectors, refs)) == [True, False]


def test_only_believable_dates_are_years():
    assert guess.year_of("1962-04-01", "exif") == 1962
    assert guess.year_of("1962", "manual") == 1962
    assert guess.year_of("2015-04-01", "mtime") == 0
    assert guess.year_of("", "manual") == 0


# --- the family app --------------------------------------------------------------------------------

def _pack(vector) -> bytes:
    return np.asarray(vector, dtype="float32").tobytes()


@pytest.fixture()
def album(app, people):
    """Paati named young in 1975-1978 and as a woman later; an unnamed girl
    like her young self in a 1955 photograph with her mother Lakshmi named
    beside her, and another in a hidden one."""
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.young_faces = True
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 ORDER BY id")]
    assert len(ids) >= 9
    years = ["1975", "1976", "1978", "1990", "1995", "2000", "2010", "1955", "1956"]
    with db._write_lock:                                              # noqa: SLF001
        for aid, year in zip(ids, years):
            conn.execute("UPDATE assets SET date_key=?, date_source='exif', visibility=1, nsfw=0 "
                         "WHERE id=?", (f"{year}-05-01", aid))
        paati = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Paati', 0)").lastrowid
        lakshmi = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Lakshmi', 0)").lastrowid
        for n, aid in enumerate(ids[:7]):
            vector = _basis(0) if n < 3 else _basis(1)
            conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, thumb) "
                         "VALUES (?, ?, 'confirmed', '[0,0,10,10]', ?, 0.9, 'p.jpg')",
                         (aid, paati, _pack(vector)))
        girl = conn.execute("INSERT INTO faces(asset_id, bbox, embedding, quality, thumb) "
                            "VALUES (?, '[0,0,10,10]', ?, 0.9, 'g.jpg')",
                            (ids[7], _pack(_like(0, 0.5)))).lastrowid
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, thumb) "
                     "VALUES (?, ?, 'confirmed', '[30,0,10,10]', ?, 0.9, 'l.jpg')",
                     (ids[7], lakshmi, _pack(_basis(5))))
        hidden = conn.execute("INSERT INTO faces(asset_id, bbox, embedding, quality, thumb) "
                              "VALUES (?, '[0,0,10,10]', ?, 0.9, 'h.jpg')",
                              (ids[8], _pack(_like(0, 0.5)))).lastrowid
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (ids[8],))
        conn.commit()
    db._faces_changed()                                               # noqa: SLF001
    family_tree.add(conn, lakshmi, paati, "parent", None)
    return {"conn": conn, "ids": ids, "paati": paati, "lakshmi": lakshmi, "girl": girl,
            "hidden": hidden, "cfg": cfg}


def test_off_by_default_and_under_advanced():
    assert Config().young_faces is False
    assert "young_faces" in GROUPS["Advanced"]


def test_every_route_is_404_until_switched_on(as_family):
    assert as_family.get("/api/young-faces").status_code == 404
    assert as_family.post("/api/young-faces/1/yes", json={"person_id": 1}).status_code == 404
    assert as_family.post("/api/young-faces/1/no", json={"person_id": 1}).status_code == 404
    assert as_family.put("/api/young-faces/born/1", json={"year": 1950}).status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["young_faces"] is False


def test_not_for_guests(album, as_guest):
    assert as_guest.get("/api/young-faces").status_code in (403, 404)


def test_the_family_sees_the_girl_and_why(album, as_family):
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["young_faces"] is True
    got = as_family.get("/api/young-faces").get_json()
    (group,) = got["people"]
    assert group["person"]["name"] == "Paati" and group["person"]["earliest"] == 1975
    (item,) = group["faces"]
    assert item["face_id"] == album["girl"], "the hidden photograph is not the family's"
    assert item["year"] == 1955 and item["gap"] == 20
    assert item["photo"]["id"] == album["ids"][7]
    assert item["family"] == [{"id": album["lakshmi"], "relation": "parent", "name": "Lakshmi"}]
    assert [p["name"] for p in got["ask_born"]] == ["Paati"]


def test_an_administrator_sees_everything(album, as_admin):
    group = as_admin.get("/api/young-faces").get_json()["people"][0]
    assert {f["face_id"] for f in group["faces"]} == {album["girl"], album["hidden"]}


def test_yes_names_her(album, as_family):
    answer = as_family.post(f"/api/young-faces/{album['girl']}/yes", json={"person_id": album["paati"]})
    assert answer.status_code == 200
    face = db.get_face(album["conn"], album["girl"])
    assert face["person_id"] == album["paati"] and face["source"] == "confirmed"
    again = as_family.post(f"/api/young-faces/{album['girl']}/yes", json={"person_id": album["paati"]})
    assert again.status_code == 409
    assert as_family.get("/api/young-faces").get_json()["people"] == []


def test_no_is_kept(album, as_family):
    answer = as_family.post(f"/api/young-faces/{album['girl']}/no", json={"person_id": album["paati"]})
    assert answer.status_code == 200
    assert (album["girl"], album["paati"]) in db.rejections(album["conn"])
    assert db.get_face(album["conn"], album["girl"])["person_id"] is None
    assert as_family.get("/api/young-faces").get_json()["people"] == []


def test_a_family_member_cannot_answer_for_a_hidden_photograph(album, as_family):
    answer = as_family.post(f"/api/young-faces/{album['hidden']}/yes", json={"person_id": album["paati"]})
    assert answer.status_code == 404
    assert db.get_face(album["conn"], album["hidden"])["person_id"] is None
    bad = as_family.post(f"/api/young-faces/{album['girl']}/yes", json={"person_id": "Paati"})
    assert bad.status_code == 400
    nobody = as_family.post(f"/api/young-faces/{album['girl']}/yes", json={"person_id": 999999})
    assert nobody.status_code == 404


def test_born_rules_out_older_photographs(album, as_family):
    assert as_family.put(f"/api/young-faces/born/{album['paati']}", json={"year": 1960}).status_code == 200
    assert born_store.born(album["conn"])[album["paati"]] == 1960
    assert as_family.get("/api/young-faces").get_json()["people"] == []
    assert as_family.put(f"/api/young-faces/born/{album['paati']}", json={"year": 1950}).status_code == 200
    (item,) = as_family.get("/api/young-faces").get_json()["people"][0]["faces"]
    assert item["age"] == 5
    assert as_family.put(f"/api/young-faces/born/{album['paati']}", json={"year": "x"}).status_code == 400
