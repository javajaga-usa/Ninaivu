"""Role behaviour — what each of the three roles can and cannot do.

These are the tests that matter most: a permission bug here is a privacy leak,
not a cosmetic defect, so every capability is asserted from *both* directions —
the role that should have it, and the roles that should not.
"""

import io

import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, GUEST, login


def make_image_bytes(size=(300, 300), fmt="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 30, 200)).save(buffer, fmt)
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------------------
# Setup and sign-in
# ---------------------------------------------------------------------------

def test_first_run_requires_setup(client):
    state = client.get("/api/auth/state").get_json()
    assert state["setup_required"] is True
    assert state["user"]["role"] == "guest"
    assert state["app_name"] == "Ninaivu"


def test_setup_creates_admin_and_signs_in(client):
    response = client.post("/api/auth/setup", json={
        "username": "dad", "password": "correcthorse1", "name": "Dad",
    })
    assert response.status_code == 200
    assert response.get_json()["user"]["role"] == "admin"
    assert client.get("/api/me").get_json()["role"] == "admin"


def test_setup_is_refused_once_an_admin_exists(client, people):
    response = client.post("/api/auth/setup", json={
        "username": "sneak", "password": "notallowed123",
    })
    assert response.status_code == 409


def test_setup_rejects_weak_credentials(client):
    assert client.post("/api/auth/setup", json={
        "username": "dad", "password": "short"}).status_code == 400
    assert client.post("/api/auth/setup", json={
        "username": "Bad Name!", "password": "longenough12"}).status_code == 400


def test_login_and_logout(app, people):
    client = app.test_client()
    assert client.post("/api/auth/login", json={
        "username": FAMILY[0], "password": "wrong"}).status_code == 401
    login(client, *FAMILY)
    assert client.get("/api/me").get_json()["username"] == FAMILY[0]
    client.post("/api/auth/logout")
    assert client.get("/api/me").get_json()["anonymous"] is True


def test_disabled_profile_cannot_sign_in(app, people, as_admin):
    as_admin.post(f"/api/people/{people['family'].id}", json={"active": False})
    response = app.test_client().post("/api/auth/login", json={
        "username": FAMILY[0], "password": FAMILY[1]})
    assert response.status_code == 401


def test_disabling_a_profile_ends_its_sessions(app, people, as_admin, as_family):
    assert as_family.get("/api/me").get_json()["anonymous"] is False
    as_admin.post(f"/api/people/{people['family'].id}", json={"active": False})
    # The open session is gone, so the same client is anonymous again.
    assert as_family.get("/api/me").get_json()["anonymous"] is True


def test_password_hash_is_never_returned(as_admin, people):
    """The people list may say *whether* a password is set, never what it is."""
    blob = as_admin.get("/api/people").get_data(as_text=True)
    assert "scrypt$" not in blob
    for secret in (ADMIN[1], FAMILY[1], GUEST[1]):
        assert secret not in blob
    # The metadata that is allowed through:
    payload = as_admin.get("/api/people").get_json()["people"][0]
    assert isinstance(payload["has_password"], bool)
    assert "pin" not in payload


# ---------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------

def test_capability_flags_match_role(as_admin, as_family, as_guest, anon):
    admin = as_admin.get("/api/me").get_json()["can"]
    family = as_family.get("/api/me").get_json()["can"]
    guest = as_guest.get("/api/me").get_json()["can"]
    visitor = anon.get("/api/me").get_json()["can"]

    assert admin == {"download": True, "favorite": True, "rotate": True,
                     "set_visibility": True, "delete_media": True,
                     "manage_people": True, "manage_library": True, "see_hidden": True}
    assert family["download"] and family["favorite"] and family["rotate"]
    assert not family["set_visibility"] and not family["manage_people"]
    assert not any(guest.values())
    assert not any(visitor.values())

    # Deleting media is the one management action that lives in the gallery
    # rather than the console, so it is worth stating on its own: it is still
    # an admin action, and the gallery reads this flag rather than guessing
    # from the role.
    assert admin["delete_media"] is True
    assert family["delete_media"] is False
    assert guest["delete_media"] is False
    assert visitor["delete_media"] is False


@pytest.mark.parametrize("path,method", [
    ("/api/people", "get"),
    ("/api/people/folders", "get"),
    ("/api/audit", "get"),
    ("/api/visibility/rules", "get"),
    ("/api/library/browse", "get"),
    ("/api/events", "get"),
])
def test_admin_only_reads(as_admin, as_family, as_guest, path, method):
    assert getattr(as_admin, method)(path).status_code == 200
    assert getattr(as_family, method)(path).status_code == 403
    assert getattr(as_guest, method)(path).status_code == 403


@pytest.mark.parametrize("path,payload", [
    ("/api/people", {"username": "x1", "password": "abcdefgh12"}),
    ("/api/visibility", {"ids": [1], "visibility": "hidden"}),
    ("/api/visibility/folder", {"folder": "private", "visibility": "hidden"}),
    ("/api/scan", {}),
    ("/api/settings", {"nsfw_filter": False}),
    ("/api/library/root", {"path": "/tmp"}),
])
def test_admin_only_writes(as_admin, as_family, as_guest, path, payload):
    assert as_family.post(path, json=payload).status_code == 403
    assert as_guest.post(path, json=payload).status_code == 403


def test_anonymous_visitor_is_told_to_sign_in(anon):
    response = anon.post("/api/visibility", json={"ids": [1], "visibility": "public"})
    assert response.status_code == 401
    assert "sign in" in response.get_json()["error"].lower()


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

def test_new_media_defaults_to_family_only(as_admin, as_guest):
    admin_total = as_admin.get("/api/segments").get_json()["total"]
    guest_total = as_guest.get("/api/segments").get_json()["total"]
    assert admin_total == 15
    assert guest_total == 0, "nothing should be public until an admin says so"


def test_admin_can_publish_a_folder_to_guests(as_admin, as_guest):
    response = as_admin.post("/api/visibility/folder", json={
        "folder": "shared/holiday", "visibility": "public", "confirm": True})
    assert response.status_code == 200
    assert response.get_json()["updated"] == 4

    items = as_guest.get("/api/assets").get_json()["items"]
    assert len(items) == 4
    assert all(item["folder"] == "shared/holiday" for item in items)


def test_admin_can_hide_a_folder_from_family(as_admin, as_family):
    as_admin.post("/api/visibility/folder", json={
        "folder": "private", "visibility": "hidden", "confirm": True})

    family_names = {i["name"] for i in as_family.get("/api/assets?limit=99").get_json()["items"]}
    admin_names = {i["name"] for i in as_admin.get("/api/assets?limit=99").get_json()["items"]}
    assert "secret0.jpg" in admin_names
    assert not any(n.startswith("secret") for n in family_names)


def test_hidden_items_are_unreachable_by_id(as_admin, as_family, as_guest):
    secret = next(i for i in as_admin.get("/api/assets?limit=99").get_json()["items"]
                  if i["name"] == "secret0.jpg")
    as_admin.post("/api/visibility", json={"ids": [secret["id"]], "visibility": "hidden"})

    for client in (as_family, as_guest):
        assert client.get(f"/api/asset/{secret['id']}").status_code == 404
        assert client.get(f"/api/thumb/{secret['id']}").status_code == 404
        assert client.get(f"/api/file/{secret['id']}").status_code == 404
    assert as_admin.get(f"/api/asset/{secret['id']}").status_code == 200


def test_family_only_items_are_unreachable_by_a_guest(as_admin, as_guest):
    item = as_admin.get("/api/assets?limit=1").get_json()["items"][0]
    assert as_guest.get(f"/api/asset/{item['id']}").status_code == 404
    assert as_guest.get(f"/api/file/{item['id']}").status_code == 404


def test_visibility_survives_a_rescan(scanned, as_admin):
    from pathlib import Path

    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    as_admin.post("/api/visibility/folder", json={
        "folder": "private", "visibility": "hidden", "confirm": True})

    Scanner(cfg)._run(Path(cfg.active_root), full=True)

    rows = conn.execute(
        "SELECT visibility FROM assets WHERE folder='private'").fetchall()
    assert rows and all(r["visibility"] == 2 for r in rows)


def test_folder_rule_applies_to_newly_added_files(scanned, as_admin):
    from pathlib import Path

    from PIL import Image as PILImage

    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    as_admin.post("/api/visibility/folder", json={
        "folder": "private", "visibility": "hidden", "confirm": True})

    new_file = Path(cfg.active_root) / "private" / "secret_new.jpg"
    PILImage.new("RGB", (100, 100), (1, 2, 3)).save(new_file)
    Scanner(cfg)._run(Path(cfg.active_root), full=False)

    row = conn.execute(
        "SELECT visibility, vis_source FROM assets WHERE filename='secret_new.jpg'"
    ).fetchone()
    assert row["visibility"] == 2
    assert row["vis_source"] == "folder"


def test_guest_cannot_widen_visibility_via_query_string(as_admin, as_guest):
    as_admin.post("/api/visibility/folder", json={
        "folder": "shared/holiday", "visibility": "public", "confirm": True})
    # Asking for hidden items as a guest must not reveal anything extra.
    sneaky = as_guest.get("/api/assets?visibility=hidden&trashed=1&limit=99").get_json()
    assert all(i["visibility"] == "public" for i in sneaky["items"])


def test_facets_and_timeline_respect_visibility(as_admin, as_guest):
    as_admin.post("/api/visibility/folder", json={
        "folder": "shared/holiday", "visibility": "public", "confirm": True})
    folders = {f["name"] for f in as_guest.get("/api/facets").get_json()["folders"]}
    assert folders == {"shared/holiday"}
    assert "private" not in folders

    days = as_guest.get("/api/timeline").get_json()["days"]
    assert len(days) <= 1


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_admin_can_scope_a_family_member_to_a_folder(app, people, as_admin):
    as_admin.post(f"/api/people/{people['family'].id}",
                  json={"scope": "shared/holiday"})

    scoped = login(app.test_client(), *FAMILY)
    items = scoped.get("/api/assets?limit=99").get_json()["items"]
    assert len(items) == 4
    assert all(i["folder"] == "shared/holiday" for i in items)
    assert scoped.get("/api/me").get_json()["scope"] == "shared/holiday"


def test_scope_blocks_direct_access_to_outside_items(app, people, as_admin):
    outside = next(i for i in as_admin.get("/api/assets?limit=99").get_json()["items"]
                   if i["folder"].startswith("2023"))
    as_admin.post(f"/api/people/{people['family'].id}",
                  json={"scope": "shared/holiday"})

    scoped = login(app.test_client(), *FAMILY)
    assert scoped.get(f"/api/asset/{outside['id']}").status_code == 404
    assert scoped.get(f"/api/file/{outside['id']}").status_code == 404
    assert scoped.get(f"/api/download/{outside['id']}").status_code == 404


def test_scope_and_visibility_stack_for_a_guest(app, people, as_admin):
    # Folder by folder: the library root cannot be set in one go any more.
    for folder in ("shared", "2023", "2019", "private", "misc"):
        as_admin.post("/api/visibility/folder", json={
            "folder": folder, "visibility": "public", "confirm": True})
    as_admin.post(f"/api/people/{people['guest'].id}", json={"scope": "shared"})

    scoped = login(app.test_client(), *GUEST)
    items = scoped.get("/api/assets?limit=99").get_json()["items"]
    assert len(items) == 4  # everything public, but only inside shared/

    as_admin.post("/api/visibility/folder", json={
        "folder": "shared/holiday", "visibility": "family", "confirm": True})
    assert scoped.get("/api/assets?limit=99").get_json()["items"] == []


def test_scope_ignores_traversal(app, people, as_admin):
    as_admin.post(f"/api/people/{people['family'].id}",
                  json={"scope": "../../../etc"})
    stored = as_admin.get("/api/people").get_json()["people"]
    scope = next(p["scope"] for p in stored if p["username"] == FAMILY[0])
    assert ".." not in (scope or "")


def test_admins_are_never_scoped(app, people, as_admin):
    as_admin.post(f"/api/people/{people['admin'].id}", json={"scope": "private"})
    assert as_admin.get("/api/me").get_json()["scope"] is None
    assert as_admin.get("/api/segments").get_json()["total"] == 15


def test_scope_folder_list_is_available_to_admin(as_admin):
    folders = as_admin.get("/api/people/folders").get_json()["folders"]
    paths = {f["path"] for f in folders}
    assert "shared" in paths and "shared/holiday" in paths
    parent = next(f for f in folders if f["path"] == "shared")
    assert parent["count"] == 4, "counts should roll up into parent folders"


# ---------------------------------------------------------------------------
# Downloads and favourites
# ---------------------------------------------------------------------------

def test_only_family_and_admin_can_download(as_admin, as_family, as_guest):
    item = as_admin.get("/api/assets?limit=1").get_json()["items"][0]
    as_admin.post("/api/visibility", json={"ids": [item["id"]], "visibility": "public"})

    assert as_admin.get(f"/api/download/{item['id']}").status_code == 200
    assert as_family.get(f"/api/download/{item['id']}").status_code == 200
    assert as_guest.get(f"/api/download/{item['id']}").status_code == 403


def test_guest_payload_omits_download_and_exif(as_admin, as_guest):
    # Pick a shot that actually carries EXIF, so the admin side is meaningful.
    item = next(i for i in as_admin.get("/api/assets?limit=99").get_json()["items"]
                if i.get("camera"))
    as_admin.post("/api/visibility", json={"ids": [item["id"]], "visibility": "public"})

    seen = as_guest.get(f"/api/asset/{item['id']}").get_json()
    assert "download" not in seen
    assert "camera" not in seen
    assert "gps" not in seen
    assert as_admin.get(f"/api/asset/{item['id']}").get_json()["camera"] == "Acme TestCam"


def test_favourites_are_per_person(as_admin, as_family):
    item = as_admin.get("/api/assets?limit=1").get_json()["items"][0]
    as_family.post(f"/api/asset/{item['id']}", json={"favorite": True})

    assert as_family.get(f"/api/asset/{item['id']}").get_json()["favorite"] is True
    assert as_admin.get(f"/api/asset/{item['id']}").get_json()["favorite"] is False
    assert as_family.get("/api/assets?favorites=1").get_json()["total"] == 1
    assert as_admin.get("/api/assets?favorites=1").get_json()["total"] == 0


def test_ratings_are_per_person(as_admin, as_family):
    item = as_admin.get("/api/assets?limit=1").get_json()["items"][0]
    as_family.post(f"/api/asset/{item['id']}", json={"rating": 5})
    as_admin.post(f"/api/asset/{item['id']}", json={"rating": 2})

    assert as_family.get(f"/api/asset/{item['id']}").get_json()["rating"] == 5
    assert as_admin.get(f"/api/asset/{item['id']}").get_json()["rating"] == 2


def test_guest_cannot_favourite(as_admin, as_guest):
    item = as_admin.get("/api/assets?limit=1").get_json()["items"][0]
    as_admin.post("/api/visibility", json={"ids": [item["id"]], "visibility": "public"})
    assert as_guest.post(f"/api/asset/{item['id']}",
                         json={"favorite": True}).status_code == 403


def test_family_cannot_retag_the_library(as_admin, as_family):
    item = as_admin.get("/api/assets?limit=1").get_json()["items"][0]
    response = as_family.post(f"/api/asset/{item['id']}", json={"tags": ["mine"]})
    assert response.status_code == 403
    assert as_admin.get(f"/api/asset/{item['id']}").get_json()["tags"] != ["mine"]


def test_bulk_update_skips_items_the_caller_cannot_see(as_admin, as_family):
    everything = [i["id"] for i in as_admin.get("/api/assets?limit=99").get_json()["items"]]
    as_admin.post("/api/visibility/folder", json={
        "folder": "private", "visibility": "hidden", "confirm": True})

    result = as_family.post("/api/assets/bulk",
                            json={"ids": everything, "favorite": True}).get_json()
    assert result["skipped"] == 3
    assert result["updated"] == len(everything) - 3


# ---------------------------------------------------------------------------
# People management
# ---------------------------------------------------------------------------

def test_admin_creates_and_disables_a_family_member(as_admin):
    created = as_admin.post("/api/people", json={
        "username": "sam", "password": "riverbank77", "name": "Sam",
        "role": "family",
    })
    assert created.status_code == 200
    person = created.get_json()["person"]
    assert person["role"] == "family"
    assert person["must_change"] is True

    disabled = as_admin.post(f"/api/people/{person['id']}", json={"active": False})
    assert disabled.get_json()["person"]["active"] is False


def test_duplicate_usernames_are_refused(as_admin):
    as_admin.post("/api/people", json={"username": "sam", "password": "riverbank77"})
    again = as_admin.post("/api/people", json={"username": "SAM", "password": "riverbank77"})
    assert again.status_code == 400
    assert "taken" in again.get_json()["error"]


def test_last_admin_cannot_be_demoted_or_disabled(as_admin, people):
    admin_id = people["admin"].id
    assert as_admin.post(f"/api/people/{admin_id}",
                         json={"role": "family"}).status_code == 409
    assert as_admin.post(f"/api/people/{admin_id}",
                         json={"active": False}).status_code == 409


def test_admin_can_promote_a_member_then_step_down(as_admin, people, app):
    as_admin.post(f"/api/people/{people['family'].id}", json={"role": "admin"})
    assert as_admin.post(f"/api/people/{people['admin'].id}",
                         json={"role": "family"}).status_code == 200

    promoted = login(app.test_client(), *FAMILY)
    assert promoted.get("/api/me").get_json()["role"] == "admin"


def test_admin_password_reset_forces_a_change(as_admin, people, app):
    as_admin.post(f"/api/people/{people['family'].id}",
                  json={"password": "brandnewpass1"})
    client = login(app.test_client(), FAMILY[0], "brandnewpass1")
    assert client.get("/api/me").get_json()["must_change"] is True


def test_admin_can_sign_a_person_out_everywhere(as_admin, people, as_family):
    assert as_family.get("/api/me").get_json()["anonymous"] is False
    response = as_admin.post(f"/api/people/{people['family'].id}/signout")
    assert response.get_json()["sessions_ended"] >= 1
    assert as_family.get("/api/me").get_json()["anonymous"] is True


# ---------------------------------------------------------------------------
# Profile pictures — every role may set their own
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["admin", "family", "guest"])
def test_every_role_can_set_their_own_picture(app, people, role):
    credentials = {"admin": ADMIN, "family": FAMILY, "guest": GUEST}[role]
    client = login(app.test_client(), *credentials)

    response = client.post("/api/me/avatar", data={
        "avatar": (make_image_bytes(), "me.png"),
    }, content_type="multipart/form-data")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["user"]["avatar"]

    served = client.get(f"/api/avatar/{people[role].id}")
    assert served.status_code == 200
    assert served.headers["Content-Type"] == "image/webp"


