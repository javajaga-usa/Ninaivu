"""What the cloud backup sends: everything in the library except hidden items.

Hidden means admin-only, and by default it does not go — hiding something from
your own household is not consent to put it on Google. A picture that has not
yet been checked for being a screenshot or a document waits for that check,
because the check is what hides it.

But hidden also means every sound file (call recordings, voice notes) and the
screenshots and documents Ninaivu hides by itself: 25,000 files, on the library
this was found on, with no copy anywhere else. So ``cloud_hidden`` can send
them: "never" (the default), "encrypted" (only while the backup is encrypted
with a key that is there), or "always".
"""

import pytest

from ninaivu.cloud import service as cloud_service
from ninaivu.cloud import store
from ninaivu.media import screens
from ninaivu.storage import db

MODEL = "ViT-B-32/laion2b_s34b_b79k"


@pytest.fixture()
def service(cfg, scanned, monkeypatch):
    # Which model is in play decides whether a picture can ever be checked.
    # Another test may have left one loaded, so this says: none loaded, and
    # the library was analysed by a model with a measured threshold.
    monkeypatch.setattr("ninaivu.ai.get_engine", lambda: None)
    _, conn, _ = scanned
    cfg.cloud_enabled = True
    cfg.hide_screens = True
    cfg.ai_enabled = True
    db.set_meta(conn, "ai_model_id", MODEL)
    conn.execute("UPDATE assets SET screen_version=?", (screens.SCREEN_VERSION,))
    conn.commit()
    store.init_schema(conn)
    return cloud_service.CloudService(cfg, lambda: db.connect(cfg.db_path)), conn


def queued(conn):
    return {r["rel_path"] for r in conn.execute(
        "SELECT rel_path FROM cloud_uploads WHERE state='pending'")}


def test_everything_visible_is_queued(service):
    backup, conn = service
    backup.queue_library()
    assert queued(conn) == {r["rel_path"] for r in conn.execute(
        "SELECT rel_path FROM assets WHERE trashed=0")}


def test_a_hidden_item_is_not_queued(service):
    backup, conn = service
    hidden = conn.execute("SELECT id, rel_path FROM assets LIMIT 1").fetchone()
    db.set_visibility(conn, [hidden["id"]], 2, source="item", record_undo=False)
    backup.queue_library()
    assert hidden["rel_path"] not in queued(conn)


def test_a_picture_waiting_for_the_screenshot_check_is_not_queued(service):
    backup, conn = service
    waiting = conn.execute("SELECT id, root, rel_path FROM assets "
                           "WHERE kind='picture' AND thumb IS NOT NULL LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET screen_version=0 WHERE id=?", (waiting["id"],))
    conn.commit()
    assert backup._awaiting_check(waiting["root"], waiting["rel_path"]) is True
    backup.queue_library()
    assert waiting["rel_path"] not in queued(conn)

    # Checked, and not a screenshot: it goes like anything else.
    conn.execute("UPDATE assets SET screen_version=? WHERE id=?",
                 (screens.SCREEN_VERSION, waiting["id"]))
    conn.commit()
    backup.queue_library()
    assert waiting["rel_path"] in queued(conn)


def test_with_the_check_switched_off_nothing_waits_for_it(service):
    backup, conn = service
    backup.cfg.hide_screens = False
    row = conn.execute("SELECT id, root, rel_path FROM assets "
                       "WHERE kind='picture' AND thumb IS NOT NULL LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET screen_version=0 WHERE id=?", (row["id"],))
    conn.commit()
    assert backup._awaiting_check(row["root"], row["rel_path"]) is False
    backup.queue_library()
    assert row["rel_path"] in queued(conn)


def test_a_model_nobody_measured_does_not_hold_pictures_for_ever(service):
    """Nothing will ever set a screen score for it, so nothing waits."""
    backup, conn = service
    db.set_meta(conn, "ai_model_id", "some/unmeasured-model")
    conn.commit()
    row = conn.execute("SELECT id, root, rel_path FROM assets "
                       "WHERE kind='picture' AND thumb IS NOT NULL LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET screen_version=0 WHERE id=?", (row["id"],))
    conn.commit()
    assert backup._awaiting_check(row["root"], row["rel_path"]) is False
    backup.queue_library()
    assert row["rel_path"] in queued(conn)


def test_a_video_is_never_held_for_the_picture_check(service, cfg, library):
    backup, conn = service
    conn.execute("INSERT INTO assets(root, rel_path, filename, folder, ext, kind, "
                 "size, mtime, date_key, date_source, thumb, screen_version) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                 (str(cfg.active_root), "clips/birthday.mp4", "birthday.mp4",
                  "clips", "mp4", "video", 10, 0, "2024-01-01", "mtime", "t"))
    conn.commit()
    assert backup._awaiting_check(str(cfg.active_root), "clips/birthday.mp4") is False


def test_the_console_cannot_ask_for_hidden_items(as_admin, cfg):
    """The setting is gone; sending it changes nothing."""
    response = as_admin.post("/api/cloud/settings", json={"include_hidden": True})
    assert response.status_code == 200
    assert "include_hidden" not in response.get_json()
    assert not hasattr(cfg, "cloud_include_hidden")


# --- cloud_hidden -----------------------------------------------------------------

@pytest.mark.parametrize("mode, encrypt, key, sends", [
    ("never", True, True, False),
    ("always", False, False, True),
    ("encrypted", False, True, False),
    ("encrypted", True, False, False),     # on, but the key is gone: not as it is
    ("encrypted", True, True, True),
])
def test_when_hidden_things_go(service, monkeypatch, mode, encrypt, key, sends):
    backup, _ = service
    backup.cfg.cloud_hidden, backup.cfg.cloud_encrypt = mode, encrypt
    monkeypatch.setattr(cloud_service.keyring, "load",
                        lambda state_dir: {"key_id": "k"} if key else None)
    assert backup.sends_hidden() is sends


def test_when_they_may_go_hidden_items_are_queued_and_nothing_waits(service):
    backup, conn = service
    backup.cfg.cloud_hidden = "always"
    hidden = conn.execute("SELECT id, root, rel_path FROM assets "
                          "WHERE kind='picture' AND thumb IS NOT NULL LIMIT 1").fetchone()
    db.set_visibility(conn, [hidden["id"]], 2, source="item", record_undo=False)
    conn.execute("UPDATE assets SET screen_version=0")
    conn.commit()
    assert backup._awaiting_check(hidden["root"], hidden["rel_path"]) is False
    backup.queue_library()
    assert hidden["rel_path"] in queued(conn)


def test_the_engine_asks_before_keeping_a_hidden_item_back():
    from ninaivu.cloud.engine import SyncEngine

    allowed = {"now": False}
    engine = SyncEngine(connect=lambda: None, open_db=lambda: None,
                        visibility_of=lambda root, rel: 2,
                        send_hidden=lambda: allowed["now"])
    assert engine._is_hidden("r", "a.m4a") is True
    allowed["now"] = True
    assert engine._is_hidden("r", "a.m4a") is False


def test_the_console_sets_it_and_refuses_anything_else(as_admin, app):
    ok = as_admin.post("/api/cloud/settings", json={"hidden": "encrypted"})
    assert ok.status_code == 200, ok.get_json()
    assert app.config["MV_CONFIG"].cloud_hidden == "encrypted"
    assert ok.get_json()["hidden"] == "encrypted" and ok.get_json()["hidden_now"] is False
    assert as_admin.post("/api/cloud/settings", json={"hidden": "sometimes"}).status_code == 400
