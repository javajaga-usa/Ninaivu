"""Focus and exposure: the measurement, the flags, and what they reach.

The measurement has to mean the same thing for a photograph off a phone and
one off a scanner, and it has to stay quiet about pictures it cannot judge.
Both are easier to get wrong than to notice, so they are tested here rather
than left to the eye.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image, ImageFilter

from ninaivu.media import media
from ninaivu.storage import db

DATA = Path(__file__).parent / "data"

THRESHOLDS = dict(blur_below=45.0, dark_below=0.16, bright_above=0.86,
                  clip_above=0.12, min_pixels=640 * 480)


def flags(img, **overrides):
    settings = {**THRESHOLDS, **overrides}
    return media.quality_flags(media.quality_stats(img), *img.size, **settings)


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------

def test_blurring_a_photograph_lowers_its_sharpness():
    sharp = Image.open(DATA / "face.jpg")
    soft = sharp.filter(ImageFilter.GaussianBlur(4))
    assert media.quality_stats(soft)["sharpness"] < \
        media.quality_stats(sharp)["sharpness"]


def test_sharpness_survives_a_change_of_resolution():
    """The same scene at three sizes must land near the same number.

    This is the whole reason the measurement downscales first. Without it a
    single blur threshold would flag every photograph from the older camera
    in the library and none from the newer one.
    """
    full = Image.open(DATA / "portrait.jpg")
    scores = [
        media.quality_stats(full.resize((full.width // n, full.height // n)))["sharpness"]
        for n in (1, 2, 4)
    ]
    spread = (max(scores) - min(scores)) / max(scores)
    assert spread < 0.25, scores


def test_a_flat_field_is_not_called_blurry():
    """A wall, a sky, a blank page: no detail to be out of focus.

    Laplacian variance reads zero for all of them, exactly as it does for a
    badly soft photograph, so the contrast guard is what keeps Ninaivu from
    telling somebody their picture of fog is a mistake.
    """
    assert "blurry" not in flags(Image.new("RGB", (900, 700), (20, 90, 200)))


def test_a_genuinely_soft_photograph_is_flagged():
    soft = Image.open(DATA / "face.jpg").filter(ImageFilter.GaussianBlur(4))
    assert "blurry" in flags(soft)


def test_a_sharp_photograph_is_left_alone():
    assert "blurry" not in flags(Image.open(DATA / "face.jpg"))


@pytest.mark.parametrize("shade, expected", [
    ((6, 6, 6), "dark"),
    ((252, 252, 252), "bright"),
])
def test_exposure_extremes_are_named(shade, expected):
    assert expected in flags(Image.new("RGB", (900, 700), shade))


def test_blown_highlights_are_called_out_separately():
    assert "clipped" in flags(Image.new("RGB", (900, 700), (252, 252, 252)))


def test_a_small_picture_is_flagged_low_resolution():
    assert "lowres" in flags(Image.new("RGB", (320, 240), (120, 130, 140)))
    assert "lowres" not in flags(Image.new("RGB", (1600, 1200), (120, 130, 140)))


def test_nothing_is_claimed_when_nothing_was_measured():
    """No measurement, no verdict — not a default of "fine" or "bad"."""
    assert media.quality_flags({}, 1600, 1200, **THRESHOLDS) == []


# ---------------------------------------------------------------------------
# What the scan stores
# ---------------------------------------------------------------------------

def test_the_scan_records_focus_and_exposure(scanned):
    _, conn, _ = scanned
    row = conn.execute(
        "SELECT sharpness, brightness, quality FROM assets "
        "WHERE kind='picture' AND thumb IS NOT NULL LIMIT 1"
    ).fetchone()
    assert row["sharpness"] is not None
    assert 0.0 <= row["brightness"] <= 1.0
    assert isinstance(json.loads(row["quality"]), list)


def test_quality_scan_can_be_turned_off(cfg):
    from ninaivu.media.scanner import Scanner
    from ninaivu.server import auth

    cfg.quality_scan = False
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    Scanner(cfg)._run(Path(cfg.active_root), full=True)
    rows = conn.execute(
        "SELECT sharpness FROM assets WHERE kind='picture'").fetchall()
    assert rows and all(r["sharpness"] is None for r in rows)


# ---------------------------------------------------------------------------
# The schema, for a library that was indexed before any of this existed
# ---------------------------------------------------------------------------

def test_an_older_database_gains_the_columns_without_a_rescan(tmp_path):
    """The columns arrive through heal_schema, not a version bump.

    A household upgrading Ninaivu should not have to re-index a library of
    forty thousand photographs to open the app again.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE assets (
        id INTEGER PRIMARY KEY, root TEXT NOT NULL, rel_path TEXT NOT NULL,
        filename TEXT NOT NULL, kind TEXT NOT NULL, UNIQUE(root, rel_path))""")
    conn.execute("INSERT INTO assets(root, rel_path, filename, kind) "
                 "VALUES ('r', 'a.jpg', 'a.jpg', 'image')")
    conn.commit()
    conn.close()

    healed = db.init_db(path)
    have = db.columns(healed, "assets")
    assert {"sharpness", "brightness", "quality"} <= set(have)
    row = healed.execute("SELECT quality FROM assets").fetchone()
    assert row["quality"] == "[]"


# ---------------------------------------------------------------------------
# Picking the keeper out of a near-identical group
# ---------------------------------------------------------------------------

def test_the_sharpest_frame_wins():
    items = [
        {"id": 1, "sharpness": 40.0, "width": 4000, "height": 3000, "size": 900},
        {"id": 2, "sharpness": 900.0, "width": 4000, "height": 3000, "size": 800},
    ]
    assert db.best_of(items) == 2


def test_resolution_breaks_a_tie_on_focus():
    items = [
        {"id": 1, "sharpness": 500.0, "width": 1600, "height": 1200, "size": 900},
        {"id": 2, "sharpness": 500.0, "width": 4000, "height": 3000, "size": 800},
    ]
    assert db.best_of(items) == 2


def test_an_unmeasured_frame_loses_to_a_measured_one():
    items = [
        {"id": 1, "sharpness": None, "width": 4000, "height": 3000, "size": 999},
        {"id": 2, "sharpness": 1.0, "width": 100, "height": 100, "size": 1},
    ]
    assert db.best_of(items) == 2


def test_a_persons_own_choice_outranks_the_measurement():
    """A favourite is a decision. Ninaivu does not know better."""
    items = [
        {"id": 1, "sharpness": 10.0, "width": 100, "height": 100, "size": 1,
         "favorite": True},
        {"id": 2, "sharpness": 900.0, "width": 4000, "height": 3000, "size": 999},
    ]
    assert db.best_of(items) == 1


def test_an_empty_group_has_no_keeper():
    assert db.best_of([]) is None


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

def test_assets_carry_their_flags(as_family):
    items = as_family.get("/api/assets?limit=5").get_json()["items"]
    assert items
    assert all(isinstance(item["quality"], list) for item in items)


def test_browsing_can_be_narrowed_to_one_flag(as_family, scanned):
    _, conn, _ = scanned
    conn.execute("UPDATE assets SET quality='[\"blurry\"]' WHERE id = "
                 "(SELECT MIN(id) FROM assets WHERE kind='picture')")
    conn.commit()
    wanted = conn.execute(
        "SELECT MIN(id) AS id FROM assets WHERE kind='picture'").fetchone()["id"]

    items = as_family.get("/api/assets?quality=blurry&limit=200").get_json()["items"]
    assert [i["id"] for i in items] == [wanted]


def test_an_unknown_flag_is_ignored_rather_than_matched(as_family):
    """Unrecognised values must not become a LIKE pattern of their own."""
    every = as_family.get("/api/assets?limit=200").get_json()["items"]
    asked = as_family.get("/api/assets?quality=%25&limit=200").get_json()["items"]
    assert len(asked) == len(every)


def test_the_duplicate_view_names_the_keeper(as_family, scanned):
    groups = as_family.get("/api/duplicates").get_json()["groups"]
    assert groups, "the fixture library contains a copied shot"
    for group in groups:
        assert group["best"] in [item["id"] for item in group["items"]]


# ---------------------------------------------------------------------------
# Re-scoring a library judged by older rules
# ---------------------------------------------------------------------------

def test_the_scan_stamps_the_rules_it_judged_by(scanned):
    _, conn, _ = scanned
    rows = conn.execute(
        "SELECT quality_version FROM assets "
        "WHERE kind='picture' AND thumb IS NOT NULL").fetchall()
    assert rows
    assert all(r["quality_version"] == media.QUALITY_VERSION for r in rows)


def test_photographs_judged_by_older_rules_are_re_scored(scanned):
    """A moved threshold has to reach the whole library, not just new files.

    Otherwise a household ends up with two vocabularies in one grid and no
    way to tell which frame was judged by which.
    """
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET quality_version=0, sharpness=NULL, "
                 "brightness=NULL, quality='[]' WHERE kind='picture'")
    conn.commit()

    Scanner(cfg)._rescore_quality(conn, cfg.active_root)

    rows = conn.execute(
        "SELECT sharpness, quality_version FROM assets "
        "WHERE kind='picture' AND thumb IS NOT NULL").fetchall()
    assert rows
    assert all(r["quality_version"] == media.QUALITY_VERSION for r in rows)
    assert any(r["sharpness"] is not None for r in rows)


def test_re_scoring_leaves_current_photographs_alone(scanned):
    """The version guard is what keeps every scan from redoing the library."""
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET sharpness=-1 WHERE kind='picture'")
    conn.commit()
    Scanner(cfg)._rescore_quality(conn, cfg.active_root)
    untouched = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE sharpness=-1").fetchone()["n"]
    assert untouched > 0, "already at the current version: nothing to redo"


def test_re_scoring_is_skipped_when_the_measurement_is_off(scanned):
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.quality_scan = False
    conn.execute("UPDATE assets SET quality_version=0, sharpness=-1")
    conn.commit()
    Scanner(cfg)._rescore_quality(conn, cfg.active_root)
    still = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE sharpness=-1").fetchone()["n"]
    assert still > 0
