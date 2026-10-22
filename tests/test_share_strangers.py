"""Share without strangers: faces nobody has named are blurred on share links.

Off until switched on. With it on, every picture a link serves (the file, the
browser copy and the tiles) is a copy with the faces nobody named blurred,
the stored ones and the ones found again now at a lower bar; a named face
stays clear; a photograph that cannot be looked at goes out blurred all
over; naming somebody makes a new copy with them clear. Videos are untouched.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from ninaivu.api import api_share
from ninaivu.media import share_faces
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db

#: Where the test photograph has its two faces (x, y, w, h).
NAMED = (40, 60, 80, 80)
STRANGER = (260, 60, 80, 80)


def _busy(size=(400, 300)) -> Image.Image:
    """A picture with fine detail everywhere, so a blur shows."""
    image = Image.new("RGB", size)
    image.putdata([((x * 37) % 256, (y * 53) % 256, ((x ^ y) * 7) % 256)
                   if (x // 4 + y // 4) % 2 else (0, 0, 0)
                   for y in range(size[1]) for x in range(size[0])])
    return image


def _detail(image: Image.Image, box) -> float:
    x, y, w, h = box
    region = image.crop((x + w // 4, y + h // 4, x + 3 * w // 4, y + 3 * h // 4)).convert("L")
    return ImageStat.Stat(region).stddev[0]


def _face(conn, aid, box, person=None, source="confirmed"):
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality) "
                     "VALUES (?, ?, ?, ?, x'00', 0.9)",
                     (aid, person, source, "[%d,%d,%d,%d]" % box))
        conn.execute("UPDATE assets SET face_version=1 WHERE id=?", (aid,))
        conn.commit()


@pytest.fixture()
def shared(app, people, as_admin):
    """shot0 drawn over with detail, Amma's face on the left named, a stranger
    on the right, shared on a link; the switch on and no face model."""
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.share_blur_strangers = True
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE rel_path LIKE '%shot0.jpg'").fetchone()
    path = Path(row["root"]) / row["rel_path"]
    _busy().save(path, "JPEG", quality=95)
    with db._write_lock:                                              # noqa: SLF001
        amma = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Amma', 0)").lastrowid
        conn.commit()
    _face(conn, row["id"], NAMED, amma)
    _face(conn, row["id"], STRANGER)
    token = as_admin.post("/api/shares", json={"scope": "asset", "target_id": row["id"]}).get_json()["token"]
    return {"id": row["id"], "token": token, "conn": conn, "cfg": cfg, "amma": amma,
            "path": path, "visitor": app.test_client()}


def _picture(response) -> Image.Image:
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    return Image.open(io.BytesIO(response.data)).convert("RGB")


def _no_model(monkeypatch):
    monkeypatch.setattr(api_share, "_face_locator", lambda: None)


def test_the_switch_is_off_until_asked_and_lives_under_advanced():
    assert Config().share_blur_strangers is False
    assert "share_blur_strangers" in GROUPS["Advanced"]


def test_off_the_link_serves_the_photograph_as_before(shared):
    shared["cfg"].share_blur_strangers = False
    plain = Image.open(shared["path"]).convert("RGB")
    got = _picture(shared["visitor"].get(f"/api/share/{shared['token']}/file/{shared['id']}"))
    assert _detail(got, STRANGER) > 0.8 * _detail(plain, STRANGER)


def test_the_stranger_is_blurred_and_amma_is_not(shared, monkeypatch):
    _no_model(monkeypatch)
    plain = Image.open(shared["path"]).convert("RGB")
    got = _picture(shared["visitor"].get(f"/api/share/{shared['token']}/file/{shared['id']}"))
    assert got.size == plain.size
    assert _detail(got, NAMED) > 0.8 * _detail(plain, NAMED)
    assert _detail(got, STRANGER) < 0.3 * _detail(plain, STRANGER)
    # The browser copy is the same blurred picture.
    view = _picture(shared["visitor"].get(f"/api/share/{shared['token']}/preview/{shared['id']}"))
    assert _detail(view, STRANGER) < 0.3 * _detail(plain, STRANGER)


def test_the_tiles_are_blurred_too(shared, monkeypatch):
    _no_model(monkeypatch)
    size = shared["cfg"].thumb_sizes[0]
    got = _picture(shared["visitor"].get(f"/api/share/{shared['token']}/thumb/{shared['id']}?s={size}"))
    assert max(got.size) <= size
    scale = got.size[0] / 400
    named = tuple(int(v * scale) for v in NAMED)
    stranger = tuple(int(v * scale) for v in STRANGER)
    assert _detail(got, stranger) < 0.5 * _detail(got, named)


def test_a_face_the_face_pass_threw_away_is_found_again_and_blurred(shared, monkeypatch):
    """The face pass keeps only faces good enough to recognise; the soft one at
    the back is somebody too."""
    back = (170, 180, 60, 60)
    monkeypatch.setattr(api_share, "_face_locator",
                        lambda: lambda picture: [NAMED, STRANGER, back])
    plain = Image.open(shared["path"]).convert("RGB")
    got = _picture(shared["visitor"].get(f"/api/share/{shared['token']}/file/{shared['id']}"))
    assert _detail(got, back) < 0.3 * _detail(plain, back)
    assert _detail(got, NAMED) > 0.8 * _detail(plain, NAMED)


def test_naming_the_stranger_makes_a_new_copy_with_them_clear(shared, monkeypatch):
    _no_model(monkeypatch)
    plain = Image.open(shared["path"]).convert("RGB")
    url = f"/api/share/{shared['token']}/file/{shared['id']}"
    _picture(shared["visitor"].get(url))
    conn = shared["conn"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE faces SET person_id=?, source='confirmed' WHERE person_id IS NULL",
                     (shared["amma"],))
        conn.commit()
    got = _picture(shared["visitor"].get(url))
    assert _detail(got, STRANGER) > 0.8 * _detail(plain, STRANGER)


def test_a_suggestion_nobody_said_yes_to_is_still_a_stranger(shared, monkeypatch):
    _no_model(monkeypatch)
    conn = shared["conn"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE faces SET person_id=?, source='none' WHERE person_id IS NULL",
                     (shared["amma"],))
        conn.commit()
    plain = Image.open(shared["path"]).convert("RGB")
    got = _picture(shared["visitor"].get(f"/api/share/{shared['token']}/file/{shared['id']}"))
    assert _detail(got, STRANGER) < 0.3 * _detail(plain, STRANGER)


def test_a_photograph_nobody_could_look_at_is_blurred_all_over(shared, monkeypatch):
    _no_model(monkeypatch)
    conn = shared["conn"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("DELETE FROM faces WHERE asset_id=?", (shared["id"],))
        conn.execute("UPDATE assets SET face_version=0 WHERE id=?", (shared["id"],))
        conn.commit()
    plain = Image.open(shared["path"]).convert("RGB")
    got = _picture(shared["visitor"].get(f"/api/share/{shared['token']}/file/{shared['id']}"))
    assert _detail(got, NAMED) < 0.3 * _detail(plain, NAMED)
    assert _detail(got, (150, 200, 80, 80)) < 0.3 * _detail(plain, (150, 200, 80, 80))


def test_with_the_model_an_unlooked_at_photograph_has_only_its_faces_blurred(shared, monkeypatch):
    conn = shared["conn"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("DELETE FROM faces WHERE asset_id=?", (shared["id"],))
        conn.execute("UPDATE assets SET face_version=0 WHERE id=?", (shared["id"],))
        conn.commit()
    monkeypatch.setattr(api_share, "_face_locator", lambda: lambda picture: [STRANGER])
    plain = Image.open(shared["path"]).convert("RGB")
    got = _picture(shared["visitor"].get(f"/api/share/{shared['token']}/file/{shared['id']}"))
    assert _detail(got, STRANGER) < 0.3 * _detail(plain, STRANGER)
    assert _detail(got, NAMED) > 0.8 * _detail(plain, NAMED)


def test_a_hidden_photograph_is_never_handed_to_the_face_model(shared):
    def refuse(picture):
        raise AssertionError("a hidden photograph went to the face model")
    conn = shared["conn"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (shared["id"],))
        conn.commit()
    row = db.get_asset(conn, shared["id"])
    plain = Image.open(shared["path"]).convert("RGB")
    orient = share_faces.opener(conn, row, open_oriented=Image.open, locate=refuse)
    with orient(shared["path"]) as got:
        assert _detail(got, NAMED) < 0.3 * _detail(plain, NAMED)


def test_the_family_app_is_told_the_switch_is_on(shared, as_family):
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["share_blur"] is True
    shared["cfg"].share_blur_strangers = False
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["share_blur"] is False


def test_strangers_keeps_the_named_and_adds_new_faces_once():
    stored = [{"box": (0, 0, 10, 10), "named": True}, {"box": (50, 0, 10, 10), "named": False}]
    found = share_faces.strangers(stored, [(1, 1, 9, 9), (51, 0, 10, 10), (100, 100, 20, 20)])
    assert found == [(50, 0, 10, 10), (100.0, 100.0, 20.0, 20.0)]


def test_the_blur_stays_inside_the_face_and_a_little_round_it():
    plain = _busy()
    got = share_faces.blur_faces(plain.copy(), [STRANGER])
    assert _detail(got, STRANGER) < 0.3 * _detail(plain, STRANGER)
    assert list(got.crop((0, 0, 150, 300)).getdata()) == list(plain.crop((0, 0, 150, 300)).getdata())
