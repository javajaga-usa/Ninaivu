"""Ask the family: "who is this?" links, their public page, and the review.

The link is the second thing in Ninaivu that answers to somebody with no
account at all, so most of what is tested here is what it must *not* do:
show a face from a photograph the asker could not open, show a photograph
only administrators may see, go on showing one hidden after the link was
made, name anybody the house already knows, or take answers without limit.
The rest is that an accepted answer names the face the way the People
screen's "Yes" does — confirmed, with the person's centroid rebuilt.
"""

import json
import time

import numpy as np
import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, login
from ninaivu.media.faceindex import FaceIndexer
from ninaivu.storage import ask_family, db


def _vector(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    vec = rng.normal(size=128).astype("float32")
    return (vec / np.linalg.norm(vec)).astype("float32").tobytes()


@pytest.fixture()
def faces(app, people, scanned):
    """A family photograph with an unnamed face and a face of Raman (named,
    confirmed), and an administrator-only photograph with an unnamed face."""
    cfg, conn, _ = scanned
    rows = conn.execute("SELECT id FROM assets WHERE root=? AND kind='picture' ORDER BY id",
                        (cfg.active_root,)).fetchall()
    family_id, hidden_id = int(rows[0]["id"]), int(rows[1]["id"])
    conn.execute("UPDATE assets SET visibility=1 WHERE id=?", (family_id,))
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden_id,))
    crops = cfg.state_dir / "faces"
    crops.mkdir(parents=True, exist_ok=True)
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(7, 'Raman', ?)",
                 (time.time(),))

    def face(asset_id, person=None, source="none", seed=1):
        thumb = f"crop{seed}.jpg"
        Image.new("RGB", (112, 112), (seed * 20, 90, 120)).save(crops / thumb)
        cur = conn.execute(
            "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, "
            "thumb, created_at) VALUES(?,?,?,?,?,0.8,?,?)",
            (asset_id, person, source, json.dumps([0, 0, 90, 90]), _vector(seed), thumb,
             time.time()))
        return int(cur.lastrowid)

    ids = {
        "unnamed": face(family_id, seed=1),
        "raman": face(family_id, person=7, source="confirmed", seed=2),
        "hidden": face(hidden_id, seed=3),
        "family_asset": family_id,
        "hidden_asset": hidden_id,
    }
    conn.commit()
    return ids


def _ask(client, face_ids, **extra):
    return client.post("/api/ask-family/questions", json={"face_ids": face_ids, **extra})


# -- making a link -------------------------------------------------------------

def test_a_family_member_can_ask_about_a_face_they_can_see(as_family, anon, faces):
    made = _ask(as_family, [faces["unnamed"]])
    assert made.status_code == 200, made.get_json()
    token = made.get_json()["token"]
    assert made.get_json()["url"] == f"/ask/{token}"
    page = anon.get(f"/ask/{token}")
    assert page.status_code == 200
    assert b"/static/js/ask.js" in page.data

    shown = anon.get(f"/api/ask/{token}")
    assert shown.status_code == 200
    body = shown.get_json()
    assert [f["n"] for f in body["faces"]] == [1]
    crop = anon.get(body["faces"][0]["face"])
    assert crop.status_code == 200 and crop.mimetype == "image/jpeg"


def test_the_page_names_nobody_and_numbers_the_faces(as_family, anon, faces):
    """No face id, no photograph id, no name the house uses — only numbers."""
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    raw = anon.get(f"/api/ask/{token}").get_data(as_text=True)
    assert "Raman" not in raw
    face = json.loads(raw)["faces"][0]
    assert set(face) == {"n", "face", "photo"}
    assert face["face"] == f"/api/ask/{token}/face/1"
    assert face["photo"] is None


def test_the_link_opens_without_signing_in_when_the_library_is_closed(app, as_family, faces):
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    app.config["MV_CONFIG"].open_browsing = False
    stranger = app.test_client()
    assert stranger.get("/api/assets").status_code == 401
    assert stranger.get(f"/ask/{token}").status_code == 200
    assert stranger.get(f"/api/ask/{token}").status_code == 200


def test_a_guest_cannot_ask(as_guest, faces):
    assert _ask(as_guest, [faces["unnamed"]]).status_code in (401, 403)


