"""Per-member library folders.

An admin can hand each family member their own folder — either a whole
library folder or one inside it — and that assignment is the only thing they
can see. The property that matters is isolation: Maya must not be able to
reach Sam's photos by any route, including a direct link to a file id.
"""

from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu.server import auth
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.server.auth import resolve_library
from ninaivu.media.scanner import Scanner


# ---------------------------------------------------------------------------
# Path resolution — pure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("assignment,roots,expected", [
    (None,               ["/a", "/b"], (["/a", "/b"], None)),
    ("/a",               ["/a", "/b"], (["/a"], None)),
    ("/a/kids",          ["/a", "/b"], (["/a"], "kids")),
    ("/a/kids/2024",     ["/a"],       (["/a"], "kids/2024")),
    ("/nowhere",         ["/a"],       ([], None)),
    ("C:/Master",        ["C:/Master"], (["C:/Master"], None)),
    ("c:\\master\\Maya", ["C:/Master"], (["C:/Master"], "Maya")),
])
def test_assignment_resolves_to_root_and_subfolder(assignment, roots, expected):
    assert resolve_library(assignment, roots) == expected


def test_deepest_root_wins_when_roots_nest():
    assert resolve_library("/srv/media/kids", ["/srv", "/srv/media"]) \
        == (["/srv/media"], "kids")


def test_assignment_to_a_removed_folder_shows_nothing():
    """Never fall back to 'everything' — that would be a silent privacy leak."""
    assert resolve_library("/gone", ["/a"]) == ([], None)


# ---------------------------------------------------------------------------
# Two library folders, one per member
# ---------------------------------------------------------------------------

def build_library(base: Path, name: str, count: int) -> Path:
    root = base / name
    (root / "trips").mkdir(parents=True, exist_ok=True)
    for i in range(count):
        Image.new("RGB", (200 + i, 150), (i * 20 % 255, 90, 40)).save(
            root / "trips" / f"{name}{i}.jpg")
    return root


@pytest.fixture()
def household(tmp_path):
    """Two separate library folders, indexed, with an admin signed in."""
    from ninaivu.server.config import Config

    maya_lib = build_library(tmp_path, "maya-photos", 4)
    sam_lib = build_library(tmp_path, "sam-photos", 3)

    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(maya_lib), str(sam_lib)]
    cfg.active_root = str(maya_lib)
    cfg.ai_enabled = False
    cfg.ai_engine = "off"
    cfg.watch = False
    cfg.workers = 2
    cfg.open_browsing = True
    cfg.min_media_bytes = 0   # fixtures draw tiny images
    cfg.ensure_dirs()

    from ninaivu.storage import db

    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    scanner = Scanner(cfg)
    for root in cfg.roots:
        scanner._run(Path(root), full=True)

    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    maya = auth.create_user(conn, "maya", "", display_name="Maya",
                            role=auth.ROLE_FAMILY, created_by=admin.id)
    sam = auth.create_user(conn, "sam", "", display_name="Sam",
                           role=auth.ROLE_FAMILY, created_by=admin.id)

    services = build_services(cfg)
    services.scanner.stop()
    return {
        "cfg": cfg, "conn": conn, "services": services,
        "maya_lib": str(maya_lib), "sam_lib": str(sam_lib),
        "maya": maya, "sam": sam, "admin": admin,
        "console": login(create_admin_app(services).test_client(), *ADMIN),
        "home": create_home_app(services),
    }


def enter(app, user_id):
    client = app.test_client()
    response = client.post("/api/auth/enter", json={"id": user_id})
    assert response.status_code == 200, response.get_json()
    return client


def test_both_library_folders_are_indexed(household):
    from ninaivu.storage import db

    rows, total = db.query_assets(
        household["conn"], household["cfg"].roots, limit=100, max_visibility=2)
    assert total == 7
    assert {r["root"] for r in rows} == set(household["cfg"].roots)


def test_admin_sees_every_folder(household):
    data = household["console"].get("/api/assets?limit=99").get_json()
    assert data["total"] == 7


