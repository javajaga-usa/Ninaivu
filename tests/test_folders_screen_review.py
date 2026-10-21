"""The console's Folders screen as the place to review hidden and explicit files.

Everything is listed there, whatever its visibility or flag, a page at a time
and, when asked, from every folder inside the open one. Hiding a folder and
stopping hiding it both work from it, and stopping really does put the files
back: taking the rule away used to leave every file it had hidden still
hidden. None of what is hidden or flagged explicit reaches the family app.
"""

from pathlib import Path

import pytest
from PIL import Image

from conftest import ids_of
from ninaivu import ai
from ninaivu.storage import db
from ninaivu.api import admin_api


@pytest.fixture()
def mixed_screens(scanned, library, people):
    """The shared library plus one screenshot, by name."""
    cfg, conn, scanner = scanned
    shots = library / "phone" / "Screenshots"
    shots.mkdir(parents=True)
    Image.new("RGB", (300, 600), (240, 240, 240)).save(shots / "Screenshot_20240607_WhatsApp.png")
    scanner._run(Path(cfg.active_root), full=False)
    shot = conn.execute("SELECT id FROM assets WHERE filename LIKE 'Screenshot_%'").fetchone()[0]
    photo = conn.execute("SELECT id FROM assets WHERE folder='private' ORDER BY id").fetchone()[0]
    # The screenshot rule's own pass, as a scan with the setting off leaves it.
    conn.execute("UPDATE assets SET visibility=1, vis_source='default' WHERE id=?", (shot,))
    conn.commit()
    return conn, shot, photo


def folder(client, path="", **args):
    query = "&".join(f"{k}={v}" for k, v in args.items())
    response = client.get(f"/api/admin/folder?folder={path}&{query}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def ids_in(conn, folder_name):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE folder=? ORDER BY id", (folder_name,))]


def flag_explicit(client, ids):
    response = client.post("/api/assets/bulk", json={"ids": list(ids), "nsfw": True})
    assert response.status_code == 200, response.get_json()


def test_hidden_and_explicit_files_are_listed_and_marked(as_admin, people):
    conn = people["conn"]
    secret = ids_in(conn, "private")
    as_admin.post("/api/visibility", json={"ids": secret[:1], "visibility": "hidden"})
    flag_explicit(as_admin, secret[1:2])
    items = {i["id"]: i for i in folder(as_admin, "private")["items"]}
    assert set(items) == set(secret)
    assert items[secret[0]]["visibility_name"] == "hidden"
    assert items[secret[1]]["explicit"] is True
    assert items[secret[1]]["explicit_by_hand"] is True
    assert items[secret[2]]["explicit"] is False


def test_the_hidden_and_explicit_filters_count_and_narrow(as_admin, people):
    conn = people["conn"]
    secret = ids_in(conn, "private")
    as_admin.post("/api/visibility", json={"ids": secret[:2], "visibility": "hidden"})
    flag_explicit(as_admin, secret[2:])
    top = folder(as_admin)
    assert top["kinds"]["hidden"] == 2 and top["kinds"]["explicit"] == 1
    assert top["total_explicit"] == 1
    hidden = folder(as_admin, show="hidden", deep=1)
    assert [c["name"] for c in hidden["children"]] == ["private"]
    assert sorted(i["id"] for i in hidden["items"]) == secret[:2]
    explicit = folder(as_admin, show="explicit", deep=1)
    assert [i["id"] for i in explicit["items"]] == secret[2:]
    assert explicit["items"][0]["folder"] == "private"


def test_everything_beneath_a_folder_on_request(as_admin, people):
    shallow = folder(as_admin, "2023")
    assert shallow["items"] == [] and shallow["items_total"] == 0
    deep = folder(as_admin, "2023", deep=1)
    assert deep["deep"] is True
    assert deep["items_total"] == len(deep["items"]) > 0
    assert all(i["folder"].startswith("2023/") for i in deep["items"])