def test_an_admin_only_face_can_be_asked_only_by_an_admin(as_family, as_admin, anon, faces):
    assert _ask(as_family, [faces["hidden"]]).status_code == 404
    assert _ask(as_family, [faces["unnamed"], faces["hidden"]]).status_code == 404
    listed = as_family.get("/api/ask-family/faces").get_json()["faces"]
    assert faces["hidden"] not in [f["face_id"] for f in listed]
    assert faces["raman"] not in [f["face_id"] for f in listed]

    made = _ask(as_admin, [faces["hidden"]], show_photo=True)
    assert made.status_code == 200
    token = made.get_json()["token"]
    face = anon.get(f"/api/ask/{token}").get_json()["faces"][0]
    # The crop, yes; the photograph only administrators may see, never.
    assert face["photo"] is None
    assert anon.get(face["face"]).status_code == 200
    assert anon.get(f"/api/ask/{token}/photo/1").status_code == 404


def test_the_whole_photograph_only_when_asked_for(as_family, anon, faces):
    plain = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    assert anon.get(f"/api/ask/{plain}").get_json()["faces"][0]["photo"] is None
    assert anon.get(f"/api/ask/{plain}/photo/1").status_code == 404

    with_photo = _ask(as_family, [faces["unnamed"]], show_photo=True).get_json()["token"]
    face = anon.get(f"/api/ask/{with_photo}").get_json()["faces"][0]
    assert face["photo"] == f"/api/ask/{with_photo}/photo/1"
    photo = anon.get(face["photo"])
    assert photo.status_code == 200 and photo.mimetype.startswith("image/")


@pytest.mark.parametrize("body", [
    {"face_ids": []},
    {"face_ids": "1"},
    {"face_ids": [True]},
    {"face_ids": list(range(1, 22))},
    {"face_ids": [1], "show_photo": "yes"},
    {"face_ids": [1], "expires_in_days": "never"},
])
def test_a_bad_request_is_a_400(as_family, faces, body):
    assert as_family.post("/api/ask-family/questions", json=body).status_code == 400


def test_a_named_face_is_not_asked_about(as_admin, faces):
    assert _ask(as_admin, [faces["raman"]]).status_code == 409


def test_a_link_lasts_thirty_days_unless_said(as_family, faces, scanned):
    _, conn, _ = scanned
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    question = ask_family.get_question(conn, token)
    assert question["expires_at"] == pytest.approx(time.time() + 30 * 86400, abs=60)
    # Only an administrator may make one that never ends.
    forever = _ask(as_family, [faces["unnamed"]], expires_in_days=0).get_json()["token"]
    assert ask_family.get_question(conn, forever)["expires_at"] is not None


# -- expiry, withdrawal, and photographs hidden later ----------------------------

def test_an_expired_link_answers_410(as_family, anon, faces, scanned):
    _, conn, _ = scanned
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    conn.execute("UPDATE ask_questions SET expires_at=? WHERE token=?", (time.time() - 1, token))
    conn.commit()
    assert anon.get(f"/ask/{token}").status_code == 410
    assert anon.get(f"/api/ask/{token}").status_code == 410
    assert anon.get(f"/api/ask/{token}/face/1").status_code == 410
    answer = anon.post(f"/api/ask/{token}/answer", json={"answers": [{"n": 1, "name": "X"}]})
    assert answer.status_code == 410


def test_a_withdrawn_link_stops_working(app, as_family, as_admin, anon, faces):
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    # Somebody else in the family cannot withdraw it; its maker can.
    other = _ask(as_admin, [faces["unnamed"]]).get_json()["token"]
    assert as_family.delete(f"/api/ask-family/questions/{other}").status_code == 404
    assert as_family.delete(f"/api/ask-family/questions/{token}").status_code == 200
    assert anon.get(f"/api/ask/{token}").status_code == 410
    assert anon.get(f"/api/ask/{token}/face/1").status_code == 410
    listed = as_family.get("/api/ask-family/questions").get_json()["questions"]
    assert [q["revoked"] for q in listed if q["token"] == token] == [True]
    assert other not in [q["token"] for q in listed]
    # An administrator may withdraw anybody's.
    assert as_admin.delete(f"/api/ask-family/questions/{other}").status_code == 200


