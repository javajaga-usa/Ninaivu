"""Colour for black-and-white photographs: offered only where a photograph has
no colour of its own, made in the model host, compared with the original, and
kept as a copy beside it the way an edit is, the original never touched."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from ninaivu import runtime
from ninaivu.api import api_colour
from ninaivu.media import media, model_catalog, onnx_tools, upload_review
from ninaivu.runtime import colour as colour_mod
from ninaivu.runtime.host import CAPABILITIES, resolve
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import colour_copies, db

PORTRAIT = Path(__file__).parent / "data" / "portrait.jpg"


def _jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=92)
    return out.getvalue()


# --- telling a black-and-white photograph ------------------------------------------

def test_grey_and_one_tint_are_black_and_white_and_colour_is_not():
    with Image.open(PORTRAIT) as photo:
        colour = photo.convert("RGB")
    grey = colour.convert("L").convert("RGB")
    sepia = ImageOps.colorize(colour.convert("L"), "#2b1d0e", "#f5e6c8")
    noisy = Image.fromarray(np.clip(np.asarray(grey, np.float32)
                                    + np.random.default_rng(1).normal(0, 3, (*grey.size[::-1], 3)),
                                    0, 255).astype(np.uint8))
    assert onnx_tools.looks_black_and_white(grey)
    assert onnx_tools.looks_black_and_white(sepia), "a sepia print has no colour of its own"
    assert onnx_tools.looks_black_and_white(noisy), "a scan's speckle is not colour"
    assert not onnx_tools.looks_black_and_white(colour)
    assert not onnx_tools.looks_black_and_white(Image.new("RGB", (64, 64), (20, 90, 200))), \
        "a picture all of one strong colour is a colour picture"


# --- the model's half -------------------------------------------------------------

class FakeSession:
    """DDColor's shape of answer: two Lab planes for the 512 square."""

    def __init__(self, a=20.0, b=30.0):
        self.a, self.b, self.seen = a, b, []

    def run(self, _names, feeds):
        grey = feeds["input"]
        self.seen.append(grey.shape)
        planes = np.stack([np.full((512, 512), self.a, np.float32),
                           np.full((512, 512), self.b, np.float32)])
        return [planes[None]]


def test_the_colour_is_laid_under_the_photographs_own_light(tmp_path):
    grey = Image.open(PORTRAIT).convert("L").convert("RGB")
    given = tmp_path / "given.png"
    grey.save(given)
    session = FakeSession()
    answer = colour_mod.colour({"session": session, "input": "input"},
                               path=str(given), out=str(tmp_path / "made.jpg"))
    assert session.seen == [(1, 3, 512, 512)]
    assert (answer["width"], answer["height"]) == grey.size
    with Image.open(tmp_path / "made.jpg") as made:
        assert made.size == grey.size and made.format == "JPEG"
        tint = np.asarray(made.convert("YCbCr"), np.float32)[..., 1:].mean(axis=(0, 1)) - 128
        assert tint[1] > 5, "the model's warm colour is in it"
        light = onnx_tools.lightness(np.asarray(made.convert("RGB"), np.float32) / 255)
    original = onnx_tools.lightness(np.asarray(grey, np.float32) / 255)
    assert float(np.mean(np.abs(light - original))) < 3, "every edge and grain is the original's"


def test_the_capability_is_registered_and_says_what_is_missing(monkeypatch, tmp_path):
    assert CAPABILITIES["colour"] == "ninaivu.runtime.colour:COLOUR"
    assert resolve(CAPABILITIES["colour"]) is colour_mod.COLOUR
    monkeypatch.setattr(onnx_tools, "colorize_model_path", lambda: None)
    assert "colouring model" in (colour_mod.ready() or "")
    with pytest.raises(runtime.Unavailable):
        colour_mod.COLOUR.load({})


def test_the_model_is_pinned_on_the_ai_models_page_where_the_playground_finds_it(tmp_path):
    entry = model_catalog.MODELS["colour"]
    (file,) = entry["files"]
    assert file["url"].startswith("https://github.com/") and len(file["sha256"]) == 64
    assert (file["folder"], file["name"]) == ("onnx", "ddcolor.onnx")
    assert "Apache-2.0" in entry["licence"] and entry["requires"] == ["onnxruntime"]
    assert file["name"] in onnx_tools.COLORIZE_FILES