def test_a_large_folder_comes_a_page_at_a_time(as_admin, people, monkeypatch):
    monkeypatch.setattr(admin_api, "FOLDER_PAGE", 4)
    first = folder(as_admin, deep=1)
    assert len(first["items"]) == 4 and first["items_total"] > 4
    rest = folder(as_admin, deep=1, offset=4)
    seen = [i["id"] for i in first["items"]] + [i["id"] for i in rest["items"]]
    assert len(set(seen)) == len(seen)
    assert rest["offset"] == 4


def test_children_say_which_folder_has_its_own_rule(as_admin):
    as_admin.post("/api/visibility/folder", json={"folder": "private", "visibility": "hidden"})
    children = {c["name"]: c for c in folder(as_admin)["children"]}
    assert children["private"]["rule"] == "hidden"
    assert children["shared"]["rule"] is None


def test_stop_hiding_a_folder_puts_its_files_back(as_admin, people):
    conn = people["conn"]
    as_admin.post("/api/visibility/folder", json={"folder": "private", "visibility": "hidden"})
    assert {r["visibility"] for r in conn.execute(
        "SELECT visibility FROM assets WHERE folder='private'")} == {2}

    # Showing them to more people is asked about first.
    asked = as_admin.delete("/api/visibility/folder?folder=private")
    assert asked.status_code == 409
    assert asked.get_json()["impact"]["exposed"] == 3
    assert folder(as_admin, "private")["rule"]["visibility"] == 2   # asked, not done

    done = as_admin.delete("/api/visibility/folder?folder=private&confirm=1")
    assert done.status_code == 200, done.get_json()
    assert done.get_json()["updated"] == 3
    rows = conn.execute("SELECT visibility, vis_source FROM assets WHERE folder='private'")
    assert {(r["visibility"], r["vis_source"]) for r in rows} == {(1, "default")}
    assert folder(as_admin, "private")["rule"] is None


def test_stop_hiding_keeps_what_was_decided_file_by_file(as_admin, people):
    conn = people["conn"]
    secret = ids_in(conn, "private")
    as_admin.post("/api/visibility/folder", json={"folder": "private", "visibility": "hidden"})
    as_admin.post("/api/visibility", json={"ids": secret[:1], "visibility": "hidden"})
    as_admin.delete("/api/visibility/folder?folder=private&confirm=1")
    level = {r["id"]: r["visibility"] for r in conn.execute(
        "SELECT id, visibility FROM assets WHERE folder='private'")}
    assert level[secret[0]] == 2
    assert level[secret[1]] == level[secret[2]] == 1


def test_stop_hiding_falls_back_to_the_rule_above(as_admin, people):
    conn = people["conn"]
    as_admin.post("/api/visibility/folder", json={"folder": "shared", "visibility": "public",
                                                       "confirm": True})
    as_admin.post("/api/visibility/folder", json={"folder": "shared/holiday", "visibility": "hidden"})
    as_admin.delete("/api/visibility/folder?folder=shared/holiday&confirm=1")
    rows = conn.execute("SELECT visibility, vis_source FROM assets WHERE folder='shared/holiday'")
    assert {(r["visibility"], r["vis_source"]) for r in rows} == {(0, "folder")}


def test_stop_hiding_can_be_undone(as_admin, people):
    conn = people["conn"]
    as_admin.post("/api/visibility/folder", json={"folder": "private", "visibility": "hidden"})
    as_admin.delete("/api/visibility/folder?folder=private&confirm=1")
    undone = as_admin.post("/api/visibility/undo", json={})
    assert undone.status_code == 200, undone.get_json()
    assert {r["visibility"] for r in conn.execute(
        "SELECT visibility FROM assets WHERE folder='private'")} == {2}
    assert folder(as_admin, "private")["rule"]["visibility"] == 2


def test_the_whole_library_has_no_rule_to_take_away(as_admin):
    assert as_admin.delete("/api/visibility/folder?folder=").status_code == 403