def test_a_photograph_hidden_after_asking_drops_out_of_the_link(as_family, as_admin, anon,
                                                                faces, scanned):
    _, conn, _ = scanned
    token = _ask(as_family, [faces["unnamed"]], show_photo=True).get_json()["token"]
    assert anon.get(f"/api/ask/{token}/face/1").status_code == 200
    # Straight in the index, so the face is still there: it is the check at
    # view time that withdraws it, not the face being forgotten.
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (faces["family_asset"],))
    conn.commit()
    assert db.get_face(conn, faces["unnamed"]) is not None
    assert anon.get(f"/api/ask/{token}").status_code == 404
    assert anon.get(f"/api/ask/{token}/face/1").status_code == 404
    assert anon.get(f"/api/ask/{token}/photo/1").status_code == 404
    answer = anon.post(f"/api/ask/{token}/answer", json={"answers": [{"n": 1, "name": "X"}]})
    assert answer.status_code == 404


def test_a_trashed_or_flagged_photograph_drops_out_too(as_admin, anon, faces, scanned):
    _, conn, _ = scanned
    token = _ask(as_admin, [faces["unnamed"]]).get_json()["token"]
    conn.execute("UPDATE assets SET nsfw=1 WHERE id=?", (faces["family_asset"],))
    conn.commit()
    assert anon.get(f"/api/ask/{token}").status_code == 404
    conn.execute("UPDATE assets SET nsfw=0, trashed=1 WHERE id=?", (faces["family_asset"],))
    conn.commit()
    assert anon.get(f"/api/ask/{token}").status_code == 404


def test_a_disabled_askers_link_shows_nothing(as_family, anon, faces, people):
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    people["conn"].execute("UPDATE users SET active=0 WHERE id=?", (people["family"].id,))
    people["conn"].commit()
    assert anon.get(f"/api/ask/{token}").status_code == 404


def test_a_deleted_askers_link_stays_dead_when_the_id_is_reused(as_family, anon, faces,
                                                               people, scanned):
    from ninaivu.server import auth
    _, conn, _ = scanned
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    auth.delete_user(conn, people["family"].id)
    assert ask_family.get_question(conn, token)["created_by"] is None
    assert anon.get(f"/api/ask/{token}").status_code == 404


def test_a_photograph_can_be_deleted_from_an_index_with_no_profiles(tmp_path):
    """The index is opened by tools that never make the users table; a key
    from these tables to it made every delete of a photograph fail there."""
    conn = db.init_db(tmp_path / "index.db")
    conn.execute("INSERT INTO assets(root, rel_path, filename, kind) "
                 "VALUES('r', 'a.jpg', 'a.jpg', 'picture')")
    conn.execute("DELETE FROM assets")
    conn.commit()


# -- answering -------------------------------------------------------------------

def _answer(client, token, *answers, who=""):
    return client.post(f"/api/ask/{token}/answer",
                       json={"answers": list(answers), "who": who})


def test_answers_are_capped_and_checked(as_family, anon, faces):
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    too_long = "x" * (ask_family.NAME_MAX + 1)
    assert _answer(anon, token, {"n": 1, "name": too_long}).status_code == 400
    assert _answer(anon, token, {"n": 1, "name": "Raman",
                                 "note": "y" * (ask_family.NOTE_MAX + 1)}).status_code == 400
    assert _answer(anon, token, {"n": 1, "name": "Raman"},
                   who="z" * (ask_family.WHO_MAX + 1)).status_code == 400
    assert _answer(anon, token, {"n": 2, "name": "Raman"}).status_code == 400
    assert _answer(anon, token, {"n": 1, "name": "   "}).status_code == 400
    assert _answer(anon, token, {"n": True, "name": "Raman"}).status_code == 400
    assert anon.post(f"/api/ask/{token}/answer", json=[1]).status_code == 400
    ok = _answer(anon, token, {"n": 1, "name": " Raman\u0007 ", "note": "my uncle,\n at Chennai"},
                 who="Paati")
    assert ok.status_code == 200, ok.get_json()
    pending = as_family.get("/api/ask-family/answers").get_json()["answers"]
    assert [(a["name"], a["note"], a["answered_by"]) for a in pending] == [
        ("Raman", "my uncle, at Chennai", "Paati")]


def test_answers_are_rate_limited_per_link_and_address(as_family, anon, faces):
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    allowed = 0
    for _ in range(30):
        reply = _answer(anon, token, {"n": 1, "name": "Raman"})
        if reply.status_code == 429:
            break
        assert reply.status_code == 200
        allowed += 1
    assert allowed == 10
    # A different link from the same address still has its own allowance.
    other = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    assert _answer(anon, other, {"n": 1, "name": "Raman"}).status_code == 200


