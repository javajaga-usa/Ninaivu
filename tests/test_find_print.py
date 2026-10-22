"""Find this print: which library photograph a phone picture of a print is.

The photographs are drawn: a "painting" of soft random blocks that has a
fingerprint of its own, hung in a dark frame with a cream mount on a wall.
The picture-search model is not installed here and is replaced by a fake
where a test is about how it is used.
"""

from __future__ import annotations

import io
import time

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from ninaivu.media import find_print, jobs, media
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def painting(seed: int, size=(480, 360)) -> Image.Image:
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 255, (6, 8, 3), dtype=np.uint8)
    img = Image.fromarray(blocks, "RGB").resize(size, Image.Resampling.BICUBIC)
    return img.filter(ImageFilter.GaussianBlur(4))


def framed(picture: Image.Image, glare: bool = True) -> Image.Image:
    """The picture behind glass on a wall: frame, mount, a little glare."""
    mount = 40
    inner = Image.new("RGB", (picture.width + 2 * mount, picture.height + 2 * mount),
                      (236, 230, 214))
    inner.paste(picture, (mount, mount))
    frame = Image.new("RGB", (inner.width + 60, inner.height + 60), (58, 36, 22))
    frame.paste(inner, (30, 30))
    wall = Image.new("RGB", (frame.width + 500, frame.height + 400), (196, 200, 188))
    wall.paste(frame, (250, 180))
    if glare:
        layer = Image.new("RGB", wall.size, (255, 255, 255))
        mask = Image.new("L", wall.size, 0)
        ImageDraw.Draw(mask).ellipse((420, 260, 560, 330), fill=40)
        wall = Image.composite(layer, wall, mask.filter(ImageFilter.GaussianBlur(20)))
    return wall


def jpeg(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------

def test_off_by_default_and_under_advanced():
    assert Config().find_print is False
    assert "find_print" in GROUPS["Advanced"]


def test_fingerprints_are_counted_in_bits_and_kept_within_the_limit():
    rows = [(1, "0000000000000000"), (2, "000000000000000f"), (3, "ffffffffffffffff"),
            (4, "not a hash"), (5, None)]
    found = find_print.by_fingerprint(rows, [[0], [0xffffffffffffff00]], within=8)
    assert found == {1: (0, 0), 2: (4, 0), 3: (8, 1)}


def test_the_picture_found_through_glass_has_nearly_its_own_fingerprint():
    picture = painting(1)
    seen = find_print.views(framed(picture))
    stored = media.perceptual_hash(picture.convert("L"))
    other = media.perceptual_hash(painting(2).convert("L"))
    found = find_print.by_fingerprint([(10, stored), (11, other)],
                                      [find_print.fingerprints(seen)])
    assert 10 in found and found[10][0] <= find_print.SURE_BITS
    assert 11 not in found


def test_a_print_stood_up_the_wrong_way_is_still_found():
    picture = painting(3)
    stored = media.perceptual_hash(picture.convert("L"))
    turned = picture.rotate(90, expand=True)
    found = find_print.by_fingerprint([(7, stored)], [find_print.fingerprints([turned])])
    assert found[7][0] == 0


class FakeEngine:
    semantic = True

    def __init__(self, vector, dim=4):
        self.vector = np.asarray(vector, np.float32)
        self.dim = dim

    def embed_images(self, images):
        return np.tile(self.vector[: self.dim], (len(images), 1))


def _store(vectors: dict[int, list[float]]):
    ids = list(vectors)
    block = np.asarray([vectors[i] for i in ids], np.float32)
    block /= np.linalg.norm(block, axis=1, keepdims=True)
    return ids, memoryview(block.tobytes()), block.shape[1], {i: n for n, i in enumerate(ids)}


def test_the_picture_search_finds_what_looks_alike():
    store = _store({1: [1, 0, 0, 0], 2: [0.9, 0.3, 0, 0], 3: [0, 1, 0, 0]})
    found = find_print.by_look(FakeEngine([1, 0, 0, 0]), [[painting(1)]], store)
    assert set(found) == {1, 2}
    assert found[1][0] == pytest.approx(1.0) and found[2][0] < found[1][0]


def test_no_picture_search_without_the_model_or_with_another_models_vectors():
    store = _store({1: [1, 0, 0, 0]})
    assert find_print.by_look(None, [[painting(1)]], store) == {}
    assert find_print.by_look(object(), [[painting(1)]], store) == {}
    wider = FakeEngine([1, 0, 0, 0, 0, 0], dim=6)
    assert find_print.by_look(wider, [[painting(1)]], store) == {}


def test_sure_matches_come_first():
    matches = find_print.combine({1: (14, 0), 2: (3, 0)}, {3: (0.86, 0), 1: (0.88, 0),
                                                           4: (0.95, 1)})
    assert [m.id for m in matches if m.sure] == [4, 2, 1]
    assert [m.id for m in matches][-1] == 3 and not matches[-1].sure
    assert matches[0].as_dict()["how"] == ["look"] and matches[0].prints == {1}


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_jobs_left_over():
    jobs.reset()
    yield
    jobs.reset()


@pytest.fixture()
def hung(app, people):
    """Two library photographs given the fingerprints of two paintings; the
    second hidden from the family."""
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.find_print = True
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 "
        "AND visibility<2 AND nsfw=0 ORDER BY id")]
    shown, hidden = ids[0], ids[1]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET phash=? WHERE id=?",
                     (media.perceptual_hash(painting(1).convert("L")), shown))
        conn.execute("UPDATE assets SET phash=?, visibility=? WHERE id=?",
                     (media.perceptual_hash(painting(2).convert("L")), auth.VIS_HIDDEN, hidden))
        conn.commit()
    return {"shown": shown, "hidden": hidden, "cfg": cfg, "conn": conn}