def test_avatar_is_re_encoded_and_square(app, people, as_family):
    as_family.post("/api/me/avatar", data={
        "avatar": (make_image_bytes((800, 300)), "wide.png"),
    }, content_type="multipart/form-data")
    served = as_family.get(f"/api/avatar/{people['family'].id}")
    with Image.open(io.BytesIO(served.data)) as img:
        assert img.size == (256, 256)
        assert img.format == "WEBP"


def test_avatar_rejects_non_images(as_family):
    response = as_family.post("/api/me/avatar", data={
        "avatar": (io.BytesIO(b"#!/bin/sh\nrm -rf /"), "evil.png"),
    }, content_type="multipart/form-data")
    assert response.status_code == 400


def test_avatar_requires_sign_in(anon):
    response = anon.post("/api/me/avatar", data={
        "avatar": (make_image_bytes(), "me.png"),
    }, content_type="multipart/form-data")
    assert response.status_code in (401, 403)


def test_nobody_can_change_someone_elses_picture(as_family, people):
    """There is no route to do it — the only avatar write is /api/me/avatar."""
    response = as_family.post(f"/api/people/{people['admin'].id}", json={"name": "X"})
    assert response.status_code == 403


def test_profile_name_and_colour_are_self_service(as_guest):
    response = as_guest.post("/api/me", json={"name": "Next Door", "color": "#ff8800"})
    assert response.status_code == 200
    assert response.get_json()["name"] == "Next Door"
    assert response.get_json()["color"] == "#ff8800"


