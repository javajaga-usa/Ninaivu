"""The Folders screen narrows to one kind of file, and shows a file where it lives.

Filtering is done by the server, so the folder cards, the counts and the files
all agree with the chip that is selected, however many files a folder holds.
And "show in the file manager" works for somebody at the Ninaivu computer who
opened the console by its network name rather than as localhost.
"""

import sys
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.api import admin_api
from ninaivu.storage import db


@pytest.fixture()
def mixed(app, scanned, library):
    """The shared library plus a video, a song and two screenshots."""
    cfg, conn, scanner = scanned
    (library / "clips").mkdir()
    (library / "clips" / "birthday.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + bytes(4000))
    (library / "music").mkdir()
    (library / "music" / "song.mp3").write_bytes(b"ID3" + bytes(4000))
    shots = library / "phone" / "Screenshots"
    shots.mkdir(parents=True)
    Image.new("RGB", (300, 600), (240, 240, 240)).save(shots / "Screenshot_20240607_WhatsApp.png")
    Image.new("RGB", (300, 600), (230, 230, 230)).save(shots / "Screenshot_20240608_Chrome.png")
    scanner._run(Path(cfg.active_root), full=False)
    return conn


def folder(client, path="", show=None):
    query = f"/api/admin/folder?folder={path}" + (f"&show={show}" if show else "")
    return client.get(query).get_json()


def test_the_counts_cover_every_kind(as_admin, mixed):
    body = folder(as_admin)
    kinds = body["kinds"]
    assert kinds["video"] == 1 and kinds["audio"] == 1
    assert kinds["screen"] == 2
    assert kinds["all"] == kinds["picture"] + kinds["video"] + kinds["audio"]
    assert body["show"] == "all"


def test_videos_only(as_admin, mixed):
    body = folder(as_admin, show="video")
    assert [c["name"] for c in body["children"]] == ["clips"]
    assert body["children"][0]["n"] == 1
    inside = folder(as_admin, "clips", show="video")
    assert [i["kind"] for i in inside["items"]] == ["video"]
    # The chips still count everything, whatever is selected.
    assert body["kinds"] == folder(as_admin)["kinds"]


def test_photos_leave_out_the_song(as_admin, mixed):
    names = {c["name"] for c in folder(as_admin, show="picture")["children"]}
    assert "music" not in names and "clips" not in names
    assert "phone" in names


def test_screenshots_and_documents(as_admin, mixed):
    body = folder(as_admin, "phone/Screenshots", show="screen")
    assert sorted(i["filename"] for i in body["items"]) == [
        "Screenshot_20240607_WhatsApp.png", "Screenshot_20240608_Chrome.png"]
    assert {c["name"] for c in folder(as_admin, show="screen")["children"]} == {"phone"}


def test_a_screenshot_shown_again_by_hand_is_still_one(as_admin, mixed):
    """The filter finds what it is, not what visibility it has."""
    item = mixed.execute("SELECT id FROM assets WHERE filename=?",
                         ("Screenshot_20240607_WhatsApp.png",)).fetchone()["id"]
    db.set_visibility(mixed, [item], 1, source="item", record_undo=False)
    body = folder(as_admin, "phone/Screenshots", show="screen")
    assert len(body["items"]) == 2


def test_an_unknown_filter_shows_everything(as_admin, mixed):
    assert folder(as_admin, show="nonsense")["show"] == "all"


# -- show in the file manager ---------------------------------------------------

@pytest.fixture()
def opened(monkeypatch):
    calls = []

    class FakeProcess:
        def __init__(self, command, *args, **kwargs):
            # Popen is patched on the shared subprocess module, so the drive
            # check the scan starts in the background (PowerShell on Windows)
            # lands here too. Only what the reveal endpoint runs is counted.
            if sys._getframe(1).f_globals.get("__name__") == admin_api.__name__:
                calls.append(command)

    monkeypatch.setattr(admin_api.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(admin_api.sys, "platform", "win32")
    monkeypatch.setattr(admin_api, "_own_addresses",
                        lambda: {"127.0.0.1", "::1", "192.168.0.119"})
    return calls


def a_file(conn):
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE kind='picture' "
                       "ORDER BY id LIMIT 1").fetchone()
    return row["id"], Path(row["root"]) / row["rel_path"]


def test_the_computer_s_own_address_counts_as_this_computer(as_admin, mixed, opened):
    asset_id, _path = a_file(mixed)
    response = as_admin.post(f"/api/admin/reveal/{asset_id}",
                             environ_overrides={"REMOTE_ADDR": "192.168.0.119"})
    assert response.status_code == 200, response.get_json()
    assert len(opened) == 1


def test_another_device_is_still_refused(as_admin, mixed, opened):
    asset_id, _path = a_file(mixed)
    response = as_admin.post(f"/api/admin/reveal/{asset_id}",
                             environ_overrides={"REMOTE_ADDR": "192.168.0.245"})
    assert response.status_code == 409
    assert not opened


def test_a_forwarded_request_is_not_this_computer(as_admin, mixed, opened):
    asset_id, _path = a_file(mixed)
    response = as_admin.post(f"/api/admin/reveal/{asset_id}",
                             headers={"X-Forwarded-For": "192.168.0.245"},
                             environ_overrides={"REMOTE_ADDR": "192.168.0.119"})
    assert response.status_code == 409
    assert not opened


def test_explorer_is_given_the_path_quoted_on_its_own(as_admin, mixed, library, opened):
    """A quoted "/select,<path>" is ignored by Explorer, which then opens
    Documents; it has to be /select,"<path>"."""
    spaced = library / "Birthday party" / "13. Cake and candles.jpg"
    spaced.parent.mkdir()
    Image.new("RGB", (400, 300), (200, 100, 50)).save(spaced)
    root = Path(mixed.execute("SELECT root FROM assets LIMIT 1").fetchone()["root"])
    as_admin.application.config["MV_SCANNER"]._run(root, full=False)
    asset_id = mixed.execute("SELECT id FROM assets WHERE filename=?",
                             ("13. Cake and candles.jpg",)).fetchone()["id"]

    response = as_admin.post(f"/api/admin/reveal/{asset_id}")
    assert response.status_code == 200, response.get_json()
    command = opened[0]
    assert isinstance(command, str)
    assert command.startswith('explorer /select,"')
    assert command.endswith('13. Cake and candles.jpg"')


def test_stopping_the_server_stays_loopback_only(app, monkeypatch):
    monkeypatch.setattr(admin_api, "_own_addresses",
                        lambda: {"127.0.0.1", "::1", "192.168.0.119"})
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.168.0.119"}):
        assert admin_api.from_this_computer() is True
        assert admin_api.from_this_machine() is False
