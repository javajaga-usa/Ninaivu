"""Scan old prints: finding prints in a photograph of them, and filing them.

Every picture here is drawn: rectangles of a pale card with a picture on it,
laid on a darker "table" with some grain. That is enough to test what the
detector promises — where the corners are, how many prints, what happens
when it finds none — without a photograph of anybody's real album, and it is
deterministic. The heavy models (orientation, face restoration, upscaling,
the picture-search model) are not installed here, and are replaced by fakes
where a test is about how they are used.
"""

from __future__ import annotations

import io
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from ninaivu.media import jobs, print_scan, upload_review
from ninaivu.storage import db


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def table(size=(1600, 1200), colour=(92, 64, 40)) -> Image.Image:
    img = Image.new("RGB", size, colour)
    draw = ImageDraw.Draw(img)
    grain = tuple(c + 8 for c in colour)
    for x in range(0, size[0], 37):
        draw.line([(x, 0), (x + 20, size[1])], fill=grain, width=2)
    return img


def card(w: int, h: int) -> Image.Image:
    """A print: a cream border round a blue picture with a face-coloured oval."""
    out = Image.new("RGB", (w, h), (238, 232, 220))
    draw = ImageDraw.Draw(out)
    draw.rectangle((w * 0.07, h * 0.07, w * 0.93, h * 0.93), fill=(60, 110, 150))
    draw.ellipse((w * 0.3, h * 0.25, w * 0.6, h * 0.75), fill=(210, 170, 140))
    return out


