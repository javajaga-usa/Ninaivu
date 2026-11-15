"""Blurred previews for the gallery's tiles (media/placeholders.py,
api/api_placeholders.py): no tile is ever empty, however fast the scroll.

What is checked: the preview format holds a picture's colours where they
were, the route answers in the order asked and only for what the caller may
see (the thumbnail's own gate: admins everything, a family member nothing
hidden or explicit), previews are kept and made again when the thumbnail
changes, and the helper that makes the rest of the library's finishes.
"""
from __future__ import annotations

import time

import numpy as np

from ninaivu.media import placeholders
from ninaivu.storage import db


def _read(ids, body):
    out = {}
    at = 0
    for asset_id in ids:
        length = body[at]
        out[asset_id] = bytes(body[at + 1:at + 1 + length])
        at += 1 + length
    assert at == len(body), "nothing more than the ids asked for"
    return out


def _decode_average(blob):
    """The format's own average colour, from the header (l, p, q)."""
    h24 = blob[0] | (blob[1] << 8) | (blob[2] << 16)
    lum = (h24 & 63) / 63
    p = ((h24 >> 6) & 63) / 31.5 - 1
    q = ((h24 >> 12) & 63) / 31.5 - 1
    b = lum - 2 / 3 * p
    r = (3 * lum - b + q) / 2
    g = r - q
    return r, g, b


def test_a_preview_is_a_few_dozen_bytes_and_keeps_the_colour():
    red = np.zeros((24, 32, 3), np.uint8)
    red[...] = (220, 30, 20)
    blob = placeholders.encode_rgb(red)
    assert 5 < len(blob) <= 32
    r, g, b = _decode_average(blob)
    assert r > 0.7 and g < 0.25 and b < 0.25
    # Landscape is said in the header, and the short side's count with it.
    assert blob[4] >> 7 == 1
    tall = placeholders.encode_rgb(np.zeros((32, 20, 3), np.uint8) + 128)
    assert tall[4] >> 7 == 0


def test_a_preview_of_a_thumbnail_file(tmp_path):
    from PIL import Image

    path = tmp_path / "t_256.webp"
    Image.new("RGB", (256, 192), (20, 40, 200)).save(path, "WEBP")
    r, g, b = _decode_average(placeholders.encode_file(path))
    assert b > 0.6 and r < 0.2


def test_the_answer_is_in_the_order_asked_with_nothing_for_none():
    packed = placeholders.pack([3, 1, 2], {1: b"abc", 2: b""})
    assert packed == bytes([0, 3]) + b"abc" + bytes([0])


def test_previews_are_kept_and_made_again_when_the_thumbnail_changes(tmp_path):
    from PIL import Image

    source = tmp_path / "a_256.webp"
    Image.new("RGB", (64, 48), (200, 0, 0)).save(source, "WEBP")
    store = placeholders.Store(tmp_path)
    row = {"id": 7, "thumb": "a"}
    made = placeholders.previews(store, [row], source=lambda r: source, version=lambda r: "1r0")
    first = made[7]
    assert store.get({7: placeholders.stamp(row, "1r0")}) == {7: first}

    # Turned (a new version): the old one is not handed out, a new one is made.
    Image.new("RGB", (64, 48), (0, 0, 200)).save(source, "WEBP")
    assert store.get({7: placeholders.stamp(row, "2r90")}) == {}
    again = placeholders.previews(store, [row], source=lambda r: source, version=lambda r: "2r90")
    assert again[7] != first
    assert _decode_average(again[7])[2] > 0.6


def test_a_missing_thumbnail_is_passed_over(tmp_path):
    store = placeholders.Store(tmp_path)
    got = placeholders.previews(store, [{"id": 1, "thumb": "gone"}],
                                source=lambda r: tmp_path / "nope.webp", version=lambda r: "1")
    assert got == {}


def _ids(people):
    conn = people["conn"]
    return [r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE thumb IS NOT NULL AND thumb != '' ORDER BY id")]


def test_the_route_answers_for_what_the_caller_may_see(people, as_admin, as_family):
    conn = people["conn"]
    ids = _ids(people)
    assert len(ids) >= 3
    hidden, explicit, plain = ids[:3]
    with db._write_lock:
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden,))
        conn.execute("UPDATE assets SET nsfw=1 WHERE id=?", (explicit,))
        conn.commit()
    asked = [plain, hidden, explicit, 999999]
    query = "/api/placeholders?ids=" + ",".join(map(str, asked))

    admin = as_admin.get(query)
    assert admin.status_code == 200
    assert admin.mimetype == "application/octet-stream"
    assert "private" in admin.headers["Cache-Control"]
    seen = _read(asked, admin.data)
    assert seen[plain] and seen[hidden] and seen[explicit], "admins see everything"
    assert seen[999999] == b""

    family = _read(asked, as_family.get(query).data)
    assert family[plain]
    assert family[hidden] == b"", "hidden from a family member"
    assert family[explicit] == b"", "explicit is an administrator's alone"

    # The same as the thumbnail itself, item by item.
    for asset_id in asked[:3]:
        thumb = as_family.get(f"/api/thumb/{asset_id}").status_code
        assert (thumb == 200) == bool(family[asset_id])


def test_a_guest_gets_nothing_not_shared(people, anon):
    ids = _ids(people)[:3]
    body = anon.get("/api/placeholders?ids=" + ",".join(map(str, ids))).data
    assert all(v == b"" for v in _read(ids, body).values())


def test_nothing_asked_is_an_empty_answer(people, as_admin):
    answer = as_admin.get("/api/placeholders")
    assert answer.status_code == 200 and answer.data == b""
    answer = as_admin.get("/api/placeholders?ids=x,,y")
    assert answer.status_code == 200 and answer.data == b""


def test_at_most_a_block_is_answered(people, as_admin):
    ids = list(range(1, 2000))
    body = as_admin.get("/api/placeholders?ids=" + ",".join(map(str, ids))).data
    from ninaivu.api.api_placeholders import MOST

    _read(ids[:MOST], body)


def test_the_rest_of_the_library_is_made_in_the_background(people, as_admin, app):
    ids = _ids(people)
    as_admin.get(f"/api/placeholders?ids={ids[0]}")
    store, filler = app.config["NINAIVU_PLACEHOLDERS"]
    filler.thread.join(timeout=30)
    assert not filler.thread.is_alive()
    conn = store._db()
    made = {r[0] for r in conn.execute("SELECT asset_id FROM placeholders")}
    assert set(ids) <= made


def test_the_route_is_declared_for_the_gate():
    from ninaivu.server import routes

    assert routes.OPEN["api.placeholders_for"][0] == "guest"


def test_a_slow_filler_stops_when_asked(tmp_path):
    calls = []

    def rows(after, limit):
        calls.append(after)
        time.sleep(0.01)
        return [{"id": after + 1, "thumb": ""}]

    filler = placeholders.Filler(placeholders.Store(tmp_path), rows=rows,
                                 source=lambda r: tmp_path, version=lambda r: "1")
    filler.start()
    time.sleep(0.2)
    filler.stop.set()
    filler.thread.join(timeout=5)
    assert not filler.thread.is_alive()
    assert len(calls) >= 2
