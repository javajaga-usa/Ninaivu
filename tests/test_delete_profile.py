"""Deleting a profile.

Disabling and deleting are different promises. Disabling is reversible: the
profile stays, keeps its favourites, and can be switched back on. Deleting is
final — the row goes, and everything personal to it goes with it. What must
*never* go is the household's media, and what must never happen is an admin
deleting the last way back into the console.
"""

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.server import auth
from ninaivu.storage import db


@pytest.fixture()
def sam(as_admin):
    """A family member to remove."""
    return as_admin.post("/api/people", json={
        "username": "sam", "password": "riverbank77", "name": "Sam",
        "role": "family",
    }).get_json()["person"]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_admin_deletes_a_profile(as_admin, sam):
    response = as_admin.delete(f"/api/people/{sam['id']}")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["removed"]["username"] == "sam"

    listed = [p["username"] for p in as_admin.get("/api/people").get_json()["people"]]
    assert "sam" not in listed


def test_a_deleted_profile_cannot_sign_in(app, as_admin, sam):
    as_admin.delete(f"/api/people/{sam['id']}")
    response = app.test_client().post(
        "/api/auth/login", json={"username": "sam", "password": "riverbank77"})
    assert response.status_code == 401


def test_deleting_ends_their_open_sessions(app, as_admin, people, as_family):
    """Someone browsing on the sofa is signed out the moment they are removed."""
    assert as_family.get("/api/me").get_json()["anonymous"] is False

    removed = as_admin.delete(f"/api/people/{people['family'].id}")
    assert removed.get_json()["removed"]["sessions"] >= 1
    assert as_family.get("/api/me").get_json()["anonymous"] is True


def test_deleting_takes_their_favourites_and_leaves_everyone_elses(
        as_admin, as_family, people, app):
    """Favourites are per person, so only the deleted person's are removed."""
    ids = [item["id"] for item in as_admin.get("/api/assets?limit=4").get_json()["items"]]
    for asset in ids:
        as_family.post(f"/api/asset/{asset}", json={"favorite": True})
        as_admin.post(f"/api/asset/{asset}", json={"favorite": True})

    assert len(as_family.get("/api/assets?favorites=1").get_json()["items"]) == len(ids)

    as_admin.delete(f"/api/people/{people['family'].id}")

    conn = db.connect(app.config["MV_CONFIG"].db_path)
    left = conn.execute("SELECT COUNT(*) n FROM user_assets WHERE user_id=?",
                        (people["family"].id,)).fetchone()["n"]
    assert left == 0, "their rows are gone"
    # The admin's own stars are untouched.
    assert len(as_admin.get("/api/assets?favorites=1").get_json()["items"]) == len(ids)


def test_deleting_a_profile_never_touches_the_media(as_admin, people, library):
    before = sorted(p.name for p in library.rglob("*.jpg"))
    total = as_admin.get("/api/assets?limit=1").get_json()["total"]

    as_admin.delete(f"/api/people/{people['family'].id}")

    assert sorted(p.name for p in library.rglob("*.jpg")) == before
    assert as_admin.get("/api/assets?limit=1").get_json()["total"] == total


def test_the_profile_picker_forgets_them(app, as_admin, sam):
    as_admin.delete(f"/api/people/{sam['id']}")
    profiles = app.test_client().get("/api/auth/profiles").get_json()["profiles"]
    assert all(p["id"] != sam["id"] for p in profiles)


def test_deletion_is_written_to_the_activity_log(as_admin, sam):
    as_admin.delete(f"/api/people/{sam['id']}")
    entries = as_admin.get("/api/audit").get_json()["entries"]
    assert any(e["action"] == "delete_person" and "sam" in (e["detail"] or "")
               for e in entries), entries