def lay(img: Image.Image, box, angle: float = 0) -> Image.Image:
    x0, y0, x1, y1 = box
    piece = card(x1 - x0, y1 - y0)
    if not angle:
        img.paste(piece, (x0, y0))
        return img
    mask = Image.new("L", piece.size, 255).rotate(angle, expand=True)
    piece = piece.rotate(angle, expand=True)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    img.paste(piece, (cx - piece.width // 2, cy - piece.height // 2), mask)
    return img


def jpeg(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def corners(found) -> list[tuple[int, int]]:
    return [(round(x), round(y)) for x, y in found.quad]


def near(a, b, tolerance=6) -> bool:
    return all(abs(p - q) <= tolerance for pa, pb in zip(a, b) for p, q in zip(pa, pb))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_one_print_is_found_at_its_corners():
    found = print_scan.find_prints(lay(table(), (400, 300, 1100, 800)))
    assert len(found) == 1 and not found[0].whole
    assert near(corners(found[0]), [(400, 300), (1100, 300), (1100, 800), (400, 800)])


def test_three_prints_are_found_in_reading_order():
    img = table()
    boxes = [(100, 150, 500, 450), (650, 150, 1000, 600), (1100, 300, 1500, 1000)]
    for box in boxes:
        lay(img, box)
    found = print_scan.find_prints(img)
    assert len(found) == 3
    for item, (x0, y0, x1, y1) in zip(found, boxes):
        assert near(corners(item), [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def test_the_picture_inside_a_prints_border_is_not_a_second_print():
    found = print_scan.find_prints(lay(table(), (300, 200, 1300, 1000)))
    assert len(found) == 1


def test_a_print_laid_crooked_is_found_and_flattened_to_its_own_size():
    img = lay(table(), (500, 350, 1100, 750), angle=20)
    found = print_scan.find_prints(img)
    assert len(found) == 1 and not found[0].whole
    flat = print_scan.warp(img, found[0])
    # 600 x 400 drawn; the corners are found to within a few pixels, and
    # pulled in a hair so no table shows along the edge (print_scan.INSET).
    assert abs(flat.width - 600) <= 12 and abs(flat.height - 400) <= 12
    assert flat.width / flat.height == pytest.approx(1.5, abs=0.03)
    # Square-on: the cream border runs along the whole top edge.
    top = np.asarray(flat)[3, 20:-20].astype(int)
    assert (top.mean(axis=0) > 200).all()


def test_a_print_seen_at_an_angle_comes_out_rectangular():
    """Perspective: the far edge of the print is shorter than the near one."""
    img = table()
    flat_card = card(600, 400)
    import cv2
    source = np.float32([[0, 0], [600, 0], [600, 400], [0, 400]])
    target = np.float32([[560, 300], [1040, 300], [1150, 760], [450, 760]])
    matrix = cv2.getPerspectiveTransform(source, target)
    warped = cv2.warpPerspective(np.asarray(flat_card), matrix, img.size)
    mask = cv2.warpPerspective(np.full((400, 600), 255, np.uint8), matrix, img.size)
    img.paste(Image.fromarray(warped), (0, 0), Image.fromarray(mask))
    found = print_scan.find_prints(img)
    assert len(found) == 1
    flat = print_scan.warp(img, found[0])
    assert flat.width > flat.height
    centre = np.asarray(flat)[flat.height // 2, flat.width // 2]
    assert tuple(centre) == pytest.approx((210, 170, 140), abs=12)


def test_nothing_found_means_the_whole_photograph():
    plain = table()
    found = print_scan.find_prints(plain)
    assert len(found) == 1 and found[0].whole
    flat = print_scan.warp(plain, found[0])
    assert flat.size == plain.size


def test_tiny_and_long_thin_shapes_are_not_prints():
    img = table()
    draw = ImageDraw.Draw(img)
    draw.rectangle((100, 100, 140, 130), fill=(240, 240, 240))        # a stamp
    draw.rectangle((200, 900, 1500, 960), fill=(240, 240, 240))       # a ruler
    assert print_scan.find_prints(img)[0].whole


def test_detection_is_deterministic():
    img = table()
    for box in [(100, 150, 500, 450), (650, 150, 1000, 600)]:
        lay(img, box)
    assert print_scan.find_prints(img) == print_scan.find_prints(img.copy())


def test_examine_uses_the_orientation_decision_it_is_given():
    img = lay(table(), (400, 300, 1000, 700))
    seen = print_scan.examine(img, decide=lambda image, engine: 90)
    assert seen[0].rotation == 90
    # Turned a quarter: the landscape print stands as a portrait.
    assert seen[0].image.height > seen[0].image.width


def test_the_library_orientation_code_is_asked(monkeypatch):
    from ninaivu.media import upright
    asked = []

    def decide(img, **kwargs):
        asked.append(kwargs)
        return upright.Verdict(270, "model", 0.97)

    monkeypatch.setattr(upright, "decide", decide)
    assert print_scan.upright_turn(card(300, 200)) == 270
    assert asked and asked[0]["exif_orientation"] is None


def test_a_slightly_crooked_page_is_levelled():
    page = Image.new("RGB", (900, 700), (245, 245, 245))
    draw = ImageDraw.Draw(page)
    for y in range(120, 620, 100):
        draw.line([(80, y), (820, y - 39)], fill=(20, 20, 20), width=3)   # about 3 degrees
    angle = print_scan.skew_angle(page)
    assert 2.0 <= angle <= 4.0
    level = print_scan.deskew(page, angle)
    assert level.width < page.width and level.height < page.height
    assert print_scan.skew_angle(Image.new("RGB", (400, 300), (200, 200, 200))) == 0.0


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def test_a_faded_print_gets_its_blacks_and_whites_back():
    ramp = np.tile(np.linspace(100, 150, 256), (64, 1))
    faded = Image.fromarray(np.dstack([ramp, ramp, ramp]).astype(np.uint8))
    fixed = np.asarray(print_scan.fix_colours(faded))
    assert fixed.dtype == np.uint8
    # Most of the way to black and white (print_scan.STRENGTH), from 100-150.
    assert fixed.min() <= 20 and fixed.max() >= 235


def test_a_red_cast_is_neutralised_and_values_stay_in_range():
    rng = np.random.default_rng(1)
    base = rng.integers(40, 200, size=(120, 160, 1)).astype(float)
    cast = np.concatenate([base + 50, base, base * 0.8], axis=2)
    fixed = np.asarray(print_scan.fix_colours(Image.fromarray(np.clip(cast, 0, 255).astype(np.uint8))))
    means = fixed.reshape(-1, 3).mean(axis=0)
    before = np.clip(cast, 0, 255).reshape(-1, 3).mean(axis=0)
    assert np.ptp(means) < np.ptp(before) / 3
    assert fixed.min() >= 0 and fixed.max() <= 255


def test_a_blank_card_is_left_alone():
    blank = Image.new("RGB", (100, 80), (180, 170, 160))
    assert np.array_equal(np.asarray(print_scan.fix_colours(blank)), np.asarray(blank))


def test_a_sepia_print_keeps_its_tone():
    ramp = np.tile(np.linspace(80, 170, 256), (40, 1))
    sepia = np.dstack([ramp * 1.06, ramp, ramp * 0.92]).clip(0, 255).astype(np.uint8)
    fixed = np.asarray(print_scan.fix_colours(Image.fromarray(sepia))).astype(int)
    middle = fixed[:, 128]
    assert (middle[:, 0] >= middle[:, 2]).all()


# ---------------------------------------------------------------------------
# The decade
# ---------------------------------------------------------------------------

class FakeEngine:
    """Speaks the picture-search model's interface; every print looks 1970s."""

    semantic = True

    def __init__(self, decade=1970, flat=False):
        self.decade, self.flat = decade, flat

    def embed_texts(self, texts):
        out = np.zeros((len(texts), 16), np.float32)
        for row, text in enumerate(texts):
            decade = next(d for d in print_scan.DECADES if f"{d}s" in text)
            out[row, print_scan.DECADES.index(decade)] = 1.0
        return out

    def embed_images(self, images):
        if self.flat:
            return np.ones((len(images), 16), np.float32)
        out = np.zeros((len(images), 16), np.float32)
        out[:, print_scan.DECADES.index(self.decade)] = 1.0
        out[:, 14] = 2.0
        return out


def test_the_decade_is_guessed_with_the_search_model():
    guess = print_scan.guess_decade([card(200, 150)] * 2, FakeEngine(1970))
    assert guess["decade"] == 1970 and 0.9 < guess["confidence"] <= 1.0


def test_no_guess_without_the_model_or_when_it_shrugs():
    assert print_scan.guess_decade([card(200, 150)], None) is None
    assert print_scan.guess_decade([card(200, 150)], object()) is None
    assert print_scan.guess_decade([card(200, 150)], FakeEngine(flat=True)) is None


def test_the_prompts_name_every_decade_from_1900_to_the_2010s():
    assert print_scan.DECADES[0] == 1900 and print_scan.DECADES[-1] == 2010


@pytest.mark.parametrize("text,label,key", [
    ("", "Unknown year", ""),
    ("1970s", "1970s", "1970-01-01"),
    ("1975", "1975", "1975-01-01"),
    ("1975-06", "1975", "1975-06-01"),
    ("1975-06-12", "1975", "1975-06-12"),
])
def test_a_date_is_a_year_a_decade_or_a_day(text, label, key):
    when = print_scan.parse_when(text)
    assert when.label == label and when.date_key == key


@pytest.mark.parametrize("text", ["1975s", "75", "1975-13", "௧௯௭௫", "1800", "../1975",
                                  f"{datetime.now().year + 1}", "1975-02-30"])
def test_anything_else_is_refused(text):
    with pytest.raises(ValueError):
        print_scan.parse_when(text)


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

def _two_prints() -> bytes:
    img = table()
    lay(img, (150, 200, 650, 550))
    lay(img, (850, 300, 1400, 1000))
    return jpeg(img)


def _detect(client, *photos, names=None):
    names = names or [f"IMG_{i}.jpg" for i in range(len(photos))]
    data = {"photos": [(io.BytesIO(p), n) for p, n in zip(photos, names)]}
    return client.post("/api/prints/detect", data=data, content_type="multipart/form-data")


def _finish(client, response):
    assert response.status_code == 202, response.get_json()
    job = response.get_json()["id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        state = client.get(f"/api/prints/jobs/{job}").get_json()
        if state["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert state["state"] == "done", state
    return client.get(f"/api/prints/jobs/{job}/result").get_json()


@pytest.fixture(autouse=True)
def _no_jobs_left_over():
    jobs.reset()
    yield
    jobs.reset()


def _save(client, session, picks, **options):
    return client.post("/api/prints/save", json={"session": session, "picks": picks, **options})


def test_a_guest_may_not_scan(as_guest):
    assert _detect(as_guest, _two_prints()).status_code in (401, 403)
    assert as_guest.get("/api/prints/capabilities").status_code in (401, 403)
    assert as_guest.post("/api/prints/save", json={}).status_code in (401, 403)


def test_the_capabilities_say_what_is_not_installed(as_family, as_admin):
    caps = as_family.get("/api/prints/capabilities").get_json()
    assert caps["restore"] is False and caps["upscale"] is False
    assert caps["year_guess"] is False and caps["needs_review"] is True
    assert as_admin.get("/api/prints/capabilities").get_json()["needs_review"] is False


def test_an_administrators_prints_go_straight_into_the_library(as_admin, scanned):
    cfg, conn, _ = scanned
    found = _detect(as_admin, _two_prints()).get_json()
    assert [len(p["prints"]) for p in found["photos"]] == [2]
    assert found["photos"][0]["prints"][0]["thumb"].startswith("data:image/jpeg;base64,")
    assert found["guess"] is None
    result = _finish(as_admin, _save(as_admin, found["session"], [[0, 0], [0, 1]],
                                     date="1975-06-12"))
    assert result["pending_approval"] is False and not result["failed"]
    assert result["folder"] == "Scanned prints/1975"
    root = Path(cfg.active_root)
    folder = root / "Scanned prints" / "1975"
    assert sorted(p.name for p in folder.iterdir()) == ["Scanned print 1.jpg", "Scanned print 2.jpg"]
    with Image.open(folder / "Scanned print 2.jpg") as saved:
        assert saved.width > saved.height * 0.6
        assert saved.getexif().get_ifd(0x8769)[0x9003] == "1975:06:12 00:00:00"
    rows = conn.execute("SELECT * FROM assets WHERE folder='Scanned prints/1975' "
                        "ORDER BY filename").fetchall()
    assert [r["date_key"] for r in rows] == ["1975-06-12", "1975-06-12"]
    assert {r["date_source"] for r in rows} == {"manual"}
    assert {r["rot_source"] for r in rows} == {"manual"}
    assert rows[0]["thumb"]
    # The phone's own photograph is kept beside them.
    assert (root / "Scanned prints" / "originals" / "IMG_0.jpg").is_file()
    # The batch is cleared away once it is filed.
    assert not any(print_scan.sessions_dir(cfg).iterdir())


def test_nothing_is_ever_overwritten(as_admin, scanned):
    cfg, _, _ = scanned
    folder = Path(cfg.active_root) / "Scanned prints" / "Unknown year"
    folder.mkdir(parents=True)
    (folder / "Scanned print 1.jpg").write_bytes(b"somebody else's")
    originals = Path(cfg.active_root) / "Scanned prints" / "originals"
    originals.mkdir(parents=True)
    (originals / "IMG_0.jpg").write_bytes(b"an earlier original")
    found = _detect(as_admin, _two_prints()).get_json()
    result = _finish(as_admin, _save(as_admin, found["session"], [[0, 1]]))
    assert (folder / "Scanned print 1.jpg").read_bytes() == b"somebody else's"
    assert (folder / "Scanned print 2.jpg").is_file()
    assert (originals / "IMG_0.jpg").read_bytes() == b"an earlier original"
    assert (originals / "IMG_0 1.jpg").is_file()
    assert result["saved"][0]["filename"] == "Scanned print 2.jpg"


def test_an_undated_print_stays_undated_through_a_rescan(as_admin, scanned):
    cfg, conn, scanner = scanned
    found = _detect(as_admin, _two_prints()).get_json()
    _finish(as_admin, _save(as_admin, found["session"], [[0, 0]], keep_original=False))
    row = conn.execute("SELECT * FROM assets WHERE folder='Scanned prints/Unknown year'").fetchone()
    assert row["captured_at"] is None and row["date_source"] == "manual"
    scanner._run(Path(cfg.active_root), full=True)
    row = db.get_asset(conn, row["id"])
    assert row["captured_at"] is None and row["date_source"] == "manual"


def test_the_original_is_not_kept_when_unticked(as_admin, scanned):
    cfg, _, _ = scanned
    found = _detect(as_admin, _two_prints()).get_json()
    result = _finish(as_admin, _save(as_admin, found["session"], [[0, 0]],
                                     keep_original=False, date="1970s"))
    assert result["folder"] == "Scanned prints/1970s"
    assert not (Path(cfg.active_root) / "Scanned prints" / "originals").exists()


def test_a_family_members_prints_wait_for_an_administrator(as_family, as_admin, scanned):
    cfg, conn, _ = scanned
    found = _detect(as_family, _two_prints()).get_json()
    result = _finish(as_family, _save(as_family, found["session"], [[0, 0], [0, 1]],
                                      date="1982"))
    assert result["pending_approval"] is True
    assert not (Path(cfg.active_root) / "Scanned prints").exists()
    pending = [s for s in result["saved"]]
    assert {s["status"] for s in pending} == {"pending"}
    rows = conn.execute("SELECT * FROM pending_uploads ORDER BY id").fetchall()
    places = [json.loads(r["record"])["_place"] for r in rows]
    assert places == ["Scanned prints/1982", "Scanned prints/1982", "Scanned prints/originals"]

    approved = as_admin.post(f"/api/admin/uploads/{pending[0]['pending_id']}/approve", json={})
    assert approved.status_code == 200, approved.get_json()
    item = db.get_asset(conn, approved.get_json()["item"]["id"])
    assert item["folder"] == "Scanned prints/1982" and item["date_key"] == "1982-01-01"
    assert item["date_source"] == "manual"
    assert (Path(cfg.active_root) / item["rel_path"]).is_file()


def test_a_batch_belongs_to_the_person_who_sent_it(as_family, as_admin):
    found = _detect(as_admin, _two_prints()).get_json()
    assert _save(as_family, found["session"], [[0, 0]]).status_code == 404
    assert _save(as_admin, "0" * 32, [[0, 0]]).status_code == 404
    assert _save(as_admin, "../../etc", [[0, 0]]).status_code == 404


def test_a_bad_request_is_refused_before_anything_runs(as_admin):
    found = _detect(as_admin, _two_prints()).get_json()
    session = found["session"]
    assert _save(as_admin, session, []).status_code == 400
    assert _save(as_admin, session, [[0, 7]]).status_code == 400
    assert _save(as_admin, session, [["0", 0]]).status_code == 400
    assert _save(as_admin, session, [[0, 0]], date="next year").status_code == 400
    assert _save(as_admin, session, [[0, 0]], upscale=3).status_code == 400
    assert as_admin.post("/api/prints/save", json=[1]).status_code == 400


def test_detect_refuses_what_is_not_a_photograph_and_too_many(as_admin, monkeypatch):
    assert _detect(as_admin, b"<html>", names=["page.html"]).status_code == 400
    assert _detect(as_admin, b"not a jpeg at all", names=["x.jpg"]).status_code == 400
    assert as_admin.post("/api/prints/detect", data={},
                         content_type="multipart/form-data").status_code == 400
    monkeypatch.setattr(print_scan, "MAX_PHOTOS", 1)
    assert _detect(as_admin, _two_prints(), _two_prints()).status_code == 400


def test_detect_refuses_a_photograph_over_the_size_limit(as_admin, monkeypatch, scanned):
    cfg, _, _ = scanned
    monkeypatch.setattr(print_scan, "MAX_PHOTO_BYTES", 1000)
    assert _detect(as_admin, _two_prints()).status_code == 413
    # And the half-written batch is not left behind.
    assert not any(print_scan.sessions_dir(cfg).iterdir())


def test_the_decade_guess_reaches_the_screen(as_admin, app, monkeypatch):
    # Each request reads the engine from the services, as search does.
    monkeypatch.setattr(app.config["MV_SERVICES"], "engine", FakeEngine(1960))
    found = _detect(as_admin, _two_prints()).get_json()
    assert found["guess"]["decade"] == 1960
    assert found["capabilities"]["year_guess"] is True


def test_installed_tools_are_used_and_missing_ones_are_skipped(as_admin, monkeypatch):
    calls = []
    monkeypatch.setattr(print_scan, "tools_available", lambda: {"restore": True, "upscale": True})
    monkeypatch.setattr(print_scan, "restore_faces", lambda img: calls.append("restore") or img)
    monkeypatch.setattr(print_scan, "upscale",
                        lambda img, factor: calls.append(f"x{factor}") or img.resize(
                            (img.width * factor, img.height * factor)))
    found = _detect(as_admin, _two_prints()).get_json()
    width = found["photos"][0]["prints"][0]["width"]
    result = _finish(as_admin, _save(as_admin, found["session"], [[0, 0]], restore_faces=True,
                                     upscale=2, keep_original=False))
    assert calls == ["restore", "x2"]
    assert not result["failed"] and width


def test_old_batches_are_swept_away(scanned):
    cfg, _, _ = scanned
    token, folder = print_scan.new_session(cfg, 1)
    print_scan.sweep(cfg, now=time.time() + print_scan.KEEP_SESSION + 10)
    assert not folder.exists()
    assert print_scan.load_session(cfg, token, 1) is None


def test_scans_are_family_routes_not_library_management(app):
    """Under /api/prints, not /api/scan, which is the console's alone."""
    paths = {str(rule) for rule in app.url_map.iter_rules()}
    assert "/api/prints/detect" in paths and "/api/prints/save" in paths
    assert not [p for p in paths if p.startswith("/api/scan") and "print" in p]


def test_staging_takes_the_review_path(scanned):
    """The family path is upload_review.stage, which is what refuses oversized
    pictures and keeps the file out of the library until approval."""
    cfg, conn, _ = scanned
    saver = print_scan.Saver(conn, cfg, cfg.active_root, None, 99, False)
    out = saver.print_(print_scan.encode(card(300, 200), print_scan.parse_when("1990")),
                       print_scan.parse_when("1990"))
    assert out["status"] == "pending"
    staged = upload_review.get(conn, out["pending_id"])
    assert staged["filename"] == "Scanned print 1.jpg"
    record = json.loads(staged["record"])
    assert record["date_key"] == "1990-01-01" and record["date_source"] == "manual"
