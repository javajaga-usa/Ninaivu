"""The visibility gate (server/visibility.py): one Viewer per request.

What each kind of caller is allowed — the administrator, a family member, one
kept to a folder, one whose folder is gone, a guest, a visitor nobody signed
in as — decided by the Viewer for one item (``may_see``) and for a listing
(``sql``), under no date limit and under one. The two answers must agree with
each other and with what the helpers the routes used before the gate answer.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask import g
from werkzeug.exceptions import Conflict

from ninaivu.server import auth, date_policy, visibility
from ninaivu.storage import db


@pytest.fixture()
def house(app, scanned, people):
    """The fixture library with an item in every state the gate decides on."""
    cfg, conn, _ = scanned
    ids = {r["rel_path"]: int(r["id"]) for r in conn.execute("SELECT id, rel_path FROM assets")}
    lib = Path(cfg.active_root)
    members = {
        "admin": people["admin"], "family": people["family"], "guest": people["guest"],
        "anon": auth.ANONYMOUS,
        "sam": auth.create_user(conn, "sam", "samsdays1234", role=auth.ROLE_FAMILY,
                                library=str(lib / "shared")),
        "kid": auth.create_user(conn, "kid", "kidsdays1234", role=auth.ROLE_GUEST,
                                scope="shared"),
        "gone": auth.create_user(conn, "gone", "gonedays1234", role=auth.ROLE_FAMILY,
                                 library="/nowhere/at/all"),
    }

    def set_(rel, **cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE assets SET {sets} WHERE id=?", (*cols.values(), ids[rel]))

    for rel in ("shared/holiday/beach0.jpg", "2023/05/12/shot0.jpg", "misc/plain.png"):
        set_(rel, visibility=0)
    set_("misc/plain.png", date_key="")
    set_("2023/06/11/shot1.jpg", visibility=2)                 # hidden
    set_("2023/08/10/shot3.jpg", nsfw=1)                       # flagged
    set_("2023/09/09/shot4.jpg", trashed=1)                    # in the bin
    set_("private/secret0.jpg", kind="audio", visibility=0)    # administrator-only kind
    conn.commit()
    db.set_private(conn, [ids["2023/07/11/shot2.jpg"]], people["family"].id)
    db.set_private(conn, [ids["shared/holiday/beach3.jpg"]], members["sam"].id)
    return conn, ids, members


def viewer_for(app, user, path="/api/assets"):
    with app.test_request_context(path):
        g.user = user
        return visibility.current()


def seen_by_one(conn, viewer):
    rows = [dict(r) for r in conn.execute("SELECT * FROM assets")]
    return {r["rel_path"] for r in rows if viewer.may_see(r)}


def seen_by_sql(conn, viewer, mine="also"):
    clause, params = viewer.sql("a", mine=mine)
    bin_ = "" if viewer.is_admin else " AND a.trashed = 0"
    return {r[0] for r in conn.execute(
        f"SELECT a.rel_path FROM assets a WHERE {clause}{bin_}", params)}


def test_each_caller_holds_the_limits_their_profile_gives(app, house):
    _, _, members = house
    admin = viewer_for(app, members["admin"])
    assert (admin.max_visibility, admin.admin_only_kinds, admin.nsfw_shield) == (2, (), False)
    family = viewer_for(app, members["family"])
    assert (family.max_visibility, family.admin_only_kinds, family.nsfw_shield) == (1, ("audio",), True)
    assert family.viewer_id == members["family"].id and family.scope is None
    assert viewer_for(app, members["guest"]).max_visibility == 0
    assert viewer_for(app, members["anon"]).viewer_id == 0
    assert viewer_for(app, members["sam"]).scope == "shared"
    assert viewer_for(app, members["kid"]).scope == "shared"
    assert viewer_for(app, members["gone"]).roots == []
    assert family.policy[1] == "all"                       # conftest: nobody limited


def test_what_each_caller_may_open(app, house):
    conn, _, members = house
    admin = seen_by_one(conn, viewer_for(app, members["admin"]))
    family = seen_by_one(conn, viewer_for(app, members["family"]))
    guest = seen_by_one(conn, viewer_for(app, members["guest"]))
    sam = seen_by_one(conn, viewer_for(app, members["sam"]))
    for rel in ("2023/06/11/shot1.jpg", "2023/08/10/shot3.jpg", "2023/09/09/shot4.jpg",
                "private/secret0.jpg", "2023/07/11/shot2.jpg", "shared/holiday/beach3.jpg"):
        assert rel in admin
    assert "2023/07/11/shot2.jpg" in family                 # her own "Only to me"
    for rel in ("2023/06/11/shot1.jpg", "2023/08/10/shot3.jpg", "2023/09/09/shot4.jpg",
                "private/secret0.jpg", "shared/holiday/beach3.jpg"):
        assert rel not in family
    assert guest == {"shared/holiday/beach0.jpg", "2023/05/12/shot0.jpg", "misc/plain.png"}
    assert guest == seen_by_one(conn, viewer_for(app, members["anon"]))
    assert sam == {"shared/holiday/beach0.jpg", "shared/holiday/beach1.jpg",
                   "shared/holiday/beach2.jpg", "shared/holiday/beach3.jpg"}
    assert seen_by_one(conn, viewer_for(app, members["kid"])) == {"shared/holiday/beach0.jpg"}
    assert seen_by_one(conn, viewer_for(app, members["gone"])) == set()


@pytest.mark.parametrize("restricted", [False, True])
def test_a_listing_and_one_item_agree_for_everybody(app, house, restricted):
    conn, _, members = house
    if restricted:
        db.set_meta(conn, date_policy.KEY, json.dumps(
            {"cutoff": "2023-07-01", "admin": "after", "family": "before", "guest": "after"}))
    for name, user in members.items():
        viewer = viewer_for(app, user)
        assert seen_by_sql(conn, viewer) == seen_by_one(conn, viewer), name


def test_the_date_limit_is_the_viewers(app, house):
    conn, _, members = house
    db.set_meta(conn, date_policy.KEY, json.dumps(
        {"cutoff": "2023-07-01", "admin": "after", "family": "before", "guest": "after"}))
    family = seen_by_one(conn, viewer_for(app, members["family"]))
    assert "2023/05/12/shot0.jpg" in family and "2023/10/09/shot5.jpg" not in family
    assert "misc/plain.png" not in family                    # undated: not for her
    admin = seen_by_one(conn, viewer_for(app, members["admin"]))
    assert "misc/plain.png" in admin                         # undated: still the admin's
    assert "2023/05/12/shot0.jpg" not in admin
    guest = seen_by_one(conn, viewer_for(app, members["guest"]))
    assert guest == {"shared/holiday/beach0.jpg"}


@pytest.mark.parametrize("mine", ["", "also", "only"])
def test_the_gate_lists_what_the_helpers_before_it_listed(app, house, mine):
    conn, ids, members = house
    for name, user in members.items():
        with app.test_request_context("/api/assets"):
            g.user = user
            viewer = visibility.current()
            asked = list(ids.values()) if mine == "also" else None
            before, _ = db.query_assets(conn, viewer.roots, **viewer.limits(), ids=asked,
                                        private_mine=mine == "only", limit=500)
            after, _ = db.query_assets(conn, gate=viewer, ids=asked,
                                       private_mine=mine == "only", limit=500)
        assert [r["id"] for r in after] == [r["id"] for r in before], (name, mine)


def test_only_mine_is_nothing_for_nobody(app, house):
    _, _, members = house
    assert viewer_for(app, members["anon"]).sql("a", mine="only")[0].endswith("0=1")
    with pytest.raises(ValueError):
        viewer_for(app, members["family"]).sql("a", mine="everyone")


def test_no_library_set_up_is_a_conflict(app, house):
    cfg = app.config["MV_CONFIG"]
    _, _, members = house
    cfg.roots, cfg.active_root = [], ""
    viewer = viewer_for(app, members["family"])
    with pytest.raises(Conflict):
        viewer.roots  # noqa: B018


def test_built_once_per_request_and_again_for_somebody_else(app, house):
    _, _, members = house
    with app.test_request_context("/api/assets"):
        g.user = members["family"]
        first = visibility.current()
        assert visibility.current() is first
        g.user = members["guest"]
        assert visibility.current() is not first
        assert visibility.current().max_visibility == 0
