"""Ninth audit (2026-10-22): what main gained after 2.0.0.

* An administrator signed in to the family app sees what the family sees:
  hidden and explicit files are the console's alone.
* A screenshot the model called explicit loses the flag only when it is
  hidden instead; one shown to the family file by file keeps it.
* A page that joins a drive count under way is answered within seconds
  ("waiting") instead of holding one of the console's few request threads
  for the whole count.
"""

import threading
import time

import pytest

from conftest import ADMIN, ids_of, login
from ninaivu.api import archive_api
from ninaivu.storage import db


def ids_in(conn, folder_name):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE folder=? ORDER BY id", (folder_name,))]


@pytest.fixture()
def family_app_admin(app, people):
    """The administrator, signed in to the family app of a two-port server."""
    from ninaivu import create_home_app
    home = create_home_app(app.config["MV_SERVICES"])
    return login(home.test_client(), *ADMIN)


# --- the family app keeps hidden and explicit files from administrators too --

def test_an_admin_in_the_family_app_sees_no_hidden_or_explicit_file(
        as_admin, family_app_admin, people):
    conn = people["conn"]
    secret = ids_in(conn, "private")
    assert as_admin.post("/api/visibility",
                         json={"ids": secret[:1], "visibility": "hidden"}).status_code == 200
    assert as_admin.post("/api/assets/bulk",
                         json={"ids": secret[1:2], "nsfw": True}).status_code == 200
    seen = set(ids_of(family_app_admin))
    assert secret[0] not in seen and secret[1] not in seen
    assert secret[2] in seen
    for asset_id in secret[:2]:
        assert family_app_admin.get(f"/api/preview/{asset_id}").status_code == 404
        assert family_app_admin.get(f"/api/thumb/{asset_id}?s=256").status_code == 404
    # Asked for by id, as the console's screens do, still nothing.
    listed = family_app_admin.get(f"/api/assets?ids={secret[0]},{secret[1]}").get_json()
    assert listed["items"] == []
    # The console (here the combined app) still shows both.
    for asset_id in secret[:2]:
        assert as_admin.get(f"/api/preview/{asset_id}").status_code == 200


def test_an_admin_in_the_family_app_can_still_hide_a_photograph(family_app_admin, people):
    conn = people["conn"]
    target = ids_in(conn, "private")[2]
    response = family_app_admin.post("/api/visibility",
                                     json={"ids": [target], "visibility": "hidden"})
    assert response.status_code == 200 and response.get_json()["updated"] == 1
    assert target not in set(ids_of(family_app_admin))


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
