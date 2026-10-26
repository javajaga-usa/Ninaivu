"""Sent for screening: family members flag items, an administrator decides.

What is promised: a family member can send one item or many; sending changes
nothing about who sees it; it waits in the console's queue with who sent it;
an administrator can ignore, hide, mark explicit or delete one or many at
once, and each takes the item out of the queue; nobody but an administrator
can see the queue or decide.
"""

from conftest import GUEST, login, ids_of  # noqa: F401
from ninaivu.server import auth
from ninaivu.storage import screening


def _send(client, ids, **extra):
    response = client.post("/api/screening", json={"ids": ids, **extra})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _queue(client):
    response = client.get("/api/admin/screening")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["items"]


def _decide(client, ids, action):
    response = client.post("/api/admin/screening/decide", json={"ids": ids, "action": action})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_a_family_member_sends_one_or_many_and_they_stay_visible(as_family, as_admin, people):
    ids = ids_of(as_family)[:3]
    assert _send(as_family, ids[:1], note="Bank statement")["sent"] == 1
    body = _send(as_family, ids)
    assert body["sent"] == 2 and body["already"] == 1
    # Still in the family's gallery until somebody decides.
    assert set(ids) <= set(ids_of(as_family))
    assert as_family.get(f"/api/screening/{ids[0]}").get_json()["sent"] is True

    queue = _queue(as_admin)
    # In the order they were sent; the two sent together, by id.
    assert [item["id"] for item in queue] == [ids[0], *sorted(ids[1:])]
    first = queue[0]
    assert first["requests"][0]["name"] == people["family"].display_name
    assert first["requests"][0]["note"] == "Bank statement"
    # Counted on the Overview's "Needs you".
    attention = as_admin.get("/api/admin/attention").get_json()["items"]
    assert {i["key"]: i["count"] for i in attention}["screening"] == 3


def test_withdrawing_takes_back_only_my_request(as_family, as_admin):
    target = ids_of(as_family)[:1]
    _send(as_family, target)
    assert _send(as_family, target, sent=False)["withdrawn"] == 1
    assert _queue(as_admin) == []
    assert as_family.get(f"/api/screening/{target[0]}").get_json()["sent"] is False


def test_hide_and_explicit_take_items_out_of_the_family_view(as_family, as_admin, people):
    conn = people["conn"]
    ids = ids_of(as_family)[:4]
    _send(as_family, ids)

    body = _decide(as_admin, ids[:2], "hide")
    assert body["settled"] == 2 and body["waiting"] == 2
    body = _decide(as_admin, ids[2:3], "explicit")
    assert body["settled"] == 1 and body["waiting"] == 1
    visible = set(ids_of(as_family))
    assert not visible & set(ids[:3])
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, visibility, nsfw, nsfw_source FROM assets WHERE id IN "
        f"({','.join('?' * 3)})", ids[:3]).fetchall()}
    assert rows[ids[0]]["visibility"] == auth.VIS_HIDDEN
    assert rows[ids[2]]["nsfw"] == 1 and rows[ids[2]]["nsfw_source"] == "manual"
    # Admins still see everything.
    assert set(ids) <= set(ids_of(as_admin, "/api/assets?limit=200&nsfw=1"))

    # Ignore leaves it as it was.
    assert _decide(as_admin, ids[3:], "ignore")["settled"] == 1
    assert ids[3] in ids_of(as_family)
    assert _queue(as_admin) == []


def test_delete_moves_to_the_bin(as_family, as_admin, people):
    ids = ids_of(as_family)[:2]
    _send(as_family, ids)
    body = _decide(as_admin, ids, "delete")
    assert body["deleted"] == 2 and body["settled"] == 2
    assert body["bin"]["items"] == 2
    assert _queue(as_admin) == []
    assert screening.open_count(people["conn"]) == 0


def test_only_administrators_see_the_queue_or_decide(as_family, as_guest, anon):
    target = ids_of(as_family)[:1]
    _send(as_family, target)
    for client in (as_family, as_guest, anon):
        assert client.get("/api/admin/screening").status_code in (401, 403, 404)
        assert client.post("/api/admin/screening/decide",
                           json={"ids": target, "action": "hide"}).status_code in (401, 403, 404)
    for client in (as_guest, anon):
        assert client.post("/api/screening", json={"ids": target}).status_code in (401, 403)


def test_nothing_out_of_reach_can_be_sent(as_family, as_admin, people):
    conn = people["conn"]
    target = ids_of(as_family)[:1]
    as_admin.post("/api/visibility", json={"ids": target, "visibility": "hidden"})
    body = _send(as_family, target)
    assert body["sent"] == 0 and body["skipped"] == 1
    assert screening.open_count(conn) == 0


def test_a_bad_action_is_refused(as_admin):
    response = as_admin.post("/api/admin/screening/decide",
                             json={"ids": [1], "action": "burn"})
    assert response.status_code == 400


def test_the_capability_is_for_family_members_not_admins(as_family, as_admin):
    assert as_family.get("/api/me").get_json()["can"]["send_for_screening"] is True
    assert as_admin.get("/api/me").get_json()["can"]["send_for_screening"] is False