# --- the switch ------------------------------------------------------------------------

def test_off_by_default_with_the_advanced_settings():
    assert Config().colour_photos is False
    assert "colour_photos" in GROUPS["Advanced"]


def test_off_every_route_is_not_there(as_admin):
    target = _a_photograph(as_admin)
    assert as_admin.get(f"/api/asset/{target}/colour").status_code == 404
    assert as_admin.post(f"/api/asset/{target}/colour").status_code == 404
    assert as_admin.get(f"/api/asset/{target}/colour/upright").status_code == 404
    assert as_admin.get("/api/status").get_json()["moments"]["colour"] is False


# --- in the viewer --------------------------------------------------------------------

def _a_photograph(client) -> int:
    items = client.get("/api/assets?limit=50").get_json()["items"]
    return next(i["id"] for i in items if i["kind"] == "picture" and "copy" not in i["name"])


class FakeHost:
    """The quick model host, colouring with a flat warm tint."""

    def __init__(self):
        self.calls = []

    def call(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        with Image.open(kwargs["path"]) as given:
            self.given = given.size
            made = given.convert("RGB")
        Image.blend(made, Image.new("RGB", made.size, (200, 140, 90)), 0.3).save(kwargs["out"], "JPEG")
        return {"width": made.width, "height": made.height, "version": 1, "took": 0.1}


@pytest.fixture()
def coloured(app, scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.colour_photos = True
    host = FakeHost()
    monkeypatch.setattr(api_colour, "_host", lambda: host)
    monkeypatch.setattr(colour_mod, "ready", lambda: None)
    return {"cfg": cfg, "conn": conn, "host": host}


def _make_black_and_white(cfg, conn, asset_id, size=(120, 80), rotation=0, colour=False):
    row = db.get_asset(conn, asset_id)
    grey = Image.open(PORTRAIT).convert("RGB" if colour else "L").convert("RGB").resize(size)
    grey.save(Path(row["root"]) / row["rel_path"], "JPEG")
    media.write_thumbnails(grey, cfg.thumbs_dir, row["thumb"], cfg.thumb_sizes, cfg.thumb_format)
    db.update_asset(conn, asset_id, width=size[0], height=size[1], rotation=rotation)


def test_add_colour_is_offered_on_a_black_and_white_photograph_only(as_family, coloured):
    cfg, conn = coloured["cfg"], coloured["conn"]
    target = _a_photograph(as_family)
    assert as_family.get("/api/status").get_json()["moments"]["colour"] is True
    _make_black_and_white(cfg, conn, target, colour=True)
    status = as_family.get(f"/api/asset/{target}/colour").get_json()
    assert status["mono"] is False and status["ready"] is True and status["source"] is None
    _make_black_and_white(cfg, conn, target)
    status = as_family.get(f"/api/asset/{target}/colour").get_json()
    assert status["mono"] is True and status["copies"] == []


def test_a_guest_is_not_offered_colour(as_guest, as_admin, coloured):
    target = _a_photograph(as_admin)
    assert as_guest.get(f"/api/asset/{target}/colour").status_code in (403, 404)
    assert as_guest.post(f"/api/asset/{target}/colour").status_code in (403, 404)


def test_colouring_answers_with_the_picture_upright_and_keeps_nothing(as_family, coloured):
    cfg, conn, host = coloured["cfg"], coloured["conn"], coloured["host"]
    target = _a_photograph(as_family)
    _make_black_and_white(cfg, conn, target, size=(120, 80), rotation=90)
    original = (Path(db.get_asset(conn, target)["root"]) / db.get_asset(conn, target)["rel_path"]).read_bytes()
    response = as_family.post(f"/api/asset/{target}/colour")
    assert response.status_code == 200 and response.mimetype == "image/jpeg"
    assert response.headers["Cache-Control"] == "no-store"
    with Image.open(io.BytesIO(response.data)) as made:
        assert made.size == (80, 120), "turned upright as the viewer shows it"
    assert host.calls[0][0] == "colour" and host.given == (80, 120)
    assert list((Path(cfg.state_dir) / "colour").iterdir()) == [], "neither file is kept"
    row = db.get_asset(conn, target)
    assert (Path(row["root"]) / row["rel_path"]).read_bytes() == original, "the original is untouched"


def test_without_the_model_it_says_where_to_get_it(as_family, coloured, monkeypatch):
    monkeypatch.setattr(colour_mod, "ready", lambda: "the colouring model is not downloaded (AI models page)")
    target = _a_photograph(as_family)
    status = as_family.get(f"/api/asset/{target}/colour").get_json()
    assert status["ready"] is False and "AI models" in status["why"]
    response = as_family.post(f"/api/asset/{target}/colour")
    assert response.status_code == 409 and "AI models" in response.get_json()["error"]


def test_a_busy_host_is_said_not_waited_on_for_ever(as_family, coloured, monkeypatch):
    class Busy:
        def call(self, *_a, **_k):
            raise runtime.HostBusy("busy")
    monkeypatch.setattr(api_colour, "_host", lambda: Busy())
    target = _a_photograph(as_family)
    assert as_family.post(f"/api/asset/{target}/colour").status_code == 429
    assert list((Path(coloured["cfg"].state_dir) / "colour").iterdir()) == []


def test_kept_it_is_a_copy_beside_the_original_with_before_and_after(as_admin, coloured):
    cfg, conn = coloured["cfg"], coloured["conn"]
    target = _a_photograph(as_admin)
    _make_black_and_white(cfg, conn, target)
    made = as_admin.post(f"/api/asset/{target}/colour").data
    response = as_admin.post(f"/api/asset/{target}/edited-copy?made=colour",
                             data=made, content_type="image/jpeg")
    assert response.status_code == 201, response.get_json()
    copy = response.get_json()["id"]
    row = db.get_asset(conn, copy)
    source = db.get_asset(conn, target)
    assert row["caption"].startswith("Colour copy of")
    assert Path(row["root"], row["rel_path"]).parent == Path(source["root"], source["rel_path"]).parent
    assert colour_copies.source_of(conn, copy) == target
    # The copy offers Before and after; the original, the copy.
    status = as_admin.get(f"/api/asset/{copy}/colour").get_json()
    assert status["source"] == target and status["mono"] is False
    assert status["before"] == f"/api/asset/{target}/colour/upright"
    before = as_admin.get(status["before"])
    assert before.status_code == 200 and before.mimetype == "image/jpeg"
    assert as_admin.get(f"/api/asset/{target}/colour").get_json()["copies"] == [copy]
    # Either one leaving the index takes the link with it.
    with db._write_lock:
        conn.execute("DELETE FROM assets WHERE id=?", (copy,))
        conn.commit()
    assert colour_copies.copies_of(conn, target) == []


def test_an_ordinary_edit_is_not_a_colour_copy(as_admin, coloured):
    conn = coloured["conn"]
    target = _a_photograph(as_admin)
    response = as_admin.post(f"/api/asset/{target}/edited-copy",
                             data=_jpeg(Image.new("RGB", (40, 30), (9, 9, 9))),
                             content_type="image/jpeg")
    assert response.status_code == 201
    assert colour_copies.source_of(conn, response.get_json()["id"]) is None
    assert db.get_asset(conn, response.get_json()["id"])["caption"].startswith("Edited copy of")


def test_a_family_members_colour_copy_waits_for_approval_then_is_linked(as_family, coloured, people):
    cfg, conn = coloured["cfg"], coloured["conn"]
    target = _a_photograph(as_family)
    _make_black_and_white(cfg, conn, target)
    made = as_family.post(f"/api/asset/{target}/colour").data
    response = as_family.post(f"/api/asset/{target}/edited-copy?made=colour",
                              data=made, content_type="image/jpeg")
    assert response.status_code == 202, response.get_json()
    approved = upload_review.approve(conn, cfg, response.get_json()["pending_id"], people["admin"].id)
    assert colour_copies.source_of(conn, approved["id"]) == target
    assert approved["caption"].startswith("Colour copy of")