def test_changing_own_password_requires_the_current_one(as_family, app):
    bad = as_family.post("/api/me/password", json={
        "current": "nope", "password": "anotherpass99"})
    assert bad.status_code == 403

    good = as_family.post("/api/me/password", json={
        "current": FAMILY[1], "password": "anotherpass99"})
    assert good.status_code == 200
    login(app.test_client(), FAMILY[0], "anotherpass99")


# ---------------------------------------------------------------------------
# Open vs private browsing
# ---------------------------------------------------------------------------

def test_open_browsing_shows_public_media_without_a_login(as_admin, anon):
    as_admin.post("/api/visibility/folder", json={
        "folder": "shared/holiday", "visibility": "public", "confirm": True})
    items = anon.get("/api/assets?limit=99").get_json()["items"]
    assert len(items) == 4


def test_private_mode_blocks_everything_until_sign_in(app, people, cfg):
    cfg.open_browsing = False
    visitor = app.test_client()
    assert visitor.get("/api/assets").status_code == 401
    assert visitor.get("/api/segments").status_code == 401
    # The login screen itself must still load.
    assert visitor.get("/").status_code == 200
    assert visitor.get("/api/auth/state").status_code == 200

    signed_in = login(app.test_client(), *FAMILY)
    assert signed_in.get("/api/assets").status_code == 200