def test_their_avatar_file_is_removed_from_disk(app, as_admin, people):
    """The picture lives on disk, not in the database."""
    from io import BytesIO

    from PIL import Image

    client = login(app.test_client(), *FAMILY)
    buffer = BytesIO()
    Image.new("RGB", (80, 80), (10, 90, 200)).save(buffer, "PNG")
    buffer.seek(0)
    uploaded = client.post(
        "/api/me/avatar",
        data={"avatar": (buffer, "face.png")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200, uploaded.get_json()

    avatars = app.config["MV_CONFIG"].state_dir / "avatars"
    assert list(avatars.glob("*")), "the upload must have landed"

    as_admin.delete(f"/api/people/{people['family'].id}")
    assert not list(avatars.glob("*")), "the picture goes with the profile"


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------

def test_an_admin_cannot_delete_themselves(as_admin, people):
    response = as_admin.delete(f"/api/people/{people['admin'].id}")
    assert response.status_code == 409
    assert "signed in with" in response.get_json()["error"]
    # And they are still there.
    assert as_admin.get("/api/me").get_json()["anonymous"] is False


def test_the_last_administrator_cannot_be_deleted(app, people, as_admin):
    """Even by another admin — the household must keep a way in."""
    as_admin.post(f"/api/people/{people['family'].id}", json={"role": "admin"})
    second = login(app.test_client(), *FAMILY)

    # Two admins: removing one is fine.
    assert second.delete(f"/api/people/{people['admin'].id}").status_code == 200

    # One left, and it is the one signed in — refused twice over.
    assert second.delete(f"/api/people/{people['family'].id}").status_code == 409


def test_a_disabled_admin_does_not_count_as_a_way_back_in(app, people, as_admin):
    """Deleting the only *working* admin is refused even if a disabled one exists."""
    as_admin.post(f"/api/people/{people['family'].id}", json={"role": "admin"})
    second = login(app.test_client(), *FAMILY)
    # Park the original admin as disabled, then try to delete the live one.
    second.post(f"/api/people/{people['admin'].id}", json={"active": False})

    response = second.delete(f"/api/people/{people['family'].id}")
    assert response.status_code == 409, response.get_json()


def test_deleting_an_unknown_profile_is_a_404(as_admin):
    assert as_admin.delete("/api/people/98765").status_code == 404


@pytest.mark.parametrize("who", ["family", "guest"])
def test_only_an_admin_may_delete(app, people, who):
    client = login(app.test_client(), *(FAMILY if who == "family" else GUEST))
    response = client.delete(f"/api/people/{people['guest'].id}")
    assert response.status_code == 403
    assert auth.get_user(people["conn"], people["guest"].id) is not None


def test_an_anonymous_visitor_may_not_delete(anon, people):
    assert anon.delete(f"/api/people/{people['guest'].id}").status_code in (401, 403)


def test_the_family_app_has_no_delete_route_at_all(scanned):
    """Port 5000 must not even route it — a 404, not a 403."""
    from ninaivu import build_services, create_home_app

    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    victim = auth.create_user(conn, "sam", "riverbank77", display_name="Sam",
                              role=auth.ROLE_FAMILY, created_by=admin.id)
    services = build_services(cfg)
    services.scanner.stop()
    home = login(create_home_app(services).test_client(), *ADMIN)

    assert home.delete(f"/api/people/{victim.id}").status_code == 404
    assert auth.get_user(conn, victim.id) is not None


# ---------------------------------------------------------------------------
# The database layer on its own
# ---------------------------------------------------------------------------

def test_delete_user_reports_what_it_removed(app, people):
    conn = db.connect(app.config["MV_CONFIG"].db_path)
    member = people["family"].id
    conn.execute(
        "INSERT OR REPLACE INTO user_assets(user_id, asset_id, favorite) "
        "SELECT ?, id, 1 FROM assets LIMIT 3", (member,))
    conn.commit()

    removed = auth.delete_user(conn, member)
    assert removed["username"] == FAMILY[0]
    assert removed["favorites"] == 3
    assert auth.get_user(conn, member) is None


def test_delete_user_keeps_folder_rules_the_person_made(app, people, as_admin):
    """A visibility decision belongs to the library, not to whoever made it."""
    conn = db.connect(app.config["MV_CONFIG"].db_path)
    as_admin.post(f"/api/people/{people['family'].id}", json={"role": "admin"})
    second = login(app.test_client(), *FAMILY)
    second.post("/api/visibility/folder", json={"folder": "private", "visibility": "hidden", "confirm": True})

    before = conn.execute("SELECT COUNT(*) n FROM folder_rules").fetchone()["n"]
    assert before >= 1

    auth.delete_user(conn, people["family"].id)

    after = conn.execute(
        "SELECT COUNT(*) n, SUM(created_by IS NULL) orphaned FROM folder_rules"
    ).fetchone()
    assert after["n"] == before, "the rule survives its author"
    assert after["orphaned"] >= 1, "but no longer points at a deleted row"


def test_delete_user_on_a_missing_row_raises(app):
    conn = db.connect(app.config["MV_CONFIG"].db_path)
    with pytest.raises(ValueError):
        auth.delete_user(conn, 4242)


def test_profiles_created_by_the_deleted_admin_survive(app, people, as_admin, sam):
    """Removing whoever added someone must not remove them too."""
    conn = db.connect(app.config["MV_CONFIG"].db_path)
    as_admin.post(f"/api/people/{people['family'].id}", json={"role": "admin"})
    second = login(app.test_client(), *FAMILY)

    assert second.delete(f"/api/people/{people['admin'].id}").status_code == 200
    assert auth.get_user(conn, sam["id"]) is not None, "Sam was created by that admin"