def test_hidden_and_explicit_files_stay_out_of_the_family_app(as_admin, as_family, people):
    conn = people["conn"]
    secret = ids_in(conn, "private")
    as_admin.post("/api/visibility", json={"ids": secret[:1], "visibility": "hidden"})
    flag_explicit(as_admin, secret[1:2])
    seen = set(ids_of(as_family))
    assert secret[0] not in seen and secret[1] not in seen
    assert secret[2] in seen
    for asset_id in secret[:2]:
        assert as_family.get(f"/api/thumb/{asset_id}?s=256").status_code == 404
        assert as_family.get(f"/api/preview/{asset_id}").status_code == 404
    # The console still opens both, full size.
    for asset_id in secret[:2]:
        assert as_admin.get(f"/api/preview/{asset_id}").status_code == 200


def test_a_family_member_cannot_read_the_folder_screen(as_family):
    assert as_family.get("/api/admin/folder?folder=").status_code in (401, 403, 404)


# --- explicit is adult content only ----------------------------------------

def test_a_screenshot_the_model_called_explicit_is_hidden_instead(as_admin, mixed_screens):
    conn, shot, photo = mixed_screens
    conn.execute("UPDATE assets SET nsfw=1, nsfw_source='auto', visibility=1, "
                 "vis_source='default' WHERE id IN (?, ?)", (shot, photo))
    conn.commit()
    root = conn.execute("SELECT root FROM assets WHERE id=?", (shot,)).fetchone()[0]
    assert db.screens_not_explicit(conn, root) == 1
    row = conn.execute("SELECT nsfw, visibility, vis_source FROM assets WHERE id=?",
                       (shot,)).fetchone()
    assert (row["nsfw"], row["visibility"], row["vis_source"]) == (0, 2, "screen")
    # An ordinary photograph the model flagged stays explicit.
    assert conn.execute("SELECT nsfw FROM assets WHERE id=?", (photo,)).fetchone()[0] == 1
    explicit = folder(as_admin, show="explicit", deep=1)
    assert [i["id"] for i in explicit["items"]] == [photo]
    hidden = folder(as_admin, show="hidden", deep=1)
    assert shot in [i["id"] for i in hidden["items"]]


def test_explicit_set_by_hand_on_a_screenshot_stays(as_admin, mixed_screens):
    conn, shot, _ = mixed_screens
    flag_explicit(as_admin, [shot])
    root = conn.execute("SELECT root FROM assets WHERE id=?", (shot,)).fetchone()[0]
    assert db.screens_not_explicit(conn, root) == 0
    assert conn.execute("SELECT nsfw FROM assets WHERE id=?", (shot,)).fetchone()[0] == 1


def test_the_content_check_puts_screens_and_documents_on_the_safe_side():
    """The content check names screenshots and documents on the safe side."""
    safe = ai.NSFW_PROMPTS[:ai.NSFW_UNSAFE_FROM]
    assert any("screenshot" in p for p in safe)
    assert any("document" in p for p in safe)
    assert not any("document" in p or "screenshot" in p
                   for p in ai.NSFW_PROMPTS[ai.NSFW_UNSAFE_FROM:])


def test_the_next_scan_tidies_what_an_earlier_release_flagged(scanned, mixed_screens):
    cfg, _, scanner = scanned
    conn, shot, _ = mixed_screens
    cfg.hide_screens = False      # tidied whether the screenshot rule is on or not
    conn.execute("UPDATE assets SET nsfw=1, nsfw_source='auto' WHERE id=?", (shot,))
    conn.commit()
    scanner._run(Path(cfg.active_root), full=False)
    row = conn.execute("SELECT nsfw, visibility FROM assets WHERE id=?", (shot,)).fetchone()
    assert (row["nsfw"], row["visibility"]) == (0, 2)


# --- deleting from the Folders screen --------------------------------------

