"""Deleting from the gallery, and the folder screen the console shows.

Deletion is the only thing in Ninaivu that removes a file from where its owner
put it, so it is the one place worth being most careful: admin only, password
every time, and the file *moved* rather than erased — into a plain folder that
can be opened in Finder without Ninaivu existing at all.
"""

from pathlib import Path

import pytest

from conftest import ADMIN, FAMILY, GUEST, ids_of, login
from ninaivu.storage import recycle


def files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def hide(client, ids):
    """Make these admin-only, which deleting now requires first.

    Hiding is the reversible step in front of the irreversible one — see
    ``test_admin_limits.py`` for the rule itself. Everything below is about
    what happens *after* it, so it goes through here rather than repeating the
    setup fifteen times.
    """
    response = client.post("/api/visibility",
                           json={"ids": list(ids), "visibility": "hidden"})
    assert response.status_code == 200, response.get_json()
    return list(ids)


# --- who may delete --------------------------------------------------------

@pytest.mark.parametrize("who", ["family", "guest"])
def test_only_an_admin_can_delete(app, people, as_admin, who):
    """A real id, so the refusal is about the role and not about the id."""
    target = ids_of(as_admin)[0]
    client = login(app.test_client(), *(FAMILY if who == "family" else GUEST))
    response = client.post("/api/delete",
                           json={"ids": [target], "password": "anything"})
    assert response.status_code in (401, 403), response.status_code


def test_an_anonymous_visitor_cannot_delete(anon):
    assert anon.post("/api/delete", json={"ids": [1]}).status_code in (401, 403)


# --- the password ----------------------------------------------------------

def test_deleting_without_a_password_is_refused(as_admin):
    target = hide(as_admin, ids_of(as_admin)[:1])[0]
    response = as_admin.post("/api/delete", json={"ids": [target]})
    assert response.status_code == 401
    assert response.get_json()["needs_password"] is True


def test_the_wrong_password_is_refused_and_says_so(as_admin):
    target = hide(as_admin, ids_of(as_admin)[:1])[0]
    response = as_admin.post("/api/delete",
                             json={"ids": [target], "password": "nope"})
    assert response.status_code == 401
    assert "not right" in response.get_json()["error"]


def test_nothing_is_touched_when_the_password_is_wrong(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    before_files = files_under(root)
    before_rows = conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]

    as_admin.post("/api/delete", json={"ids": hide(as_admin, ids_of(as_admin)[:3]),
                                       "password": "nope"})

    assert files_under(root) == before_files
    assert conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"] == before_rows


def test_a_refused_delete_is_written_to_the_activity_log(as_admin):
    as_admin.post("/api/delete", json={"ids": hide(as_admin, ids_of(as_admin)[:1]),
                                       "password": "nope"})
    assert "delete_refused" in str(as_admin.get("/api/audit").get_json())


# --- what deleting actually does -------------------------------------------

def test_the_file_is_moved_into_the_bin_not_erased(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    target = hide(as_admin, ids_of(as_admin)[:1])[0]
    row = conn.execute("SELECT rel_path, filename FROM assets WHERE id=?",
                       (target,)).fetchone()
    original = root / row["rel_path"]
    assert original.exists()

    result = as_admin.post("/api/delete",
                           json={"ids": [target], "password": ADMIN[1]})
    assert result.status_code == 200
    assert result.get_json()["deleted"] == 1

    assert not original.exists(), "the file is still where it was"
    survivors = list(recycle.bin_path(root).rglob(row["filename"]))
    assert survivors, "the file was erased instead of moved to the bin"
    assert survivors[0].is_file()


def test_the_bin_is_a_plain_folder_anyone_can_open(as_admin, scanned):
    """No database is needed to get a photograph back out of it."""
    cfg, _, _ = scanned
    root = Path(cfg.active_root)
    as_admin.post("/api/delete",
                  json={"ids": hide(as_admin, ids_of(as_admin)[:2]),
                        "password": ADMIN[1]})
    bin_dir = recycle.bin_path(root)
    assert bin_dir.is_dir()
    # Filed under the date, keeping the path it had.
    assert list(bin_dir.iterdir()), "nothing landed in the bin"
    assert all(len(p.name) == 10 and p.name[4] == "-" for p in bin_dir.iterdir())


def test_the_item_leaves_the_gallery_at_once(as_admin, app, people):
    target = hide(as_admin, ids_of(as_admin)[:1])[0]
    as_admin.post("/api/delete", json={"ids": [target], "password": ADMIN[1]})
    assert as_admin.get(f"/api/asset/{target}").status_code == 404
    family = login(app.test_client(), *FAMILY)
    assert target not in ids_of(family)


def test_a_deleted_file_is_not_re_indexed_by_the_next_scan(as_admin, scanned):
    """The bin is inside the library, so a scan must step over it."""
    cfg, conn, _ = scanned
    from ninaivu.media.scanner import Scanner

    before = conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]
    as_admin.post("/api/delete",
                  json={"ids": hide(as_admin, ids_of(as_admin)[:2]),
                        "password": ADMIN[1]})
    Scanner(cfg)._run(Path(cfg.active_root), full=True)
    after = conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"]
    assert after == before - 2, "the scan put the deleted files back"


def test_deleting_something_already_gone_is_reported_not_hidden(as_admin, scanned):
    cfg, conn, _ = scanned
    target = hide(as_admin, ids_of(as_admin)[:1])[0]
    row = conn.execute("SELECT rel_path FROM assets WHERE id=?", (target,)).fetchone()
    (Path(cfg.active_root) / row["rel_path"]).unlink()

    body = as_admin.post("/api/delete",
                         json={"ids": [target], "password": ADMIN[1]}).get_json()
    assert any("missing" in f["why"] for f in body["failed"]), body


