"""Screenshots, documents and photographs of screens are for administrators.

By name as soon as a file is indexed; by picture once the search model has a
vector for it. Only a level nobody chose for the item is ever replaced, a
folder being opened up does not bring them back, and turning the setting off
puts every one of them back where it would otherwise be.
"""

import time

import numpy as np
import pytest
from PIL import Image

from ninaivu.media import screens
from ninaivu.media.scanner import Scanner
from ninaivu.server import auth
from ninaivu.storage import db

FAKE_MODEL = "fake/screens"


@pytest.mark.parametrize("name", [
    "Screenshot_20240607_234608_WhatsApp.jpg",
    "2024/05/23/Screenshot (67).png",
    "Screen Shot 2019-03-01 at 10.12.44.png",
    "Pictures/Screenshots/IMG_0001.jpg",
    "screen_capture_2.png",
    "Bildschirmfoto 2020-01-01 um 10.00.00.png",
    "Capture d’écran 2021-02-02.png",
])
def test_names_that_are_screenshots(name):
    assert screens.named_like_screenshot(name)


@pytest.mark.parametrize("name", [
    "2023/05/12/IMG_1234.jpg",
    "Holiday/screensaver ideas/beach.jpg",
    "Film screening night/DSC0001.jpg",
    "WP_20150604_021.jpg",
])
def test_names_that_are_not(name):
    assert not screens.named_like_screenshot(name)


def add_picture(root, rel, colour=(120, 80, 40)):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 240), colour).save(path)
    return path


def open_db(cfg):
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    return conn


def row(conn, filename):
    return dict(conn.execute(
        "SELECT id, visibility, vis_source, screen_score, screen_version "
        "FROM assets WHERE filename=?", (filename,)).fetchone())


# -- by name ------------------------------------------------------------------

def test_a_screenshot_is_hidden_when_it_is_indexed(cfg, library):
    add_picture(library, "2024/06/07/Screenshot_20240607_234608_WhatsApp.jpg")
    conn = open_db(cfg)
    Scanner(cfg)._run(library, full=True)
    shot = row(conn, "Screenshot_20240607_234608_WhatsApp.jpg")
    assert (shot["visibility"], shot["vis_source"]) == (2, "screen")
    assert row(conn, "shot0.jpg")["visibility"] == 1


def test_with_the_setting_off_it_is_left_at_family(cfg, library):
    cfg.hide_screens = False
    add_picture(library, "Screenshots/one.png")
    conn = open_db(cfg)
    Scanner(cfg)._run(library, full=True)
    assert row(conn, "one.png")["visibility"] == 1


def test_a_library_indexed_before_the_rule_is_caught_up(cfg, library):
    """A screenshot already at family level from an earlier scan."""
    add_picture(library, "Screenshots/old.png")
    cfg.hide_screens = False
    conn = open_db(cfg)
    scanner = Scanner(cfg)
    scanner._run(library, full=True)
    assert row(conn, "old.png")["visibility"] == 1

    cfg.hide_screens = True
    scanner.apply_screen_rule()
    assert row(conn, "old.png")["vis_source"] == "screen"


def test_a_family_member_cannot_see_one(cfg, library):
    add_picture(library, "Screenshots/chat.png")
    conn = open_db(cfg)
    Scanner(cfg)._run(library, full=True)
    item = row(conn, "chat.png")
    family = [a["id"] for a in db.query_assets(conn, [str(library)], max_visibility=1,
                                               limit=1000)[0]]
    admin = [a["id"] for a in db.query_assets(conn, [str(library)], max_visibility=2,
                                              limit=1000)[0]]
    assert item["id"] not in family
    assert item["id"] in admin


# -- by picture ---------------------------------------------------------------

class FakeEngine:
    """Text vectors one-hot per prompt, so a picture can be pointed at one."""

    model_id = FAKE_MODEL
    semantic = True
    name = "fake"

    def embed_texts(self, texts):
        return np.eye(len(texts), dtype=np.float32)


@pytest.fixture()
def judged(cfg, library, monkeypatch):
    """A scanned library whose pictures have vectors from a measured model."""
    monkeypatch.setitem(screens.THRESHOLDS, FAKE_MODEL, 0.8)
    conn = open_db(cfg)
    scanner = Scanner(cfg)
    scanner._run(library, full=True)
    scanner.ai = FakeEngine()
    width = len(screens.SCREEN_PROMPTS) + len(screens.PHOTO_PROMPTS)

    def point(filename, prompt_index):
        vector = np.zeros(width, dtype=np.float32)
        vector[prompt_index] = 1.0
        db.store_embedding(conn, row(conn, filename)["id"], FAKE_MODEL, width,
                           vector.tobytes())

    point("shot1.jpg", 0)                                   # a phone screenshot
    point("shot2.jpg", len(screens.SCREEN_PROMPTS))         # a family photo
    return scanner, conn