def test_a_link_holds_a_bounded_number_of_answers(as_family, anon, faces, scanned, monkeypatch):
    _, conn, _ = scanned
    monkeypatch.setattr(ask_family, "MAX_ANSWERS", 2)
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    assert _answer(anon, token, {"n": 1, "name": "A"}).status_code == 200
    assert _answer(anon, token, {"n": 1, "name": "B"}).status_code == 200
    assert _answer(anon, token, {"n": 1, "name": "C"}).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM ask_answers").fetchone()[0] == 2


# -- reviewing -------------------------------------------------------------------

def _pending(client):
    return client.get("/api/ask-family/answers").get_json()["answers"]


def test_accepting_names_the_face_through_the_confirm_path(as_family, anon, faces, scanned,
                                                           monkeypatch):
    _, conn, _ = scanned
    calls = []
    original = FaceIndexer.confirm

    def spy(self, conn_, face_id, person_id):
        calls.append((face_id, person_id))
        return original(self, conn_, face_id, person_id)

    monkeypatch.setattr(FaceIndexer, "confirm", spy)
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    _answer(anon, token, {"n": 1, "name": "raman", "note": "my uncle"}, who="Paati")
    answer = _pending(as_family)[0]
    # An existing person with that name, ignoring case, is offered.
    assert [m["name"] for m in answer["matches"]] == ["Raman"]
    before = conn.execute("SELECT centroid, confirmed_count FROM people_clusters WHERE id=7"
                          ).fetchone()

    accepted = as_family.post(f"/api/ask-family/answers/{answer['id']}/accept",
                              json={"person_id": 7})
    assert accepted.status_code == 200, accepted.get_json()
    assert calls == [(faces["unnamed"], 7)]
    face = db.get_face(conn, faces["unnamed"])
    assert (face["person_id"], face["source"]) == (7, "confirmed")
    after = conn.execute("SELECT centroid, confirmed_count FROM people_clusters WHERE id=7"
                         ).fetchone()
    assert after["confirmed_count"] == 2
    assert after["centroid"] is not None and after["centroid"] != before["centroid"]
    assert _pending(as_family) == []
    # Answered once: a second accept or reject is refused.
    assert as_family.post(f"/api/ask-family/answers/{answer['id']}/reject",
                          json={}).status_code == 409


def test_accepting_a_new_name_makes_a_person(as_family, anon, faces, scanned):
    _, conn, _ = scanned
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    _answer(anon, token, {"n": 1, "name": "Kamala"})
    _answer(anon, token, {"n": 1, "name": "Kamala Patti"})
    first, second = sorted(_pending(as_family), key=lambda a: a["name"])
    assert first["matches"] == []
    reply = as_family.post(f"/api/ask-family/answers/{first['id']}/accept",
                           json={"name": "Kamala"})
    assert reply.status_code == 200
    person = reply.get_json()["person"]
    assert person["name"] == "Kamala"
    face = db.get_face(conn, faces["unnamed"])
    assert (face["person_id"], face["source"]) == (person["id"], "confirmed")
    assert db.get_person_cluster(conn, person["id"])["confirmed_count"] == 1
    # The other answer about the same face is closed, not left waiting.
    assert _pending(as_family) == []
    status = conn.execute("SELECT status FROM ask_answers WHERE id=?",
                          (second["id"],)).fetchone()[0]
    assert status == "closed"
    names = [p["name"] for p in as_family.get("/api/faces/people").get_json()["people"]]
    assert "Kamala" in names


def test_rejecting_dismisses_and_changes_nothing(as_family, anon, faces, scanned):
    _, conn, _ = scanned
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    _answer(anon, token, {"n": 1, "name": "Nobody"})
    answer = _pending(as_family)[0]
    assert as_family.post(f"/api/ask-family/answers/{answer['id']}/reject",
                          json={}).status_code == 200
    assert _pending(as_family) == []
    assert db.get_face(conn, faces["unnamed"])["person_id"] is None
    assert conn.execute("SELECT status FROM ask_answers").fetchone()[0] == "rejected"