def test_the_folders_screen_deletes_without_a_password(as_admin, people):
    conn = people["conn"]
    target = ids_in(conn, "private")[:1]
    response = as_admin.post("/api/admin/folders/delete", json={"ids": target})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["deleted"] == 1
    # In the bin, so Recently deleted can put it back.
    assert response.get_json()["bin"]["items"] == 1


def test_the_gallery_delete_still_asks_for_the_password(as_admin, people):
    conn = people["conn"]
    response = as_admin.post("/api/delete", json={"ids": ids_in(conn, "private")[:1]})
    assert response.status_code == 401 and response.get_json()["needs_password"]


def test_a_family_member_cannot_delete_from_the_folders_screen(as_family, people):
    conn = people["conn"]
    target = ids_in(conn, "private")[:1]
    assert as_family.post("/api/admin/folders/delete",
                          json={"ids": target}).status_code in (401, 403, 404)
    assert conn.execute("SELECT trashed FROM assets WHERE id=?", target).fetchone()[0] == 0


# --- videos a browser cannot play, and the file on disk ----------------------

@pytest.fixture()
def clips(scanned, library, people):
    """A 3GP and an MKV, made by ffmpeg, beside an MP4."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    cfg, conn, scanner = scanned
    folder_path = library / "clips"
    folder_path.mkdir()
    for name, extra in (("old phone.3gp", ["-c:v", "mpeg4"]),
                        ("film.mkv", ["-c:v", "libx264"]),
                        ("ok.mp4", ["-c:v", "libx264", "-pix_fmt", "yuv420p"])):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=size=176x144:rate=10:duration=1", *extra,
                        str(folder_path / name)], check=True)
    scanner._run(Path(cfg.active_root), full=False)
    return conn


def test_the_viewer_is_told_how_to_play_each_file(as_admin, clips):
    items = {i["filename"]: i for i in folder(as_admin, "clips")["items"]}
    assert items["ok.mp4"]["playable"] and not items["ok.mp4"]["needs_proxy"]
    for name in ("old phone.3gp", "film.mkv"):
        assert items[name]["needs_proxy"] is True, items[name]
        assert items[name]["playable"] is False


def test_a_3gp_plays_through_a_converted_copy(as_admin, clips):
    asset_id = next(i["id"] for i in folder(as_admin, "clips")["items"]
                    if i["filename"] == "old phone.3gp")
    import time
    for _ in range(120):
        response = as_admin.get(f"/api/proxy/{asset_id}",
                                headers={"Accept": "application/json"})
        if response.status_code != 202:
            break
        time.sleep(0.5)
    assert response.status_code == 200, response.get_json()
    assert response.mimetype == "video/mp4"


def test_the_folder_path_on_disk_is_given(as_admin, people):
    body = folder(as_admin, "private")
    assert body["path"].endswith("private")
    assert all(i["path"].endswith(i["filename"]) for i in body["items"])


def test_a_folder_opens_in_the_file_manager_at_this_computer(as_admin, people, monkeypatch):
    calls = []
    monkeypatch.setattr(admin_api.subprocess, "Popen",
                        lambda command, *a, **k: calls.append(command))
    monkeypatch.setattr(admin_api.sys, "platform", "darwin")
    response = as_admin.post("/api/admin/reveal-folder", json={"folder": "private"})
    assert response.status_code == 200, response.get_json()
    assert calls[0][0] == "open" and calls[0][1].endswith("private")
    # Nothing outside the library, however it is spelled.
    assert as_admin.post("/api/admin/reveal-folder",
                         json={"folder": "../.."}).status_code == 404
    refused = as_admin.post("/api/admin/reveal-folder", json={"folder": "private"},
                            environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    assert refused.status_code == 409


def test_an_address_of_this_computer_is_recognised_by_binding_to_it():
    assert admin_api._is_own_address("127.0.0.1")
    assert not admin_api._is_own_address("203.0.113.9")
    assert not admin_api._is_own_address("not an address")
