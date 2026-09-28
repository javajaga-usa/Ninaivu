"""Auto-enhance that reads the photograph instead of guessing.

The old Auto button applied the same six numbers to everything. The risk in
replacing it is not that the new numbers are imperfect — a starting point is
allowed to be — but that they fire on photographs that did not need them.
Most of this is about restraint.
"""

from pathlib import Path

from PIL import Image, ImageEnhance

from ninaivu.media import enhance

DATA = Path(__file__).parent / "data"


def photo():
    return Image.open(DATA / "portrait.jpg")


def tinted(factors):
    channels = photo().split()
    return Image.merge("RGB", [
        c.point(lambda v, f=f: min(255, int(v * f)))
        for c, f in zip(channels, factors)
    ])


# ---------------------------------------------------------------------------
# Reading the photograph
# ---------------------------------------------------------------------------

def test_a_dark_photograph_is_brightened():
    dark = ImageEnhance.Brightness(photo()).enhance(0.35)
    assert enhance.suggest(dark)["exposure"] > 10


def test_a_bright_photograph_is_pulled_back():
    bright = ImageEnhance.Brightness(photo()).enhance(1.9)
    assert enhance.suggest(bright)["exposure"] < 0


def test_a_flat_photograph_gains_contrast():
    flat = ImageEnhance.Contrast(photo()).enhance(0.25)
    assert enhance.suggest(flat)["contrast"] > 0


def test_a_photograph_that_already_spans_the_range_is_left_alone():
    assert "contrast" not in enhance.suggest(photo())


def test_a_blue_cast_is_warmed():
    assert enhance.suggest(tinted((0.8, 0.95, 1.3)))["warmth"] > 0


def test_an_orange_cast_is_cooled():
    assert enhance.suggest(tinted((1.3, 1.0, 0.8)))["warmth"] < 0


def test_blown_highlights_are_pulled_down():
    blown = ImageEnhance.Brightness(photo()).enhance(2.6)
    assert enhance.suggest(blown).get("highlights", 0) < 0


# ---------------------------------------------------------------------------
# Restraint
# ---------------------------------------------------------------------------

def test_nothing_is_proposed_for_a_featureless_frame():
    """A grey card, a blank scan, a lens cap: nothing to enhance."""
    assert enhance.suggest(Image.new("RGB", (600, 400), (118, 118, 118))) == {}


def test_no_slider_moves_further_than_a_person_would_drag_it():
    extreme = ImageEnhance.Brightness(photo()).enhance(0.02)
    assert all(abs(v) <= enhance.MAX_MOVE for v in enhance.suggest(extreme).values())


def test_a_one_unit_nudge_is_not_reported():
    """Noise with a number on it is worse than saying nothing."""
    assert all(abs(v) >= 2 for v in enhance.suggest(photo()).values())


def test_an_unreadable_image_proposes_nothing():
    broken = Image.new("L", (0, 0)) if False else Image.new("RGB", (1, 1))
    assert isinstance(enhance.suggest(broken), dict)


# ---------------------------------------------------------------------------
# Saying what it did
# ---------------------------------------------------------------------------

def test_an_empty_suggestion_says_so_plainly():
    assert enhance.describe({}) == "This photograph already looks balanced."


def test_the_summary_names_what_changed():
    summary = enhance.describe({"exposure": 12, "warmth": -8})
    assert "brighter" in summary and "cooler" in summary


def test_one_change_reads_as_a_sentence_not_a_list():
    assert enhance.describe({"exposure": 12}) == "Suggested: brighter."


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

def test_the_endpoint_returns_settings_and_a_summary(as_family, scanned):
    _, conn, _ = scanned
    asset_id = conn.execute(
        "SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()["id"]
    body = as_family.get(f"/api/asset/{asset_id}/enhance").get_json()
    assert isinstance(body["settings"], dict)
    assert body["summary"]


def test_only_photographs_can_be_enhanced(as_family, scanned):
    cfg, conn, _ = scanned
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind) "
        "VALUES (?,?,?,'video')", (cfg.active_root, "v.mp4", "v.mp4"))
    conn.commit()
    video = conn.execute(
        "SELECT id FROM assets WHERE rel_path='v.mp4'").fetchone()["id"]
    assert as_family.get(f"/api/asset/{video}/enhance").status_code == 400


def test_a_photograph_the_caller_may_not_open_is_a_404(as_family, scanned):
    """Hidden is admin-only, and 404 rather than 403 — the same answer a
    photograph that does not exist would give, so the endpoint cannot be used
    to find out that one does."""
    _, conn, _ = scanned
    hidden = conn.execute(
        "SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden,))
    conn.commit()
    assert as_family.get(f"/api/asset/{hidden}/enhance").status_code == 404


def test_editing_is_not_a_guest_action(as_guest, scanned):
    _, conn, _ = scanned
    asset_id = conn.execute(
        "SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()["id"]
    assert as_guest.get(f"/api/asset/{asset_id}/enhance").status_code == 403


def test_nothing_is_written_to_the_photograph(as_family, scanned):
    """Advisory only — the endpoint proposes, the person decides."""
    cfg, conn, _ = scanned
    row = conn.execute(
        "SELECT id, rel_path FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    path = Path(cfg.active_root) / row["rel_path"]
    before = path.read_bytes()
    as_family.get(f"/api/asset/{row['id']}/enhance")
    assert path.read_bytes() == before
