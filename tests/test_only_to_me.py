""""Only to me": a family member keeping items out of everybody else's view.

What is promised: once Maya keeps a photograph to herself, no other family
member or guest can list it, count it, search for it, open it, download it or
reach it through a link; she finds it under "Only to me"; giving it back puts
back exactly the visibility it had; and nothing an administrator does in bulk
shows it again behind her back.
"""

from conftest import GUEST, login, ids_of
from ninaivu.server import auth
from ninaivu.storage import db


def _second_member(app, people):
    """Another family member, to look from the outside."""
    auth.create_user(people["conn"], "arjun", "riverstones42", display_name="Arjun",
                     role=auth.ROLE_FAMILY, created_by=people["admin"].id)
    return login(app.test_client(), "arjun", "riverstones42")


def _keep(client, ids, private=True):
    response = client.post("/api/private", json={"ids": ids, "private": private})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _segment_ids(client, query=""):
    body = client.get(f"/api/segments?{query}").get_json()
    return [item[0] for seg in body["segments"] for item in seg["items"]]


def test_private_items_leave_everybody_elses_view(app, people, as_family, as_admin, as_guest):
    other = _second_member(app, people)
    # Something the guest can see too, so the guest's view is a real test.
    target = ids_of(as_family)[0]
    as_admin.post("/api/visibility", json={"ids": [target], "visibility": "public"})
    assert target in ids_of(as_guest)
    before = as_family.get("/api/status/stats").get_json()["stats"]["count"]

    body = _keep(as_family, [target])
    assert body["updated"] == 1 and body["count"] == 1

    # Gone from the other member, the guest, and from Maya's own gallery.
    for client in (other, as_guest, as_family):
        assert target not in ids_of(client)
        assert target not in _segment_ids(client)
    for client in (other, as_guest):
        assert client.get(f"/api/file/{target}").status_code == 404
        assert client.get(f"/api/thumb/{target}").status_code in (404, 400)
    assert other.get(f"/api/assets?ids={target}").get_json()["items"] == []
    assert other.get(f"/api/download/{target}").status_code == 404
    # The other member's counts do not move by a hidden one, nor show it.
    stats = other.get("/api/status/stats").get_json()["stats"]
    assert stats["private"] == 0 and stats["count"] == before - 1

    # Maya finds it under "Only to me", and only there.
    assert _segment_ids(as_family, "private=1") == [target]
    mine = as_family.get("/api/assets?private=1").get_json()["items"]
    assert [i["id"] for i in mine] == [target] and mine[0]["private"] is True
    assert as_family.get("/api/status/stats").get_json()["stats"]["private"] == 1
    # And can open it, the way she opens anything.
    assert as_family.get(f"/api/file/{target}").status_code == 200
    assert as_family.get(f"/api/assets?ids={target}").get_json()["items"][0]["id"] == target

    # Nobody else's "Only to me" holds it.
    assert _segment_ids(other, "private=1") == []
    assert as_guest.get("/api/segments?private=1").get_json()["total"] == 0

    # The administrator still has every item.
    assert target in ids_of(as_admin)
    assert as_admin.get(f"/api/file/{target}").status_code == 200


def test_giving_it_back_restores_what_it_was(people, as_family, as_admin, scanned):
    _, conn, _ = scanned
    target = ids_of(as_family)[0]
    as_admin.post("/api/visibility", json={"ids": [target], "visibility": "public"})
    _keep(as_family, [target])
    row = conn.execute("SELECT visibility, vis_source, private_to FROM assets WHERE id=?",
                       (target,)).fetchone()
    assert (row["visibility"], row["vis_source"]) == (2, "private")
    assert row["private_to"] == people["family"].id

    assert _keep(as_family, [target], private=False)["updated"] == 1
    row = conn.execute("SELECT visibility, vis_source, private_to FROM assets WHERE id=?",
                       (target,)).fetchone()
    assert row["visibility"] == 0 and row["private_to"] is None
    assert target in ids_of(as_family)


def test_nobody_takes_or_releases_another_members_item(app, people, as_family, scanned):
    _, conn, _ = scanned
    other = _second_member(app, people)
    target = ids_of(as_family)[0]
    _keep(as_family, [target])
    # Arjun cannot reach it, so he can neither release nor claim it.
    assert _keep(other, [target], private=False)["updated"] == 0
    assert _keep(other, [target])["updated"] == 0
    assert conn.execute("SELECT private_to FROM assets WHERE id=?",
                        (target,)).fetchone()[0] == people["family"].id


def test_guests_cannot_keep_anything(as_guest, anon):
    for client in (as_guest, anon):
        response = client.post("/api/private", json={"ids": [1], "private": True})
        assert response.status_code in (401, 403)


def test_an_administrators_bulk_change_does_not_show_it_again(people, as_family, as_admin, scanned):
    _, conn, _ = scanned
    listing = as_family.get("/api/assets?limit=200&folder=shared/holiday").get_json()["items"]
    target = listing[0]["id"]
    _keep(as_family, [target])
    # Item by item, by a folder rule, and by undoing a change made before.
    as_admin.post("/api/visibility", json={"ids": [target], "visibility": "public"})
    as_admin.post("/api/visibility/folder", json={
        "folder": "shared/holiday", "visibility": "public", "confirm": True})
    db.undo_visibility_batch(conn)
    conn.execute("UPDATE assets SET visibility=0 WHERE id=?", (target,))
    conn.commit()
    row = conn.execute("SELECT visibility, private_to FROM assets WHERE id=?",
                       (target,)).fetchone()
    assert row["visibility"] == 2 and row["private_to"] == people["family"].id
    guest = login(as_admin.application.test_client(), *GUEST)
    assert target not in ids_of(guest)


def test_an_administrator_can_release_it(people, as_family, as_admin):
    target = ids_of(as_family)[0]
    _keep(as_family, [target])
    item = as_admin.get(f"/api/assets?ids={target}").get_json()["items"][0]
    assert item["private_owner"] == "Maya"
    assert _keep(as_admin, [target], private=False)["updated"] == 1
    assert target in ids_of(as_family)


def test_a_share_link_does_not_carry_a_private_item(app, people, as_family):
    target = ids_of(as_family)[0]
    response = as_family.post("/api/shares", json={"scope": "asset", "target_id": target})
    assert response.status_code in (200, 201), response.get_json()
    token = response.get_json()["token"]
    visitor = app.test_client()
    assert visitor.get(f"/api/share/{token}").status_code == 200
    _keep(as_family, [target])
    assert visitor.get(f"/api/share/{token}").status_code in (404, 410)
    assert visitor.get(f"/api/share/{token}/file/{target}").status_code in (404, 410)


def test_a_private_live_photo_takes_its_clip(people, scanned):
    _, conn, _ = scanned
    rows = conn.execute("SELECT id FROM assets ORDER BY id LIMIT 1").fetchall()
    assert db.set_private(conn, [rows[0]["id"]], people["family"].id) == 1
    assert db.set_private(conn, [rows[0]["id"]], people["admin"].id) == 0
