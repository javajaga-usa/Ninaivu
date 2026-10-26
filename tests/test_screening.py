"""Sent for screening: family members flag items, an administrator decides.

What is promised: a family member can send one item or many; it leaves every
family member's view at once, everywhere (gallery, albums, search, files),
while administrators still see it; it waits in the console's queue with who
sent it; an administrator can ignore (it comes back as it was), hide, mark
explicit or delete one or many at once, and each takes the item out of the
queue; the AI's reading of a held item is kept; nobody but an administrator
can see the queue or decide.
"""

from conftest import GUEST, login, ids_of  # noqa: F401
from ninaivu.server import auth
from ninaivu.storage import db, screening


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


def _second_member(app, people):
    """Another family member, to look from the outside."""
    auth.create_user(people["conn"], "arjun", "riverstones42", display_name="Arjun",
                     role=auth.ROLE_FAMILY, created_by=people["admin"].id)
    return login(app.test_client(), "arjun", "riverstones42")


def test_a_family_member_sends_one_or_many_and_they_vanish_at_once(app, as_family, as_admin,
                                                                   people):
    other = _second_member(app, people)
    ids = ids_of(as_family)[:3]
    album = other.post("/api/albums", json={"name": "Trip", "ids": ids}).get_json()["id"]
    assert _send(as_family, ids[:1], note="Bank statement")["sent"] == 1
    body = _send(as_family, ids[1:])
    assert body["sent"] == 2
    # Gone for every family member, the sender too, in the gallery, an album,
    # and the file itself; administrators still have them.
    for client in (as_family, other):
        assert not set(ids) & set(ids_of(client))
        assert not set(ids) & set(ids_of(client, f"/api/assets?album={album}&limit=200"))
        assert client.get(f"/api/file/{ids[0]}").status_code == 404
    assert set(ids) <= set(ids_of(as_admin))
    assert as_family.get(f"/api/screening/{ids[0]}").get_json()["sent"] is True

    queue = _queue(as_admin)
    # In the order they were sent; the two sent together, by id.
    assert [item["id"] for item in queue] == [ids[0], *sorted(ids[1:])]
    first = queue[0]
    assert first["held"] is True
    assert first["requests"][0]["name"] == people["family"].display_name
    assert first["requests"][0]["note"] == "Bank statement"
    # Counted on the Overview's "Needs you".
    attention = as_admin.get("/api/admin/attention").get_json()["items"]
    assert {i["key"]: i["count"] for i in attention}["screening"] == 3


def test_withdrawing_brings_it_back(as_family, as_admin):
    target = ids_of(as_family)[:1]
    _send(as_family, target)
    assert target[0] not in ids_of(as_family)
    assert _send(as_family, target, sent=False)["withdrawn"] == 1
    assert _queue(as_admin) == []
    assert target[0] in ids_of(as_family)
    assert as_family.get(f"/api/screening/{target[0]}").get_json()["sent"] is False


def test_ignore_puts_back_exactly_what_it_was(as_family, as_admin, people):
    conn = people["conn"]
    target = ids_of(as_family)[:1]
    as_admin.post("/api/visibility", json={"ids": target, "visibility": "public"})
    _send(as_family, target)
    row = conn.execute("SELECT visibility, vis_source FROM assets WHERE id=?", target).fetchone()
    assert (row["visibility"], row["vis_source"]) == (2, screening.HOLD_SOURCE)
    assert _decide(as_admin, target, "ignore")["settled"] == 1
    row = conn.execute("SELECT visibility, vis_source FROM assets WHERE id=?", target).fetchone()
    assert (row["visibility"], row["vis_source"]) == (0, "item")
    assert target[0] in ids_of(as_family)


def test_a_held_item_keeps_what_the_ai_read(as_family, as_admin, people):
    conn = people["conn"]
    target = ids_of(as_family)[:1]
    conn.execute("UPDATE assets SET caption='a beach', caption_source='ai', ai_version=5 "
                 "WHERE id=?", target)
    conn.commit()
    _send(as_family, target)
    # The pass that takes back what AI read of hidden items, as a scan runs it.
    db.forget_ai_reading(conn)
    row = conn.execute("SELECT caption, ai_version FROM assets WHERE id=?", target).fetchone()
    assert (row["caption"], row["ai_version"]) == ("a beach", 5)
    _decide(as_admin, target, "ignore")
    row = conn.execute("SELECT caption FROM assets WHERE id=?", target).fetchone()
    assert row["caption"] == "a beach"


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
    assert conn.execute("SELECT vis_source FROM assets WHERE id=?",
                        ids[:1]).fetchone()[0] == "item"
    assert rows[ids[2]]["nsfw"] == 1 and rows[ids[2]]["nsfw_source"] == "manual"
    # Admins still see everything.
    assert set(ids) <= set(ids_of(as_admin, "/api/assets?limit=200&nsfw=1"))

    # Ignore brings it back.
    assert ids[3] not in ids_of(as_family)
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


def _live_pair(conn, still: int, clip: int) -> None:
    """Make *clip* the live clip of *still*, as the scan pairs them."""
    path = conn.execute("SELECT rel_path FROM assets WHERE id=?", (clip,)).fetchone()[0]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET is_live=1, live_video_path=? WHERE id=?", (path, still))
        conn.execute("UPDATE assets SET live_clip=1 WHERE id=?", (clip,))
        conn.commit()


def _vis(conn, asset_id):
    return conn.execute("SELECT visibility FROM assets WHERE id=?", (asset_id,)).fetchone()[0]


def test_nobody_else_can_bring_back_a_held_clip_while_its_still_waits(app, as_family, people):
    """The clip is held with its still, on the still's request. Somebody who
    had not sent it took back a request for the clip alone, and the clip came
    back to the family while the still still waited (audit 2026-10-26 F-01)."""
    conn = people["conn"]
    still, clip = ids_of(as_family)[:2]
    _live_pair(conn, still, clip)
    _send(as_family, [still])
    assert _vis(conn, still) == 2 and _vis(conn, clip) == 2
    other = _second_member(app, people)
    for ids in ([clip], [still], [still, clip]):
        assert _send(other, ids, sent=False)["withdrawn"] == 0
        assert _vis(conn, still) == 2 and _vis(conn, clip) == 2
    assert other.get(f"/api/asset/{clip}").status_code == 404
    # The one who sent it takes it back: both come back together.
    assert _send(as_family, [still], sent=False)["withdrawn"] == 1
    assert _vis(conn, still) == 1 and _vis(conn, clip) == 1


def test_a_clip_stays_held_while_anybody_still_asks_about_its_still(app, as_family, people):
    conn = people["conn"]
    still, clip = ids_of(as_family)[:2]
    _live_pair(conn, still, clip)
    other = _second_member(app, people)
    _send(as_family, [still])
    # Asked by a second person too (held, it is out of their view, so as two
    # requests made side by side would leave it).
    arjun = conn.execute("SELECT id FROM users WHERE username='arjun'").fetchone()[0]
    screening.send(conn, [still], arjun)
    assert _send(as_family, [still, clip], sent=False)["withdrawn"] == 1
    assert _vis(conn, still) == 2 and _vis(conn, clip) == 2
    assert _send(other, [still], sent=False)["withdrawn"] == 1
    assert _vis(conn, still) == 1 and _vis(conn, clip) == 1
