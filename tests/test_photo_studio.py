"""The Photo Studio's server side, and the promises the editor makes.

Almost all of the editing happens in the browser; what is testable here is the
one request it makes — "where is the face in this photograph" — and the
guarantees around it. Those guarantees are the product: the original is never
written to, nothing leaves the machine, and a selection is proposed rather
than applied.
"""

import numpy as np
import pytest
from PIL import Image
from pathlib import Path

from ninaivu.media import portrait


# ---------------------------------------------------------------------------
# The proposal itself
# ---------------------------------------------------------------------------

def test_a_photograph_with_no_face_proposes_nothing(tmp_path):
    """Silence, not a guess. An empty selection is an honest answer."""
    flat = Image.new("RGB", (400, 300), (120, 140, 90))
    result = portrait.auto_masks(flat, tmp_path)
    assert result["masks"] == {}
    assert result["faces"] == 0
    assert "reason" in result


def test_hair_is_not_offered_without_a_face(tmp_path):
    assert "hair" not in portrait.auto_masks(
        Image.new("RGB", (200, 200), (10, 10, 10)), tmp_path).get("masks", {})


def _hair_probe(background=200):
    """A distinct crown above a face; detector-independent segmentation probe."""
    bgr = np.full((300, 300, 3), background, np.uint8)
    bgr[120:220, 110:190] = 180
    bgr[85:120, 105:195] = 40
    return bgr, (110, 120, 80, 100)


def test_bounded_crown_is_an_editable_full_size_suggestion(monkeypatch):
    bgr, face = _hair_probe()
    monkeypatch.setattr(portrait, "_faces", lambda *_: [face])
    result = portrait.auto_masks(Image.fromarray(bgr))
    hair = result["masks"]["hair"]
    assert hair.shape == (300, 300) and hair.dtype == np.uint8
    assert hair[100, 150] == 255
    assert not hair[120:].any()
    assert not hair[:85].any()


@pytest.mark.parametrize("background", [10, 40, 110, 180])
def test_uniform_background_with_a_face_is_not_hair(background, monkeypatch):
    bgr, face = _hair_probe(background)
    bgr[:120] = background  # no crown: just a wall above the face
    monkeypatch.setattr(portrait, "_faces", lambda *_: [face])
    assert "hair" not in portrait.auto_masks(Image.fromarray(bgr))["masks"]


@pytest.mark.parametrize("failure", ["wall", "bridge", "weak_edge", "skin", "oversize"])
def test_background_false_selections_are_declined(failure):
    bgr, face = _hair_probe()
    if failure == "wall":
        bgr[:120] = 40
    elif failure == "bridge":
        bgr[0:90, 145:150] = 40  # even a narrow path into background must fail
    elif failure == "weak_edge":
        bgr[75:85, 105:195] = 110  # unstable boundary at the second threshold
    elif failure == "skin":
        bgr[115:190, 140:160] = 40
    else:
        bgr[56:140, 80:220] = 40
    assert portrait._hair(bgr, face) is None


@pytest.mark.parametrize("face", [(110, 20, 80, 100), (110, 120, 10, 10)])
def test_cropped_or_small_heads_require_painting(face):
    bgr, _ = _hair_probe()
    assert portrait._hair(bgr, face) is None


def test_light_or_low_contrast_hair_requires_painting():
    bgr, face = _hair_probe()
    bgr[85:120, 105:195] = 170
    assert portrait._hair(bgr, face) is None


@pytest.mark.parametrize("brightness", [.7, 1., 1.3])
def test_existing_portrait_dark_backdrop_is_never_suggested(brightness, monkeypatch):
    from PIL import ImageEnhance

    # Recorded box from the existing local detector; no models or network needed.
    monkeypatch.setattr(portrait, "_faces", lambda *_: [(682, 304, 202, 272)])
    with Image.open(Path(__file__).parent / "data" / "portrait.jpg") as image:
        result = portrait.auto_masks(ImageEnhance.Brightness(image).enhance(brightness))
    assert "skin" in result["masks"]
    assert "hair" not in result["masks"]