def _find(client, *photos):
    data = {"photo": [(io.BytesIO(p), f"IMG_{i}.jpg") for i, p in enumerate(photos)]}
    return client.post("/api/prints/find", data=data, content_type="multipart/form-data")


def test_the_route_is_404_until_switched_on(as_family):
    assert _find(as_family, jpeg(framed(painting(1)))).status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["find_print"] is False


def test_a_guest_may_not_look(as_guest, hung):
    assert _find(as_guest, jpeg(framed(painting(1)))).status_code in (401, 403)


def test_the_original_of_a_framed_print_is_found(as_family, hung):
    got = _find(as_family, jpeg(framed(painting(1))))
    assert got.status_code == 200, got.get_json()
    answer = got.get_json()
    assert answer["verdict"] == "sure"
    assert answer["matches"][0]["id"] == hung["shown"]
    assert answer["matches"][0]["how"] == ["fingerprint"]
    assert "hidden" not in answer["matches"][0]
    assert answer["session"] and answer["photos"][0]["prints"]
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["find_print"] is True


def test_a_hidden_original_is_found_for_an_administrator_only(as_family, as_admin, hung):
    photo = jpeg(framed(painting(2)))
    family = _find(as_family, photo).get_json()
    assert family["verdict"] == "none" and family["matches"] == []
    admin = _find(as_admin, photo).get_json()
    assert admin["verdict"] == "sure"
    assert admin["matches"][0]["id"] == hung["hidden"] and admin["matches"][0]["hidden"] is True


def test_with_no_copy_the_print_is_taken_in(as_admin, hung):
    answer = _find(as_admin, jpeg(framed(painting(9)))).get_json()
    assert answer["verdict"] == "none"
    started = as_admin.post("/api/prints/save", json={"session": answer["session"],
                                                      "picks": [[0, 0]]})
    assert started.status_code == 202, started.get_json()
    job = started.get_json()["id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        state = as_admin.get(f"/api/prints/jobs/{job}").get_json()
        if state["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert state["state"] == "done", state
    result = as_admin.get(f"/api/prints/jobs/{job}/result").get_json()
    assert len(result["saved"]) == 1


def test_one_photograph_at_a_time(as_family, hung):
    assert _find(as_family).status_code == 400
    two = jpeg(framed(painting(1)))
    assert _find(as_family, two, two).status_code == 400


def test_the_picture_search_finds_it_too(as_family, hung, app, monkeypatch):
    conn = hung["conn"]
    vector = np.asarray([0.0, 1.0, 0.0, 0.0], np.float32)
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("DELETE FROM embeddings")
        conn.execute("INSERT INTO embeddings(asset_id, model, vector, dim) VALUES (?, 'fake', ?, 4)",
                     (hung["shown"], vector.tobytes()))
        conn.commit()
    monkeypatch.setattr(app.config["MV_SERVICES"], "engine", FakeEngine([0, 1, 0, 0]))
    answer = _find(as_family, jpeg(framed(painting(1)))).get_json()
    assert answer["search"] is True
    assert answer["matches"][0]["id"] == hung["shown"]
    assert answer["matches"][0]["how"] == ["fingerprint", "look"]