def test_assigning_a_folder_limits_what_a_member_sees(household):
    console, home = household["console"], household["home"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": household["maya_lib"]})
    console.post(f"/api/people/{household['sam'].id}",
                 json={"library": household["sam_lib"]})

    maya = enter(home, household["maya"].id)
    sam = enter(home, household["sam"].id)

    maya_items = maya.get("/api/assets?limit=99").get_json()["items"]
    sam_items = sam.get("/api/assets?limit=99").get_json()["items"]

    assert len(maya_items) == 4
    assert len(sam_items) == 3
    assert all(i["name"].startswith("maya-photos") for i in maya_items)
    assert all(i["name"].startswith("sam-photos") for i in sam_items)


def test_a_member_cannot_reach_another_folder_by_id(household):
    console, home = household["console"], household["home"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": household["maya_lib"]})

    sams = [i for i in console.get("/api/assets?limit=99").get_json()["items"]
            if i["name"].startswith("sam-photos")]
    assert sams

    maya = enter(home, household["maya"].id)
    for item in sams:
        assert maya.get(f"/api/asset/{item['id']}").status_code == 404
        assert maya.get(f"/api/thumb/{item['id']}").status_code == 404
        assert maya.get(f"/api/file/{item['id']}").status_code == 404
        assert maya.get(f"/api/download/{item['id']}").status_code == 404


def test_unassigned_members_see_every_folder(household):
    maya = enter(household["home"], household["maya"].id)
    assert maya.get("/api/assets?limit=99").get_json()["total"] == 7


def test_a_subfolder_assignment_narrows_further(household):
    console, home = household["console"], household["home"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": f"{household['maya_lib']}/trips"})

    maya = enter(home, household["maya"].id)
    items = maya.get("/api/assets?limit=99").get_json()["items"]
    assert len(items) == 4
    assert all(i["folder"] == "trips" for i in items)


def test_assignment_outside_every_library_is_refused(household, tmp_path):
    stray = tmp_path / "not-a-library"
    stray.mkdir()
    response = household["console"].post(
        f"/api/people/{household['maya'].id}", json={"library": str(stray)})
    assert response.status_code == 400
    assert "not inside any library folder" in response.get_json()["error"]


def test_facets_and_search_respect_the_assignment(household):
    console, home = household["console"], household["home"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": household["maya_lib"]})
    maya = enter(home, household["maya"].id)

    hits = maya.get("/api/assets?q=sam-photos&limit=99").get_json()
    assert hits["total"] == 0, "search must not leak another member's folder"

    stats = maya.get("/api/status").get_json()["stats"]
    assert stats["count"] == 4


def test_people_list_reports_what_each_person_can_see(household):
    console = household["console"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": household["maya_lib"]})

    people = {p["username"]: p for p in console.get("/api/people").get_json()["people"]}
    assert people["maya"]["library"] == household["maya_lib"]
    assert people["maya"]["visible_count"] == 4
    assert people["sam"]["visible_count"] == 7      # unassigned: everything
    assert people["dad"]["visible_count"] == 7      # admin: always everything


def test_admins_are_never_confined(household):
    console = household["console"]
    console.post(f"/api/people/{household['admin'].id}",
                 json={"library": household["sam_lib"]})
    assert console.get("/api/assets?limit=99").get_json()["total"] == 7
    assert console.get("/api/me").get_json()["library"] is None


# ---------------------------------------------------------------------------
# Managing the folders themselves
# ---------------------------------------------------------------------------

def test_console_lists_each_folder_with_its_counts(household):
    folders = household["console"].get("/api/admin/libraries").get_json()["folders"]
    by_path = {f["path"]: f for f in folders}
    assert by_path[household["maya_lib"]]["count"] == 4
    assert by_path[household["sam_lib"]]["count"] == 3
    assert by_path[household["maya_lib"]]["active"] is True


def test_assignable_offers_roots_and_their_subfolders(household):
    folders = household["console"].get("/api/admin/assignable").get_json()["folders"]
    paths = {f["path"] for f in folders}
    assert household["maya_lib"] in paths
    assert f"{household['maya_lib']}/trips" in paths
    roots = [f for f in folders if f["is_root"]]
    assert len(roots) == 2


def test_adding_a_folder_indexes_it(household, tmp_path):
    extra = build_library(tmp_path, "extra-photos", 2)
    response = household["console"].post(
        "/api/library/root", json={"path": str(extra)})
    assert response.status_code == 200
    assert response.get_json()["added"] is True
    assert str(extra) in household["cfg"].roots


def test_removing_a_folder_warns_when_someone_is_assigned(household):
    console = household["console"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": household["maya_lib"]})

    response = console.delete(
        f"/api/admin/libraries?path={household['maya_lib']}")
    assert response.status_code == 409
    assert "Maya" in response.get_json()["error"]
    assert household["maya_lib"] in household["cfg"].roots, "not removed yet"


def test_forcing_removal_leaves_the_member_seeing_nothing(household):
    """Removing someone's folder must not widen them to every other folder.

    Clearing the assignment used to be the answer, and an empty assignment
    means "the whole library" — so removing a child's folder handed them the
    house. The assignment stays, matches nothing, and shows nothing.
    """
    console, home = household["console"], household["home"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": household["maya_lib"]})

    response = console.delete(
        f"/api/admin/libraries?path={household['maya_lib']}&force=1")
    assert response.status_code == 200
    assert household["maya_lib"] not in household["cfg"].roots

    people = {p["username"]: p for p in console.get("/api/people").get_json()["people"]}
    assert people["maya"]["library"] == household["maya_lib"]
    assert people["maya"]["visible_count"] == 0
    maya = enter(home, household["maya"].id)
    assert maya.get("/api/assets?limit=99").get_json()["total"] == 0
    # And the removed folder's items are gone from the index.
    assert console.get("/api/assets?limit=99").get_json()["total"] == 3


def test_removing_a_folder_leaves_the_files_alone(household):
    console = household["console"]
    console.delete(f"/api/admin/libraries?path={household['sam_lib']}")
    assert Path(household["sam_lib"]).is_dir()
    assert list(Path(household["sam_lib"]).rglob("*.jpg"))


def test_a_folder_from_another_kind_of_machine_can_be_removed(household):
    r"""A library brought across from Windows to a Mac is listed as
    "E:\MasterArchive". Resolved on the Mac that is a relative path, so it
    matched nothing, and Remove answered that the folder was not in the
    library."""
    import os

    foreign = "/Volumes/Photos/MasterArchive" if os.name == "nt" else "E:\\MasterArchive"
    cfg = household["cfg"]
    cfg.roots.append(foreign)
    cfg.active_root = foreign
    response = household["console"].delete(
        "/api/admin/libraries", query_string={"path": foreign})
    assert response.status_code == 200, response.get_json()
    assert foreign not in cfg.roots
    assert cfg.active_root in (household["maya_lib"], household["sam_lib"])


def test_default_folder_can_be_switched(household):
    console = household["console"]
    response = console.post("/api/admin/libraries/active",
                            json={"path": household["sam_lib"]})
    assert response.status_code == 200
    assert household["cfg"].active_root == household["sam_lib"]


def test_only_admins_can_manage_folders(household):
    home = household["home"]
    maya = enter(home, household["maya"].id)
    assert maya.get("/api/admin/libraries").status_code == 404
    assert maya.get("/api/admin/assignable").status_code == 404
    assert maya.post("/api/library/root", json={"path": "/tmp"}).status_code == 404


def test_a_member_cannot_assign_themselves_a_folder(household):
    home = household["home"]
    maya = enter(home, household["maya"].id)
    response = maya.post(f"/api/people/{household['maya'].id}",
                         json={"library": household["sam_lib"]})
    assert response.status_code == 404, "people management is console-only"


def test_preview_shows_what_each_assigned_member_would_see(household):
    """Regression: the preview used to read the legacy scope column and only
    the default folder, so every member showed the same number."""
    console = household["console"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": household["maya_lib"]})
    console.post(f"/api/people/{household['sam'].id}",
                 json={"library": household["sam_lib"]})

    maya = console.get(f"/api/admin/preview?person={household['maya'].id}").get_json()
    sam = console.get(f"/api/admin/preview?person={household['sam'].id}").get_json()

    assert (maya["as"], maya["total"]) == ("Maya", 4)
    assert (sam["as"], sam["total"]) == ("Sam", 3)
    assert maya["library"] == household["maya_lib"]
    assert maya["folders"] == [household["maya_lib"]]

    # …and it must not depend on which folder happens to be the default.
    console.post("/api/admin/libraries/active", json={"path": household["sam_lib"]})
    again = console.get(f"/api/admin/preview?person={household['maya'].id}").get_json()
    assert again["total"] == 4


def test_preview_for_a_role_spans_every_folder(household):
    data = household["console"].get("/api/admin/preview?role=family").get_json()
    assert data["total"] == 7


def test_preview_accepts_a_hypothetical_folder(household):
    """"What would a guest see if I gave them this folder?" before committing."""
    data = household["console"].get(
        f"/api/admin/preview?role=family&library={household['sam_lib']}").get_json()
    assert data["total"] == 3


# ---------------------------------------------------------------------------
# One folder, several people
# ---------------------------------------------------------------------------

def test_one_folder_can_be_assigned_to_several_members(household):
    """A shared folder is the normal case — the whole family's holiday photos."""
    console, home = household["console"], household["home"]
    shared = household["maya_lib"]
    console.post(f"/api/people/{household['maya'].id}", json={"library": shared})
    console.post(f"/api/people/{household['sam'].id}", json={"library": shared})

    maya = enter(home, household["maya"].id)
    sam = enter(home, household["sam"].id)

    assert maya.get("/api/assets?limit=99").get_json()["total"] == 4
    assert sam.get("/api/assets?limit=99").get_json()["total"] == 4
    assert ({i["id"] for i in maya.get("/api/assets?limit=99").get_json()["items"]}
            == {i["id"] for i in sam.get("/api/assets?limit=99").get_json()["items"]})


def test_sharing_a_folder_keeps_favourites_private(household):
    console, home = household["console"], household["home"]
    shared = household["maya_lib"]
    console.post(f"/api/people/{household['maya'].id}", json={"library": shared})
    console.post(f"/api/people/{household['sam'].id}", json={"library": shared})

    maya = enter(home, household["maya"].id)
    sam = enter(home, household["sam"].id)
    item = maya.get("/api/assets?limit=1").get_json()["items"][0]

    maya.post(f"/api/asset/{item['id']}", json={"favorite": True, "rating": 5})

    assert maya.get(f"/api/asset/{item['id']}").get_json()["favorite"] is True
    assert sam.get(f"/api/asset/{item['id']}").get_json()["favorite"] is False
    assert sam.get(f"/api/asset/{item['id']}").get_json()["rating"] == 0
    assert maya.get("/api/assets?favorites=1").get_json()["total"] == 1
    assert sam.get("/api/assets?favorites=1").get_json()["total"] == 0


def test_members_can_share_a_folder_at_different_depths(household):
    """One on the whole folder, one on a subfolder inside it."""
    console, home = household["console"], household["home"]
    console.post(f"/api/people/{household['maya'].id}",
                 json={"library": household["maya_lib"]})
    console.post(f"/api/people/{household['sam'].id}",
                 json={"library": f"{household['maya_lib']}/trips"})

    assert enter(home, household["maya"].id).get(
        "/api/assets?limit=99").get_json()["total"] == 4
    assert enter(home, household["sam"].id).get(
        "/api/assets?limit=99").get_json()["total"] == 4


def test_console_counts_everyone_assigned_to_a_folder(household):
    console = household["console"]
    shared = household["maya_lib"]
    console.post(f"/api/people/{household['maya'].id}", json={"library": shared})
    console.post(f"/api/people/{household['sam'].id}", json={"library": shared})

    folders = {f["path"]: f for f in
               console.get("/api/admin/libraries").get_json()["folders"]}
    assert folders[shared]["assigned"] == 2
    assert folders[household["sam_lib"]]["assigned"] == 0


def test_removing_a_shared_folder_names_everyone_affected(household):
    console = household["console"]
    shared = household["maya_lib"]
    console.post(f"/api/people/{household['maya'].id}", json={"library": shared})
    console.post(f"/api/people/{household['sam'].id}", json={"library": shared})

    response = console.delete(f"/api/admin/libraries?path={shared}")
    assert response.status_code == 409
    assert set(response.get_json()["assigned"]) == {"Maya", "Sam"}


def test_unassigning_one_member_leaves_the_other(household):
    console, home = household["console"], household["home"]
    shared = household["maya_lib"]
    console.post(f"/api/people/{household['maya'].id}", json={"library": shared})
    console.post(f"/api/people/{household['sam'].id}", json={"library": shared})

    console.post(f"/api/people/{household['maya'].id}", json={"library": ""})

    assert enter(home, household["maya"].id).get(
        "/api/assets?limit=99").get_json()["total"] == 7      # back to everything
    assert enter(home, household["sam"].id).get(
        "/api/assets?limit=99").get_json()["total"] == 4      # still confined