def test_hair_suggestion_scales_to_original_size(monkeypatch):
    bgr, face = _hair_probe()
    monkeypatch.setattr(portrait, "_faces", lambda *_: [tuple(v * 3 for v in face)])
    result = portrait.auto_masks(Image.fromarray(bgr).resize((900, 900)))
    assert result["masks"]["hair"].shape == (900, 900)
    assert result["masks"]["hair"][300, 450] == 255


def test_a_runaway_segmentation_falls_back_to_the_shape_it_started_from():
    """GrabCut fails by over-claiming, and it fails silently.

    A mask covering half the frame is not a slightly wrong face, it is the
    segmentation having latched onto the background — and skin smoothing
    applied to half a photograph is the worst thing this tool could do.
    """
    runaway = np.full((100, 100), 255, np.uint8)
    fallback = np.zeros((100, 100), np.uint8)
    fallback[40:60, 40:60] = 255
    kept = portrait._bounded(runaway, fallback)
    assert kept.mean() == pytest.approx(fallback.mean())


def test_a_mask_that_found_nothing_also_falls_back():
    empty = np.zeros((100, 100), np.uint8)
    fallback = np.zeros((100, 100), np.uint8)
    fallback[10:20, 10:20] = 255
    assert portrait._bounded(empty, fallback).mean() > 0


def test_a_reasonable_mask_is_kept_as_it_is():
    good = np.zeros((100, 100), np.uint8)
    good[30:50, 30:50] = 255                  # 4% of the frame
    other = np.zeros((100, 100), np.uint8)
    assert portrait._bounded(good, other).sum() == good.sum()


def test_the_definite_background_is_everything_outside_the_box():
    outside = portrait._outside((100, 100), 20, 20, 40, 40)
    assert outside[0, 0] == 255 and outside[99, 99] == 255
    assert outside[40, 40] == 0


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_a_guest_may_not_ask_where_the_face_is(as_guest, scanned):
    """Same level as editing: whoever may correct a photograph may ask."""
    _, conn, _ = scanned
    asset = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    assert as_guest.post(
        f"/api/asset/{asset['id']}/portrait-masks").status_code == 403


def test_a_family_member_may(as_family):
    ids = [i["id"] for i in as_family.get("/api/assets?limit=1").get_json()["items"]]
    response = as_family.post(f"/api/asset/{ids[0]}/portrait-masks")
    assert response.status_code in (200, 501)      # 501 where OpenCV is absent
    if response.status_code == 200:
        body = response.get_json()
        assert "faces" in body and "masks" in body


def test_a_video_has_no_face_to_find(as_family, scanned):
    _, conn, _ = scanned
    row = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET kind='video' WHERE id=?", (row["id"],))
    conn.commit()
    assert as_family.post(
        f"/api/asset/{row['id']}/portrait-masks").status_code == 400


def test_finding_the_face_never_touches_the_photograph(as_family, scanned):
    """It proposes a selection. It is not an edit and must not behave as one."""
    cfg, conn, _ = scanned
    row = conn.execute(
        "SELECT id, root, rel_path FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    from pathlib import Path

    path = Path(row["root"]) / row["rel_path"]
    before = path.read_bytes()
    stamp = conn.execute("SELECT indexed_at FROM assets WHERE id=?",
                         (row["id"],)).fetchone()["indexed_at"]

    as_family.post(f"/api/asset/{row['id']}/portrait-masks")

    assert path.read_bytes() == before, "the original file must not be written to"
    assert conn.execute("SELECT indexed_at FROM assets WHERE id=?",
                        (row["id"],)).fetchone()["indexed_at"] == stamp


def test_the_masks_come_back_as_images_the_browser_can_draw(as_family):
    import base64
    import io

    ids = [i["id"] for i in as_family.get("/api/assets?limit=1").get_json()["items"]]
    body = as_family.post(f"/api/asset/{ids[0]}/portrait-masks").get_json() or {}
    for name, encoded in (body.get("masks") or {}).items():
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as mask:
            assert mask.mode == "L", f"{name} should be a greyscale mask"
