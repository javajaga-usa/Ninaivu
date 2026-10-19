"""The living photo frame and time capsules: off until switched on, and never
a way to see more than the gallery would show.

The frame's batch goes through the viewer's own limits; a capsule's letter
and photographs are not sent to anybody before the day it opens, not even to
whoever sealed it, and when it opens only what the reader may see is in it.
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import pytest

from conftest import login
from ninaivu.media import frame
from ninaivu.server import auth
from ninaivu.storage import capsules, db

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 400


@pytest.fixture()
def switched_on(app, people):
    # The fixtures are flat colour, which the frame would call blurred.
    people["conn"].execute("UPDATE assets SET sharpness=200, brightness=0.5")
    people["conn"].commit()
    cfg = app.config["MV_CONFIG"]
    cfg.photo_frame = True
    cfg.time_capsules = True
    return cfg


def all_ids(client):
    return [i["id"] for i in client.get("/api/assets?limit=200").get_json()["items"]]


# --- the switches ----------------------------------------------------------

def test_both_are_off_until_switched_on(as_family, app):
    assert as_family.get("/api/frame/next").status_code == 404
    assert as_family.get("/api/capsules").status_code == 404
    first = all_ids(as_family)[0]
    assert as_family.get(f"/api/frame/picture/{first}").status_code == 404
    moments = as_family.get("/api/status?stats=0").get_json()["moments"]
    assert moments == {"frame": False, "capsules": False, "ask": False}


def test_the_status_says_which_are_on(as_family, switched_on):
    assert as_family.get("/api/status?stats=0").get_json()["moments"] == {
        "frame": True, "capsules": True, "ask": False}


# --- the frame ---------------------------------------------------------------

def test_the_frame_gives_a_batch_of_pictures_with_what_to_say(as_family, switched_on, people):
    conn = people["conn"]
    answer = as_family.get("/api/frame/next?n=6&seed=7")
    assert answer.status_code == 200
    body = answer.get_json()
    items = body["items"]
    assert 1 <= len(items) <= 6
    assert len({i["id"] for i in items}) == len(items), "no photograph twice in a batch"
    for item in items:
        row = db.get_asset(conn, item["id"])
        assert row["kind"] == "picture"
        assert item["src"] == f"/api/frame/picture/{item['id']}"
        assert {"years_ago", "place", "people", "story", "focus", "on_this_day"} <= set(item)
    picture = as_family.get(items[0]["src"])
    assert picture.status_code == 200
    assert picture.mimetype == "image/jpeg"
    assert b"Exif" not in picture.data[:4096], "the viewing copy carries no metadata"


def test_the_frame_never_shows_what_the_viewer_may_not_see(as_family, switched_on, people):
    conn = people["conn"]
    ids = all_ids(as_family)
    hidden = ids[:3]
    conn.executemany("UPDATE assets SET visibility=? WHERE id=?",
                     [(auth.VIS_HIDDEN, i) for i in hidden])
    conn.commit()
    for seed in range(1, 6):
        shown = {i["id"] for i in as_family.get(f"/api/frame/next?n=40&seed={seed}").get_json()["items"]}
        assert not shown & set(hidden)
    assert as_family.get(f"/api/frame/picture/{hidden[0]}").status_code == 404


def test_on_this_day_and_stories_come_first(as_family, switched_on, people):
    conn = people["conn"]
    ids = all_ids(as_family)
    today = date.today()
    # One photograph from this day four years ago, and one with a story.
    then = f"{today.year - 4}-{today.month:02d}-{today.day:02d}"
    conn.execute("UPDATE assets SET date_key=? WHERE id=?", (then, ids[0]))
    conn.commit()
    told = as_family.post(f"/api/asset/{ids[1]}/stories",
                          data={"audio": (io.BytesIO(WEBM), "s.webm", "audio/webm"),
                                "text": "Appa's first scooter, outside the old house"},
                          content_type="multipart/form-data")
    assert told.status_code == 201
    items = {i["id"]: i for i in as_family.get("/api/frame/next?n=4&seed=3").get_json()["items"]}
    assert ids[0] in items and items[ids[0]]["on_this_day"]
    assert items[ids[0]]["years_ago"] == 4
    assert ids[1] in items
    assert items[ids[1]]["story"]["text"].startswith("Appa's first scooter")
    assert items[ids[1]]["story"]["speaker"] == "Maya"


def test_screenshots_and_blurred_frames_are_left_out(people, scanned):
    _, conn, _ = scanned
    viewer = {"viewer_id": people["family"].id, "max_visibility": 1, "scope": None}
    roots = [scanned[0].active_root]
    conn.execute("UPDATE assets SET sharpness=200, brightness=0.5")
    rows = conn.execute("SELECT id FROM assets WHERE kind='picture'").fetchall()
    ids = [r[0] for r in rows]
    conn.execute(f"UPDATE assets SET quality='[\"screenshot\"]' WHERE id IN ({','.join('?' * len(ids[:-1]))})",
                 ids[:-1])
    conn.commit()
    batch = frame.batch(conn, roots, viewer, count=10, seed=5)
    assert [i["id"] for i in batch["items"]] == [ids[-1]]


def test_the_focus_is_the_middle_of_the_faces():
    item = {"width": 1000, "height": 500, "rotation": 0}
    faces = [{"bbox": json.dumps([100, 100, 100, 100])}, {"bbox": json.dumps([700, 100, 100, 100])}]
    assert frame._focus(item, faces) == [0.45, 0.3]           # noqa: SLF001
    assert frame._focus({**item, "rotation": 90}, faces) is None   # noqa: SLF001
    assert frame._focus(item, []) is None                    # noqa: SLF001


# --- time capsules ---------------------------------------------------------

def seal(client, ids, *, days=30, reader=None, title="For Arun", letter="Dear Arun"):
    opens = (date.today() + timedelta(days=days)).isoformat()
    return client.post("/api/capsules", json={
        "title": title, "letter": letter, "opens_on": opens, "for": reader, "ids": ids})


def open_now(conn, capsule_id):
    """Move the day it opens to today, as if the time had passed."""
    conn.execute("UPDATE capsules SET opens_on=? WHERE id=?", (date.today().isoformat(), capsule_id))
    conn.commit()


def test_a_sealed_capsule_shows_nothing_inside_to_anybody(as_family, as_admin, switched_on, people):
    ids = all_ids(as_family)[:3]
    made = seal(as_family, ids)
    assert made.status_code == 201, made.get_json()
    capsule = made.get_json()["capsule"]
    assert capsule["sealed"] and capsule["items"] == 3 and capsule["mine"]
    for client in (as_family, as_admin):
        answer = client.get(f"/api/capsules/{capsule['id']}")
        assert answer.status_code == 423
        body = answer.get_json()
        assert "letter" not in body and "ids" not in body
        assert "Dear Arun" not in answer.get_data(as_text=True)
    listed = as_admin.get("/api/capsules").get_json()["capsules"]
    assert [c["title"] for c in listed] == ["For Arun"]
    assert "Dear Arun" not in json.dumps(listed)


def test_on_the_day_it_opens_with_the_letter_and_photographs(as_family, as_admin, switched_on, people):
    ids = all_ids(as_family)[:3]
    capsule_id = seal(as_family, ids, reader=people["admin"].id).get_json()["capsule"]["id"]
    open_now(people["conn"], capsule_id)
    waiting = as_admin.get("/api/capsules").get_json()["capsules"][0]
    assert waiting["waiting"] and waiting["for_me"]
    opened = as_admin.get(f"/api/capsules/{capsule_id}").get_json()
    assert opened["letter"] == "Dear Arun"
    assert opened["ids"] == ids and opened["not_shown"] == 0
    assert not as_admin.get("/api/capsules").get_json()["capsules"][0]["waiting"]


def test_a_capsule_for_one_person_is_not_shown_to_another(app, as_family, switched_on, people):
    conn = people["conn"]
    other = auth.create_user(conn, "ravi", "longpassword9", display_name="Ravi",
                             role=auth.ROLE_FAMILY, created_by=people["admin"].id)
    capsule_id = seal(as_family, all_ids(as_family)[:1], reader=other.id).get_json()["capsule"]["id"]
    open_now(conn, capsule_id)
    as_ravi = login(app.test_client(), "ravi", "longpassword9")
    assert as_ravi.get(f"/api/capsules/{capsule_id}").status_code == 200
    # The admin is neither who it is for nor who sealed it.
    as_admin = login(app.test_client(), "dad", "correcthorse1")
    assert as_admin.get(f"/api/capsules/{capsule_id}").status_code == 404
    assert as_admin.get("/api/capsules").get_json()["capsules"] == []


def test_only_what_the_reader_may_see_is_in_it_when_it_opens(app, as_admin, switched_on, people):
    conn = people["conn"]
    ids = all_ids(as_admin)[:3]
    capsule_id = seal(as_admin, ids).get_json()["capsule"]["id"]
    conn.execute("UPDATE assets SET visibility=? WHERE id=?", (auth.VIS_HIDDEN, ids[0]))
    conn.commit()
    open_now(conn, capsule_id)
    as_family = login(app.test_client(), "maya", "summerdays24")
    opened = as_family.get(f"/api/capsules/{capsule_id}").get_json()
    assert opened["ids"] == ids[1:] and opened["not_shown"] == 1


def test_a_photograph_the_sealer_may_not_see_cannot_be_sealed(as_family, switched_on, people):
    conn = people["conn"]
    ids = all_ids(as_family)[:2]
    hidden = conn.execute("SELECT id FROM assets WHERE id NOT IN (?, ?) LIMIT 1", ids).fetchone()[0]
    conn.execute("UPDATE assets SET visibility=? WHERE id=?", (auth.VIS_HIDDEN, hidden))
    conn.commit()
    capsule = seal(as_family, [*ids, hidden]).get_json()["capsule"]
    assert capsule["items"] == 2
    assert capsules.items_of(conn, capsule["id"]) == ids


@pytest.mark.parametrize("body, error", [
    ({"title": "", "letter": "x", "opens_on": "2040-01-01"}, "Give the time capsule a name."),
    ({"title": "x", "letter": "x", "opens_on": date.today().isoformat()},
     "A time capsule opens on a day after today."),
    ({"title": "x", "letter": "x", "opens_on": "2199-01-01"}, "A time capsule opens within sixty years."),
    ({"title": "x", "letter": "x", "opens_on": "soon"}, "Choose the day it opens."),
    ({"title": "x", "letter": "", "opens_on": "2040-01-01", "ids": []}, "Put a photograph or a letter in it."),
])
def test_what_a_capsule_needs(as_family, switched_on, body, error):
    answer = as_family.post("/api/capsules", json=body)
    assert answer.status_code == 400
    assert answer.get_json()["error"] == error


def test_a_capsule_is_for_family_not_a_guest(as_family, as_guest, switched_on, people):
    answer = seal(as_family, [], letter="hello", reader=people["guest"].id)
    assert answer.status_code == 400
    assert as_guest.get("/api/capsules").status_code in (401, 403)


def test_only_whoever_sealed_it_or_an_admin_may_take_it_back(app, as_admin, switched_on, people):
    conn = people["conn"]
    capsule_id = seal(as_admin, [], letter="hello").get_json()["capsule"]["id"]
    as_family = login(app.test_client(), "maya", "summerdays24")
    assert as_family.delete(f"/api/capsules/{capsule_id}").status_code == 403
    assert as_admin.delete(f"/api/capsules/{capsule_id}").status_code == 200
    assert capsules.get(conn, capsule_id) is None


def test_a_deleted_photograph_leaves_the_capsule(as_family, switched_on, people):
    conn = people["conn"]
    ids = all_ids(as_family)[:2]
    capsule_id = seal(as_family, ids).get_json()["capsule"]["id"]
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM assets WHERE id=?", (ids[0],))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    assert capsules.items_of(conn, capsule_id) == ids[1:]
