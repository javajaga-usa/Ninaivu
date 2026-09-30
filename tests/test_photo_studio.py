"""The Photo Studio's one request to the server, and the promises around it.

Almost all of the editing happens in the browser, on pixels that never leave it.
What needs the server is one thing: "where are the faces in this picture?", which
the household's own face detector answers. The maps it sends back say which pixels
are skin, hair, a bindi; what they mean is tested in ``test_portrait``. Here it is
the request: who may make it, what it does with a picture it is given (nothing
but look at it), and what it says when it cannot answer.
"""

import base64
import io

import numpy as np
import pytest
from PIL import Image

from ninaivu.media import faces as faces_mod
from ninaivu.media import portrait
from synthetic_faces import draw

pytestmark = pytest.mark.skipif(not portrait.available(), reason="needs OpenCV")

FACES = [dict(x=150, y=170, d=80, skin="fair", bindi="red"),
         dict(x=400, y=170, d=80, skin="dark", hair="black", beard=True)]


@pytest.fixture()
def picture():
    return draw(FACES, size=(560, 420), background=(170, 175, 180))


@pytest.fixture()
def detector(monkeypatch, picture):
    """A face detector that finds exactly the faces that were drawn."""
    monkeypatch.setattr(faces_mod.FaceEngine, "available", property(lambda self: True))
    monkeypatch.setattr(faces_mod.FaceEngine, "locate", lambda self, image, **kwargs: picture.located)


def encoded(rgb, fmt="JPEG"):
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format=fmt, quality=92)
    return buffer.getvalue()


def ask(client, body, content_type="image/jpeg"):
    return client.post("/api/portrait/analyse", data=body, content_type=content_type)


def decode(text):
    return np.asarray(Image.open(io.BytesIO(base64.b64decode(text))))


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------

def test_a_guest_may_not_send_a_picture_for_analysis(as_guest, picture, detector):
    """Same level as editing: whoever may correct a photograph may ask."""
    assert ask(as_guest, encoded(picture.rgb)).status_code == 403


def test_nobody_signed_out_may_ask(client, picture, detector):
    assert ask(client, encoded(picture.rgb)).status_code in (401, 403)


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

def test_every_face_comes_back_with_what_it_needs_and_the_maps_to_find_it(as_family, picture, detector):
    response = ask(as_family, encoded(picture.rgb))
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["size"] == [560, 420]
    assert [face["id"] for face in body["faces"]] == [1, 2]
    assert all(face["usable"] for face in body["faces"])
    assert body["faces"][0]["marks"] is True           # the bindi is known about
    assert body["faces"][1]["beard"] > 0.2

    a, b, c, ids = (decode(body["maps"][name]) for name in ("a", "b", "c", "ids"))
    for name, array in (("a", a), ("b", b), ("c", c), ("ids", ids)):
        assert array.shape == (420, 560, 3), name
    assert a[170 + 50, 150 - 36, 0] > 200               # a cheek of the first face is skin
    assert a[170 + 80, 150 + 10, 0] < 30                # its mouth is not
    assert set(np.unique(ids[..., 0])) <= {0, 1, 2}
    assert ids[170 + 50, 150 - 36, 0] == 1 and ids[170 + 50, 400 - 36, 0] == 2


def test_the_maps_are_opaque_pictures(as_family, picture, detector):
    """A canvas premultiplies alpha on decoding, which would scramble a plane kept in a channel beside one."""
    body = ask(as_family, encoded(picture.rgb)).get_json()
    for name in ("a", "b", "c", "ids"):
        assert Image.open(io.BytesIO(base64.b64decode(body["maps"][name]))).mode == "RGB"


def test_a_png_is_as_good_as_a_jpeg(as_family, picture, detector):
    body = ask(as_family, encoded(picture.rgb, "PNG"), "image/png").get_json()
    assert len(body["faces"]) == 2


def test_a_big_picture_is_looked_at_at_a_working_size(as_family, monkeypatch, detector):
    big = draw([dict(x=300, y=200, d=100, skin="brown")], size=(3600, 2400))
    monkeypatch.setattr(faces_mod.FaceEngine, "locate", lambda self, image, **kwargs: [
        {"box": (0, 0, 10, 10), "landmarks": [(300, 200), (400, 200), (350, 250), (320, 300), (380, 300)],
         "score": 0.9}])
    body = ask(as_family, encoded(big.rgb)).get_json()
    assert max(body["size"]) == portrait.WORK_SIZE
    assert body["size"][0] / body["size"][1] == pytest.approx(1.5, abs=0.01)


# ---------------------------------------------------------------------------
# When there is nothing to say
# ---------------------------------------------------------------------------

def test_a_picture_with_no_face_is_an_honest_empty_answer(as_family, monkeypatch):
    """Silence, not a guess."""
    monkeypatch.setattr(faces_mod.FaceEngine, "available", property(lambda self: True))
    monkeypatch.setattr(faces_mod.FaceEngine, "locate", lambda self, image, **kwargs: [])
    flat = np.full((200, 300, 3), (120, 140, 90), np.uint8)
    body = ask(as_family, encoded(flat)).get_json()
    assert body["faces"] == [] and body["maps"] is None and body["reason"]


