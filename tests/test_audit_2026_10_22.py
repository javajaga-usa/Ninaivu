"""Ninth audit (2026-10-22): what main gained after 2.0.0.

* A screenshot the model called explicit loses the flag only when it is
  hidden instead; one shown to the family file by file keeps it.
* A page that joins a drive count under way is answered within seconds
  ("waiting") instead of holding one of the console's few request threads
  for the whole count.
"""

import threading
import time

from ninaivu.api import archive_api
from ninaivu.storage import db


def ids_in(conn, folder_name):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE folder=? ORDER BY id", (folder_name,))]


# --- screenshots and documents ----------------------------------------------

def test_a_screenshot_shown_to_the_family_by_hand_keeps_its_explicit_flag(scanned, people):
    from PIL import Image
    from pathlib import Path
    cfg, conn, scanner = scanned
    shots = Path(cfg.active_root) / "phone" / "Screenshots"
    shots.mkdir(parents=True)
    Image.new("RGB", (300, 600), (240, 240, 240)).save(shots / "Screenshot_20240607_Chat.png")
    scanner._run(Path(cfg.active_root), full=False)
    shot = conn.execute("SELECT id FROM assets WHERE filename LIKE 'Screenshot_%'").fetchone()[0]
    # Shown to the family file by file, then flagged by the model.
    conn.execute("UPDATE assets SET visibility=1, vis_source='item', nsfw=1, "
                 "nsfw_source='auto' WHERE id=?", (shot,))
    conn.commit()
    assert db.screens_not_explicit(conn, cfg.active_root) == 0
    row = conn.execute("SELECT nsfw, visibility FROM assets WHERE id=?", (shot,)).fetchone()
    assert (row["nsfw"], row["visibility"]) == (1, 1)
    # From the default, it is hidden instead and loses the flag, as before.
    conn.execute("UPDATE assets SET vis_source='default' WHERE id=?", (shot,))
    conn.commit()
    assert db.screens_not_explicit(conn, cfg.active_root) == 1
    row = conn.execute("SELECT nsfw, visibility FROM assets WHERE id=?", (shot,)).fetchone()
    assert (row["nsfw"], row["visibility"]) == (0, 2)


# --- a count joined by another page -----------------------------------------

def test_a_page_joining_a_count_under_way_is_answered_within_seconds(
        app, as_admin, tmp_path, monkeypatch):
    source = tmp_path / "card"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    destination = tmp_path / "archive"
    destination.mkdir()
    monkeypatch.setattr(archive_api, "JOIN_WAIT_SECONDS", 0.3)
    form = {"source_dirs": [str(source)], "destination_dir": str(destination)}
    with app.app_context():
        probe = archive_api.ArchiveJob([str(source)], str(destination),
                                       media_types=archive_api.VALID_MEDIA_TYPES,
                                       vision=archive_api._vision())
    signature = archive_api.archive_scanner._count_signature(probe)
    archive_api._estimate_begin("someone-else", signature, watched=False)
    try:
        started = time.monotonic()
        answer = as_admin.post("/api/archive/capacity",
                               json={**form, "progress_token": "this-page", "peek": True})
        assert time.monotonic() - started < 5
        assert answer.status_code == 200
        assert answer.get_json().get("waiting") is True
        # Once the walk ends, asking again picks up its answer.
        result = {"ok": True, "files": 1, "bytes": 103}
        threading.Timer(0.1, lambda: archive_api._estimate_end(
            "someone-else", files=1, size=103, result=result)).start()
        again = as_admin.post("/api/archive/capacity",
                              json={**form, "progress_token": "this-page", "peek": True})
        assert again.get_json().get("files") == 1
    finally:
        archive_api._estimate_end("someone-else", files=0, size=0, cancelled=True)


# --- a switch for describing photographs ------------------------------------

def test_describing_photographs_has_a_switch_of_its_own():
    from types import SimpleNamespace
    from ninaivu.runtime.describer import Describer
    from ninaivu.server.config import Config
    from ninaivu.server.settings_groups import GROUPS
    assert Config().describe_photos is True
    assert "describe_photos" in GROUPS["Advanced"]
    on = Describer(SimpleNamespace(ai_runtime=True, describe_photos=True), lambda: None,
                   host=lambda: None)
    off = Describer(SimpleNamespace(ai_runtime=True, describe_photos=False), lambda: None,
                    host=lambda: None)
    assert on.enabled and not off.enabled