# --- putting it back -------------------------------------------------------

def test_the_console_lists_what_is_in_the_bin(as_admin, scanned):
    as_admin.post("/api/delete",
                  json={"ids": hide(as_admin, ids_of(as_admin)[:2]),
                        "password": ADMIN[1]})
    body = as_admin.get("/api/recycle").get_json()
    assert body["summary"]["items"] == 2
    assert len(body["items"]) == 2
    assert all(item["present"] for item in body["items"])


def test_the_console_can_show_a_thumbnail_for_a_deleted_file(as_admin, scanned):
    target = hide(as_admin, ids_of(as_admin)[:1])[0]
    as_admin.post("/api/delete",
                  json={"ids": [target], "password": ADMIN[1]})

    item = as_admin.get("/api/recycle").get_json()["items"][0]
    assert item["thumbnail"] == f"/api/recycle/thumb/{item['id']}"
    preview = as_admin.get(item["thumbnail"])
    assert preview.status_code == 200
    assert preview.mimetype.startswith("image/")
    assert preview.data


def test_restoring_puts_the_file_back_where_it_came_from(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    target = hide(as_admin, ids_of(as_admin)[:1])[0]
    row = conn.execute("SELECT rel_path FROM assets WHERE id=?", (target,)).fetchone()
    original = root / row["rel_path"]

    as_admin.post("/api/delete", json={"ids": [target], "password": ADMIN[1]})
    assert not original.exists()

    entry = as_admin.get("/api/recycle").get_json()["items"][0]
    result = as_admin.post("/api/recycle/restore", json={"ids": [entry["id"]]})
    assert result.get_json()["restored"] == 1
    assert original.exists(), "the file did not come back to its own path"


def test_a_restored_entry_leaves_the_bin_listing(as_admin, scanned):
    as_admin.post("/api/delete",
                  json={"ids": hide(as_admin, ids_of(as_admin)[:1]),
                        "password": ADMIN[1]})
    entry = as_admin.get("/api/recycle").get_json()["items"][0]
    as_admin.post("/api/recycle/restore", json={"ids": [entry["id"]]})
    assert as_admin.get("/api/recycle").get_json()["summary"]["items"] == 0


def test_two_files_of_the_same_name_both_survive_in_the_bin(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    # shot0.jpg and its copy live in the same folder in the fixture.
    rows = conn.execute(
        "SELECT id, filename FROM assets WHERE folder='2023/05/12'").fetchall()
    ids = hide(as_admin, [r["id"] for r in rows][:2])
    as_admin.post("/api/delete", json={"ids": ids, "password": ADMIN[1]})
    landed = [p for p in recycle.bin_path(root).rglob("*") if p.is_file()]
    assert len(landed) == len(ids), "a same-named file overwrote another in the bin"


# --- the folder screen -----------------------------------------------------

def test_the_folder_screen_lists_what_is_directly_inside(as_admin):
    body = as_admin.get("/api/admin/folder?folder=2023").get_json()
    assert [c["name"] for c in body["children"]] == ["05", "06", "07", "08", "09", "10"]
    assert body["items"] == [], "2023 holds folders, not loose files"


def test_each_subfolder_carries_its_own_visibility_counts(as_admin):
    as_admin.post("/api/visibility/folder",
                  json={"folder": "private", "visibility": "hidden"})
    body = as_admin.get("/api/admin/folder").get_json()
    private = next(c for c in body["children"] if c["name"] == "private")
    assert private["hidden"] == private["n"] > 0
    assert private["public"] == 0


def test_the_screen_says_how_much_is_hidden_overall(as_admin):
    as_admin.post("/api/visibility/folder",
                  json={"folder": "private", "visibility": "hidden"})
    body = as_admin.get("/api/admin/folder").get_json()
    assert body["total_hidden"] == 3


def test_items_carry_their_visibility_and_where_it_came_from(as_admin):
    body = as_admin.get("/api/admin/folder?folder=private").get_json()
    assert body["items"], "the private folder has files directly in it"
    for item in body["items"]:
        assert item["visibility_name"] in ("public", "family", "hidden")
        assert "vis_source" in item


def test_a_hidden_item_is_identifiable_for_the_red_treatment(as_admin, scanned):
    _, conn, _ = scanned
    target = conn.execute(
        "SELECT id FROM assets WHERE folder='private' LIMIT 1").fetchone()["id"]
    as_admin.post("/api/visibility", json={"ids": [target], "visibility": "hidden"})
    body = as_admin.get("/api/admin/folder?folder=private").get_json()
    marked = [i for i in body["items"] if i["visibility_name"] == "hidden"]
    assert len(marked) == 1 and marked[0]["id"] == target


def test_a_breadcrumb_trail_comes_back_for_navigation(as_admin):
    body = as_admin.get("/api/admin/folder?folder=2023/05/12").get_json()
    assert [t["path"] for t in body["trail"]] == ["", "2023", "2023/05", "2023/05/12"]


def test_each_subfolder_offers_a_cover_picture(as_admin):
    body = as_admin.get("/api/admin/folder?folder=2023/05").get_json()
    day = next(c for c in body["children"] if c["name"] == "12")
    assert day["cover"], "no thumbnail to show for the folder"


def test_the_folder_screen_is_console_only(app, people, scanned):
    """It exposes counts of hidden media, so the family port must not route it."""
    from ninaivu import build_services, create_home_app

    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    paths = {str(r) for r in create_home_app(services).url_map.iter_rules()}
    assert "/api/admin/folder" not in paths