def test_faces_too_small_to_work_on_are_reported_not_invented(as_family, monkeypatch):
    monkeypatch.setattr(faces_mod.FaceEngine, "available", property(lambda self: True))
    monkeypatch.setattr(faces_mod.FaceEngine, "locate", lambda self, image, **kwargs: [
        {"box": (10, 10, 6, 6), "landmarks": [(11, 12), (14, 12), (12, 14), (11, 16), (14, 16)], "score": 0.9}])
    body = ask(as_family, encoded(np.full((80, 80, 3), 128, np.uint8))).get_json()
    assert body["maps"] is None and body["reason"]


def test_without_the_face_model_it_says_what_to_install(as_family, picture):
    """The models are a download; until then the editor offers the brush, and says why."""
    response = ask(as_family, encoded(picture.rgb))
    assert response.status_code == 503
    body = response.get_json()
    assert body["needs"] == "faces" and "AI models" in body["error"]


def test_nothing_sent_is_a_mistake_not_a_crash(as_family, detector):
    assert ask(as_family, b"").status_code == 400


def test_something_that_is_not_a_picture_is_refused(as_family, detector):
    response = ask(as_family, b"this is not an image at all")
    assert response.status_code == 400 and "could not be read" in response.get_json()["error"]


def test_an_enormous_picture_is_refused_before_it_fills_the_machine(as_family, detector):
    huge = Image.new("1", (9000, 9000))
    buffer = io.BytesIO()
    huge.save(buffer, format="PNG")
    assert len(buffer.getvalue()) < 1_000_000          # a few kilobytes that unpack to 81 megapixels
    assert ask(as_family, buffer.getvalue(), "image/png").status_code == 413


# ---------------------------------------------------------------------------
# What it does not do
# ---------------------------------------------------------------------------

def test_looking_at_a_picture_changes_nothing(as_family, scanned, picture, detector):
    """It is not an edit and must not behave as one: no file written, no row changed."""
    cfg, conn, _ = scanned
    library = sorted(p for p in __import__("pathlib").Path(cfg.active_root).rglob("*") if p.is_file())
    before = [(p, p.read_bytes()) for p in library]
    rows = conn.execute("SELECT COUNT(*), MAX(indexed_at) FROM assets").fetchone()

    assert ask(as_family, encoded(picture.rgb)).status_code == 200

    assert [(p, p.read_bytes()) for p in sorted(p for p in __import__("pathlib").Path(cfg.active_root).rglob("*") if p.is_file())] == before
    assert tuple(conn.execute("SELECT COUNT(*), MAX(indexed_at) FROM assets").fetchone()) == tuple(rows)


def test_the_old_route_that_looked_at_a_library_photograph_is_gone(as_family, scanned):
    """The studio sends the picture it is showing now — cropped, turned, retouched — so the
    answer lines up with what is on screen. A server that re-read the original could not."""
    _, conn, _ = scanned
    asset = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    assert as_family.post(f"/api/asset/{asset['id']}/portrait-masks").status_code in (404, 405)


def test_the_answer_says_which_method_made_the_maps(as_family, picture, detector, monkeypatch):
    """So the panel can say so, and so a household can tell whether the optional model is being used."""
    from ninaivu.media import face_parser

    assert ask(as_family, encoded(picture.rgb)).get_json()["engine"] == "builtin"

    class Oracle:
        def probabilities(self, rgb, origin=None):
            out = np.zeros((19, *rgb.shape[:2]), np.float32)
            out[0] = 1.0
            y0, x0 = origin
            for truth in picture.truth:
                for label, plane in ((1, truth["skin"]), (17, truth["hair"])):
                    region = plane[y0:y0 + rgb.shape[0], x0:x0 + rgb.shape[1]]
                    out[label][region] = 1.0
                    out[0][region] = 0.0
            return out

    monkeypatch.setattr(face_parser, "get", lambda: Oracle())
    body = ask(as_family, encoded(picture.rgb)).get_json()
    assert body["engine"] == "parser"
    assert all(face["engine"] == "parser" for face in body["faces"])


def needs_the_face_model(test):
    from ninaivu.media import model_catalog

    return pytest.mark.real_models(pytest.mark.skipif(
        not model_catalog.installed("faces"), reason="the face model has not been downloaded")(test))


@needs_the_face_model
def test_a_real_photograph_through_the_whole_request(as_family, monkeypatch):
    """The detector, the analysis and the packing, end to end, on a public-domain portrait."""
    from pathlib import Path

    from ninaivu.media import model_catalog

    # The app pointed every model at this test's empty folder when it started; this one wants the real ones.
    monkeypatch.setattr(model_catalog, "_root", None)
    monkeypatch.delenv("NINAIVU_AI_MODELS_DIR", raising=False)
    data = (Path(__file__).parent / "data" / "portrait.jpg").read_bytes()
    response = ask(as_family, data)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert len(body["faces"]) == 1 and body["faces"][0]["usable"] is True
    assert body["size"] == [1280, 1600]
    assert decode(body["maps"]["a"]).shape == (1600, 1280, 3)
