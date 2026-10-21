"""The console's Folders screen as the place to review hidden and explicit files.

Everything is listed there, whatever its visibility or flag, a page at a time
and, when asked, from every folder inside the open one. Hiding a folder and
stopping hiding it both work from it, and stopping really does put the files
back: taking the rule away used to leave every file it had hidden still
hidden. None of what is hidden or flagged explicit reaches the family app.
"""

from conftest import ids_of
from ninaivu.api import admin_api


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
