"""Audit after 2.2.0, the second part: what the trials cost the web threads.

A share link with strangers blurred makes its copies at most two at a
time, and a tile that would wait gets a stand-in blurred all over while the
real one is made off the web threads; young faces is read once for everybody
with the same limits, not once per person; and the long AI answers together
never hold more than a quarter of the web threads.
"""
from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from ninaivu.api import (_long_calls, api_letters, api_life_story, api_our_year,
                         api_photo_talk, api_share, api_young_faces)
from ninaivu.server import auth
from ninaivu.storage import db

from conftest import login

NAMED = (40, 60, 80, 80)


def _busy(size=(400, 300)) -> Image.Image:
    image = Image.new("RGB", size)
    image.putdata([((x * 37) % 256, (y * 53) % 256, ((x ^ y) * 7) % 256)
                   if (x // 4 + y // 4) % 2 else (0, 0, 0)
                   for y in range(size[1]) for x in range(size[0])])
    return image


def _detail(image: Image.Image, box) -> float:
    x, y, w, h = box
    region = image.crop((x + w // 4, y + h // 4, x + 3 * w // 4, y + 3 * h // 4)).convert("L")
    return ImageStat.Stat(region).stddev[0]


@pytest.fixture()
def shared(app, people, as_admin, monkeypatch):
    """shot0 with Amma's face named on the left, shared, the switch on, no
    face model, and its thumbnail drawn over with detail too."""
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.share_blur_strangers = True
    monkeypatch.setattr(api_share, "_face_locator", lambda: None)
    row = db.get_asset(conn, conn.execute(
        "SELECT id FROM assets WHERE rel_path LIKE '%shot0.jpg'").fetchone()["id"])
    path = Path(row["root"]) / row["rel_path"]
    _busy().save(path, "JPEG", quality=95)
    with db._write_lock:                                              # noqa: SLF001
        amma = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Amma', 0)").lastrowid
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality) "
                     "VALUES (?, ?, 'confirmed', ?, x'00', 0.9)",
                     (row["id"], amma, "[%d,%d,%d,%d]" % NAMED))
        conn.execute("UPDATE assets SET face_version=1 WHERE id=?", (row["id"],))
        conn.commit()
    size = cfg.thumb_sizes[0]
    from ninaivu.media import media
    thumb = cfg.thumbs_dir / media.thumb_file(row["thumb"], size, cfg.thumb_format)
    tile = _busy()
    tile.thumbnail((size, size))
    tile.save(thumb, cfg.thumb_format)
    token = as_admin.post("/api/shares", json={"scope": "asset", "target_id": row["id"]}).get_json()["token"]
    return {"id": row["id"], "url": f"/api/share/{token}/thumb/{row['id']}?s={size}",
            "visitor": app.test_client(), "size": size}


@pytest.fixture()
def no_turn_free(monkeypatch):
    """Both blur turns taken, as on the first visit to a big share link."""
    monkeypatch.setattr(api_share, "TILE_WAIT", 0.05)
    for _ in range(api_share.BLUR_BUILDS):
        api_share._building.acquire()                                 # noqa: SLF001
    yield
    for _ in range(api_share.BLUR_BUILDS):
        api_share._building.release()                                 # noqa: SLF001


def _picture(response) -> Image.Image:
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    return Image.open(io.BytesIO(response.data)).convert("RGB")


def test_a_tile_that_would_wait_gets_a_stand_in_blurred_all_over(shared, no_turn_free, monkeypatch):
    warmed = []
    monkeypatch.setattr(api_share, "_warm", lambda row, path, size: warmed.append(int(row["id"])))
    response = shared["visitor"].get(shared["url"])
    got = _picture(response)
    assert "max-age=30" in response.headers["Cache-Control"]
    scale = got.size[0] / 400
    named = tuple(int(v * scale) for v in NAMED)
    # Blurred all over, Amma too: the faces have not been looked at yet.
    plain = _busy()
    plain.thumbnail((shared["size"], shared["size"]))
    assert _detail(got, named) < 0.3 * _detail(plain, named)
    assert warmed == [shared["id"]]


def test_the_real_tile_is_made_behind_the_stand_in(shared, monkeypatch):
    monkeypatch.setattr(api_share, "TILE_WAIT", 0.05)
    for _ in range(api_share.BLUR_BUILDS):
        api_share._building.acquire()                                 # noqa: SLF001
    try:
        first = shared["visitor"].get(shared["url"])
        assert "max-age=30" in first.headers["Cache-Control"]
    finally:
        for _ in range(api_share.BLUR_BUILDS):
            api_share._building.release()                             # noqa: SLF001
    warmer = api_share._warmer                                        # noqa: SLF001
    assert warmer is not None
    warmer.join(timeout=30)
    again = shared["visitor"].get(shared["url"])
    got = _picture(again)
    assert "max-age=3600" in again.headers["Cache-Control"]
    scale = got.size[0] / 400
    named = tuple(int(v * scale) for v in NAMED)
    plain = _busy()
    plain.thumbnail((shared["size"], shared["size"]))
    # The real one: Amma, who is named, is clear.
    assert _detail(got, named) > 0.6 * _detail(plain, named)


def test_the_full_picture_waits_for_its_turn_instead(shared, monkeypatch):
    monkeypatch.setattr(api_share, "TILE_WAIT", 0.05)
    api_share._building.acquire()                                     # noqa: SLF001
    api_share._building.acquire()                                     # noqa: SLF001
    threading.Timer(0.3, api_share._building.release).start()        # noqa: SLF001
    try:
        response = shared["visitor"].get(shared["url"].replace("/thumb/", "/file/").split("?")[0])
        assert response.status_code == 200
        assert "max-age=3600" in response.headers["Cache-Control"]
    finally:
        api_share._building.release()                                 # noqa: SLF001


def test_young_faces_is_read_once_for_everybody_with_the_same_limits(app, people, as_family, monkeypatch):
    cfg = app.config["MV_CONFIG"]
    cfg.young_faces = True
    auth.create_user(people["conn"], "maya2", "a-long-password-2", display_name="Maya's brother",
                     role=auth.ROLE_FAMILY, created_by=people["admin"].id)
    brother = login(app.test_client(), "maya2", "a-long-password-2")
    read = []
    real = api_young_faces._find

    def counted():
        read.append(1)
        return real()
    monkeypatch.setattr(api_young_faces, "_find", counted)
    assert as_family.get("/api/young-faces").status_code == 200
    assert brother.get("/api/young-faces").status_code == 200
    assert len(read) == 1


@pytest.fixture()
def eight_threads(app):
    cfg = app.config["MV_CONFIG"]
    before = cfg.server_threads
    cfg.server_threads = 8
    yield
    cfg.server_threads = before


def test_long_ai_answers_hold_at_most_a_quarter_of_the_web_threads(app, eight_threads):
    with app.app_context():
        assert _long_calls.most() == 2
        assert _long_calls.take() and _long_calls.take()
        try:
            assert not _long_calls.take()
        finally:
            _long_calls.give()
            _long_calls.give()
        app.config["MV_CONFIG"].server_threads = 4
        assert _long_calls.most() == 2


def test_a_feature_with_a_turn_free_still_waits_for_a_place(app, eight_threads):
    with app.app_context():
        assert _long_calls.take() and _long_calls.take()
        try:
            # Photo talk has two turns of its own, both free, and no place.
            assert not api_photo_talk._asking.acquire(timeout=0.01)   # noqa: SLF001
            # Its own turn was given back, not kept.
            assert api_photo_talk._asking._own.acquire(timeout=0)     # noqa: SLF001
            api_photo_talk._asking._own.release()                     # noqa: SLF001
        finally:
            _long_calls.give()
            _long_calls.give()
        assert api_photo_talk._asking.acquire(timeout=0.01)           # noqa: SLF001
        api_photo_talk._asking.release()                              # noqa: SLF001
        assert _long_calls._held == 0                                 # noqa: SLF001


def test_every_long_ai_answer_takes_a_place():
    for limit in (api_life_story._writing, api_our_year._writing,      # noqa: SLF001
                  api_photo_talk._asking, api_letters._speaking):      # noqa: SLF001
        assert isinstance(limit, _long_calls.Limited)