def test_a_picture_that_looks_like_a_screen_is_hidden(judged):
    scanner, conn = judged
    scanner.apply_screen_rule()
    screen, photo = row(conn, "shot1.jpg"), row(conn, "shot2.jpg")
    assert (screen["visibility"], screen["vis_source"]) == (2, "screen")
    assert screen["screen_score"] > 0.99
    assert screen["screen_version"] == screens.SCREEN_VERSION
    assert photo["visibility"] == 1
    assert photo["screen_score"] < 0.01
    # A picture with no vector has not been judged yet, and is not guessed at.
    assert row(conn, "shot3.jpg")["screen_version"] == 0


def test_a_picture_just_tagged_again_is_judged_again(judged):
    """A changed file is tagged again and gets a new vector; its old score,
    from the file as it was, must not stand."""
    scanner, conn = judged
    scanner.apply_screen_rule()
    photo = row(conn, "shot2.jpg")
    assert photo["visibility"] == 1
    width = len(screens.SCREEN_PROMPTS) + len(screens.PHOTO_PROMPTS)
    vector = np.zeros(width, dtype=np.float32)
    vector[3] = 1.0                                 # now a chat screenshot
    db.store_embedding(conn, photo["id"], FAKE_MODEL, width, vector.tobytes())
    scanner._judge_screens(conn, str(scanner.cfg.active_root), ids=[photo["id"]])
    assert row(conn, "shot2.jpg")["vis_source"] == "screen"


def test_a_model_nobody_measured_hides_nothing_by_picture(judged, monkeypatch):
    scanner, conn = judged
    monkeypatch.delitem(screens.THRESHOLDS, FAKE_MODEL)
    scanner.ai = FakeEngine()                  # no scorer cached from before
    scanner.apply_screen_rule()
    assert row(conn, "shot1.jpg")["visibility"] == 1


def test_an_admins_own_decision_is_kept(judged):
    scanner, conn = judged
    item = row(conn, "shot1.jpg")["id"]
    db.set_visibility(conn, [item], 0, source="item", record_undo=False)
    scanner.apply_screen_rule()
    assert (row(conn, "shot1.jpg")["visibility"],
            row(conn, "shot1.jpg")["vis_source"]) == (0, "item")


def test_opening_up_the_folder_does_not_show_it(judged, library):
    scanner, conn = judged
    scanner.apply_screen_rule()
    db.set_folder_visibility(conn, str(library), "", 0, record_undo=False)
    assert row(conn, "shot1.jpg")["visibility"] == 2
    assert row(conn, "shot2.jpg")["visibility"] == 0
    # Shown on its own, it is shown.
    db.set_visibility(conn, [row(conn, "shot1.jpg")["id"]], 0, source="item",
                      record_undo=False)
    assert row(conn, "shot1.jpg")["visibility"] == 0


def test_a_rescan_keeps_it_hidden(judged, library):
    scanner, conn = judged
    scanner.apply_screen_rule()
    scanner._run(library, full=True)
    assert row(conn, "shot1.jpg")["vis_source"] == "screen"


def test_turning_it_off_puts_everything_back(judged, cfg, library):
    scanner, conn = judged
    add_picture(library, "Screenshots/late.png")
    scanner._run(library, full=False)
    scanner.apply_screen_rule()
    db.set_folder_visibility(conn, str(library), "2023", 0, record_undo=False)
    assert row(conn, "late.png")["vis_source"] == "screen"
    assert row(conn, "shot1.jpg")["vis_source"] == "screen"

    cfg.hide_screens = False
    scanner.apply_screen_rule()
    # Under the public folder rule it goes back to public; elsewhere, family.
    assert (row(conn, "shot1.jpg")["visibility"],
            row(conn, "shot1.jpg")["vis_source"]) == (0, "folder")
    assert (row(conn, "late.png")["visibility"],
            row(conn, "late.png")["vis_source"]) == (1, "default")
    # The score is kept, so turning it on again judges nothing twice.
    assert row(conn, "shot1.jpg")["screen_version"] == screens.SCREEN_VERSION

    cfg.hide_screens = True
    scanner.apply_screen_rule()
    assert row(conn, "shot1.jpg")["vis_source"] == "screen"


def test_a_new_search_model_judges_again(judged, monkeypatch):
    scanner, conn = judged
    scanner.apply_screen_rule()
    db.set_meta(conn, "ai_model_id", FAKE_MODEL)
    conn.commit()

    class Other(FakeEngine):
        model_id = "fake/other"

    scanner.ai = Other()
    scanner._retag_if_the_model_changed(conn)
    assert row(conn, "shot1.jpg")["screen_version"] == 0


# -- the console ----------------------------------------------------------------

def test_the_switch_is_saved_and_reported(as_admin, app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.config["MV_SCANNER"], "apply_screen_rule",
                        lambda: calls.append(True))
    assert as_admin.get("/api/admin/overview").get_json()["app"]["hide_screens"] is True
    response = as_admin.post("/api/admin/settings", json={"hide_screens": False})
    assert response.get_json()["settings"]["hide_screens"] is False
    assert app.config["MV_CONFIG"].hide_screens is False
    for _ in range(100):
        if calls:
            break
        time.sleep(0.02)
    assert calls, "turning the switch did not apply it"