def test_status_hides_the_library_path_from_non_admins(as_admin, as_family, as_guest):
    assert as_admin.get("/api/status").get_json()["root"]
    assert as_family.get("/api/status").get_json()["root"] is None
    assert as_guest.get("/api/status").get_json()["root"] is None
    # …but they still get a friendly label.
    assert as_family.get("/api/status").get_json()["root_label"]


def test_nsfw_photos_never_served_to_family_or_guest(as_admin, as_family, as_guest, scanned):
    cfg, conn, _ = scanned
    asset = conn.execute("SELECT id FROM assets WHERE visibility <= 1 LIMIT 1").fetchone()
    nsfw_id = asset["id"]
    conn.execute("UPDATE assets SET nsfw = 1 WHERE id = ?", (nsfw_id,))
    conn.commit()

    # Family app listings never show it, even if requested with ?nsfw=1
    fam_assets = as_family.get("/api/assets?nsfw=1").get_json()["items"]
    assert nsfw_id not in [a["id"] for a in fam_assets]

    guest_assets = as_guest.get("/api/assets?nsfw=1").get_json()["items"]
    assert nsfw_id not in [a["id"] for a in guest_assets]

    # Direct access is blocked with 404 for non-admins
    assert as_family.get(f"/api/file/{nsfw_id}").status_code == 404
    assert as_family.get(f"/api/thumb/{nsfw_id}").status_code == 404
    assert as_family.get(f"/api/asset/{nsfw_id}").status_code == 404
    assert as_family.get(f"/api/download/{nsfw_id}").status_code == 404
    assert as_guest.get(f"/api/file/{nsfw_id}").status_code == 404
    assert as_guest.get(f"/api/thumb/{nsfw_id}").status_code == 404

    # Admin app CAN see and access it for control
    admin_assets = as_admin.get("/api/assets?nsfw=1").get_json()["items"]
    assert nsfw_id in [a["id"] for a in admin_assets]
    assert as_admin.get(f"/api/file/{nsfw_id}").status_code == 200