def test_only_the_asker_or_an_admin_reviews_the_answers(app, as_admin, anon, faces, people):
    from ninaivu.server import auth
    other = auth.create_user(people["conn"], "arjun", "riverbank55", display_name="Arjun",
                             role=auth.ROLE_FAMILY, created_by=people["admin"].id)
    assert other
    token = _ask(as_admin, [faces["unnamed"]]).get_json()["token"]
    _answer(anon, token, {"n": 1, "name": "Raman"})
    answer = _pending(as_admin)[0]
    family = login(app.test_client(), *FAMILY)
    assert _pending(family) == []
    assert family.post(f"/api/ask-family/answers/{answer['id']}/accept",
                       json={"person_id": 7}).status_code == 404
    assert family.post(f"/api/ask-family/answers/{answer['id']}/reject",
                       json={}).status_code == 404
    assert login(app.test_client(), *ADMIN).post(
        f"/api/ask-family/answers/{answer['id']}/reject", json={}).status_code == 200


def test_a_family_member_cannot_attach_a_face_to_somebody_they_cannot_see(
        as_family, anon, faces, scanned):
    _, conn, _ = scanned
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(8, 'Ghost', ?)",
                 (time.time(),))
    conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, "
                 "created_at) VALUES(?,8,'confirmed','[0,0,9,9]',?,0.8,?)",
                 (faces["hidden_asset"], _vector(9), time.time()))
    conn.commit()
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    _answer(anon, token, {"n": 1, "name": "Ghost"})
    answer = _pending(as_family)[0]
    # Not offered — the name would say there is a Ghost somewhere — and refused.
    assert answer["matches"] == []
    assert as_family.post(f"/api/ask-family/answers/{answer['id']}/accept",
                          json={"person_id": 8}).status_code == 404
    assert db.get_face(conn, faces["unnamed"])["person_id"] is None


def test_answers_about_a_photograph_since_hidden_wait_for_an_admin(as_family, anon, faces,
                                                                   scanned):
    """Hidden behind Ninaivu's back (the faces stay): the family member who
    asked no longer sees the answer, since they cannot open the photograph."""
    _, conn, _ = scanned
    token = _ask(as_family, [faces["unnamed"]]).get_json()["token"]
    assert _answer(anon, token, {"n": 1, "name": "Raman"}).status_code == 200
    answer = _pending(as_family)[0]
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (faces["family_asset"],))
    conn.commit()
    assert _pending(as_family) == []
    assert as_family.post(f"/api/ask-family/answers/{answer['id']}/accept",
                          json={"person_id": 7}).status_code == 404


def test_hiding_a_photograph_forgets_its_unnamed_faces_and_their_answers(as_admin, anon,
                                                                         faces, scanned):
    """Hiding takes back what the AI made of a photograph, unconfirmed faces
    included (db.forget_ai_reading); questions and answers about them go too."""
    _, conn, _ = scanned
    token = _ask(as_admin, [faces["unnamed"]]).get_json()["token"]
    assert _answer(anon, token, {"n": 1, "name": "Raman"}).status_code == 200
    db.set_visibility(conn, [faces["family_asset"]], 2)
    assert _pending(as_admin) == []
    assert conn.execute("SELECT COUNT(*) FROM ask_faces").fetchone()[0] == 0
    assert anon.get(f"/api/ask/{token}").status_code == 404


def test_a_public_photograph_made_family_only_drops_out_of_the_link(as_family, anon, faces,
                                                                    scanned):
    """Checked at every view against the visibility when it was asked."""
    _, conn, _ = scanned
    db.set_visibility(conn, [faces["family_asset"]], 0)
    token = _ask(as_family, [faces["unnamed"]], show_photo=True).get_json()["token"]
    assert anon.get(f"/api/ask/{token}").status_code == 200
    db.set_visibility(conn, [faces["family_asset"]], 1)
    assert anon.get(f"/api/ask/{token}").status_code == 404
    assert anon.get(f"/api/ask/{token}/photo/1").status_code == 404


def test_the_family_app_routes_the_feature(scanned):
    """Asking is for the family app — and the public page has to be on it,
    since the console never answers anybody without an account."""
    from ninaivu import build_services, create_home_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    rules = {str(r) for r in create_home_app(services).url_map.iter_rules()}
    for path in ("/ask/<token>", "/api/ask/<token>", "/api/ask/<token>/answer",
                 "/api/ask-family/questions", "/api/ask-family/answers"):
        assert path in rules
