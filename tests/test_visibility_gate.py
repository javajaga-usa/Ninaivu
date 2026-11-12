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


@pytest.mark.parametrize("restricted", [False, True])
def test_the_lists_count_through_the_gate_as_they_did_before(app, house, restricted):
    conn, _, members = house
    if restricted:
        db.set_meta(conn, date_policy.KEY, json.dumps(
            {"cutoff": "2023-07-01", "admin": "after", "family": "before", "guest": "after"}))
    for name, user in members.items():
        with app.test_request_context("/api/timeline"):
            g.user = user
            viewer = visibility.current()
            old = {"max_visibility": viewer.max_visibility, "scope": viewer.scope}
            assert db.facets(conn, gate=viewer, limit=200) == \
                db.facets(conn, viewer.roots, limit=200, **old), name
            assert db.timeline(conn, gate=viewer) == db.timeline(conn, viewer.roots, **old), name
            assert db.list_occasions(conn, gate=viewer) == db.list_occasions(
                conn, viewer.roots, viewer_id=viewer.viewer_id, **old), name
            for month, day in ((5, 12), (7, 11), (9, 13)):
                assert db.query_on_this_day(conn, gate=viewer, month=month, day=day) == \
                    db.query_on_this_day(conn, viewer.roots, month=month, day=day,
                                         **viewer.limits()), name


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


def _before_the_gate(asset, user, cfg):
    """What ``api._may_see`` answered before it asked the gate (3.x)."""
    libraries = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
    roots, sub = auth.resolve_library(user.assigned_library, libraries)
    scope = sub or (user.effective_scope or None)
    if not db.visible_to(asset, user.max_visibility, scope, viewer_id=user.id):
        return False
    if asset.get("root") not in roots:
        return False
    return not ((asset.get("trashed") or asset.get("nsfw")) and not user.is_admin)


@pytest.mark.parametrize("restricted", [False, True])
def test_the_routes_helpers_are_the_gates(app, house, restricted):
    """``_viewer``, ``_roots``, ``_may_see`` and ``_guard`` ask the Viewer, and
    answer as the helpers that kept their own copy of the rules did."""
    from werkzeug.exceptions import NotFound

    from ninaivu.api import api
    conn, _, members = house
    cfg = app.config["MV_CONFIG"]
    if restricted:
        db.set_meta(conn, date_policy.KEY, json.dumps(
            {"cutoff": "2023-07-01", "admin": "after", "family": "before", "guest": "after"}))
    rows = [dict(r) for r in conn.execute("SELECT * FROM assets")]
    for name, user in members.items():
        with app.test_request_context("/api/assets"):
            g.user = user
            viewer = visibility.current()
            libraries = cfg.libraries or [cfg.active_root]
            assert api._roots() == auth.resolve_library(user.assigned_library, libraries)[0]
            assert api._viewer() == viewer.limits() == {
                "viewer_id": user.id, "max_visibility": user.max_visibility,
                "scope": viewer.scope}
            assert api._viewer_limits() == {"max_visibility": user.max_visibility,
                                            "scope": viewer.scope}
            for row in rows:
                expected = _before_the_gate(row, user, cfg)
                assert api._may_see(row) is expected, (name, row["rel_path"])
                if expected:
                    assert api._guard(row) is row
                else:
                    with pytest.raises(NotFound):
                        api._guard(row)


# -- three answers decided on 10 Nov 2026 (Viewer's switches), pinned ------------

def test_occasions_count_a_pair_once(app, house, monkeypatch):
    """OCCASIONS_COUNT_BOTH_HALVES is off: a live photograph's clip is not
    counted in its occasion, as every other list counts the pair once."""
    conn, ids, members = house
    root = conn.execute("SELECT root FROM assets LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO occasions(id, root, key, title, started_at, ended_at, count) "
                 "VALUES (77, ?, 'beach', 'Beach', 1, 2, 3)", (root,))
    beach = [ids[f"shared/holiday/beach{i}.jpg"] for i in (0, 1, 2)]
    conn.execute("UPDATE assets SET occasion_id=77, visibility=0 WHERE id IN (?,?,?)", beach)
    conn.execute("UPDATE assets SET live_clip=1 WHERE id=?", (beach[1],))
    conn.commit()
    viewer = viewer_for(app, members["admin"])
    counted = {o["id"]: o["count"] for o in db.list_occasions(conn, gate=viewer)}
    assert counted[77] == 2
    # What it was before the decision.
    monkeypatch.setattr(visibility.Viewer, "OCCASIONS_COUNT_BOTH_HALVES", True)
    assert viewer.occasion_halves_sql("a") == ""
    counted = {o["id"]: o["count"] for o in db.list_occasions(conn, gate=viewer)}
    assert counted[77] == 3


def test_a_profile_whose_folder_is_gone_has_no_candidates(app, house, monkeypatch):
    """NO_FOLDER_RANKS_EVERY_VECTOR is off: with no folders, the picture
    search has no candidates to rank; one with folders gets its own."""
    conn, ids, members = house
    for rel in ("shared/holiday/beach0.jpg", "2023/06/11/shot1.jpg", "2023/07/11/shot2.jpg"):
        conn.execute("INSERT INTO embeddings(asset_id, model, dim, vector) VALUES (?, 't', 2, ?)",
                     (ids[rel], b"\0" * 8))
    conn.commit()
    every = sorted(r[0] for r in conn.execute("SELECT asset_id FROM embeddings"))
    gone = viewer_for(app, members["gone"])
    assert not gone.ranks_every_vector()
    assert db.visible_embedding_ids(conn, gate=gone) == []
    guest = viewer_for(app, members["guest"])
    assert db.visible_embedding_ids(conn, gate=guest) == [ids["shared/holiday/beach0.jpg"]]
    # An internal caller without a gate still means "no limit" by no roots.
    assert sorted(db.visible_embedding_ids(conn, gone.roots, gone.max_visibility,
                                           gone.scope)) == every
    # What it was before the decision.
    monkeypatch.setattr(visibility.Viewer, "NO_FOLDER_RANKS_EVERY_VECTOR", True)
    assert sorted(db.visible_embedding_ids(conn, gate=gone)) == every


def test_the_stack_route_hides_the_bin_from_the_administrator(app, house, monkeypatch):
    """STACK_HIDES_THE_BIN_FROM_ADMIN, kept: the stack route leaves a binned
    frame out for the administrator too, who may open it by id."""
    from ninaivu.features.stacks import routes as api_stacks
    conn, ids, members = house
    binned = ids["2023/09/09/shot4.jpg"]
    with app.test_request_context("/api/stacks/1"):
        g.user = members["admin"]
        viewer = visibility.current()
        assert viewer.may_see(dict(conn.execute(
            "SELECT * FROM assets WHERE id=?", (binned,)).fetchone()))
        assert not viewer.stack_shows_bin()
        assert api_stacks._visible(conn, [binned]) == []
        monkeypatch.setattr(visibility.Viewer, "STACK_HIDES_THE_BIN_FROM_ADMIN", False)
        assert [r["id"] for r in api_stacks._visible(conn, [binned])] == [binned]
        g.user = members["family"]
        assert api_stacks._visible(conn, [binned]) == []


# -- the second slice: each domain's lists through the gate, as before ------------

@pytest.fixture()
def furnished(house):
    """The house with albums, named faces and places in every state."""
    from ninaivu.storage import db_albums, db_faces
    conn, ids, members = house
    admin, maya = members["admin"].id, members["family"].id
    for name, by, rels in (
            ("Holiday", admin, ["shared/holiday/beach0.jpg", "shared/holiday/beach3.jpg",
                                "2023/06/11/shot1.jpg", "2023/08/10/shot3.jpg",
                                "2023/09/09/shot4.jpg", "2023/05/12/shot0.jpg"]),
            ("Mayas", maya, ["2023/07/11/shot2.jpg", "misc/plain.png"]),
            ("Old", None, ["private/secret0.jpg", "shared/holiday/beach1.jpg"]),
            ("Empty", maya, [])):
        album = db_albums.create_album(conn, name, created_by=by)
        db_albums.album_add(conn, album, [ids[r] for r in rels])
    people = {n: int(db_faces.create_person_cluster(conn, n)["id"]) for n in ("Amma", "Appa")}
    for rel, who in (("shared/holiday/beach0.jpg", "Amma"), ("2023/06/11/shot1.jpg", "Amma"),
                     ("2023/07/11/shot2.jpg", "Appa"), ("shared/holiday/beach3.jpg", "Appa"),
                     ("2023/10/09/shot5.jpg", "Appa")):
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, thumb, "
                     "quality) VALUES (?, ?, 'confirmed', '[1,1,9,9]', x'00', 't.jpg', 0.5)",
                     (ids[rel], people[who]))
    for rel in ("shared/holiday/beach0.jpg", "2023/06/11/shot1.jpg", "2023/07/11/shot2.jpg",
                "2023/05/12/shot0.jpg", "shared/holiday/beach3.jpg", "misc/plain.png"):
        conn.execute("UPDATE assets SET gps_lat=13.0, gps_lon=80.2, country='India', "
                     "city='Chennai' WHERE id=?", (ids[rel],))
    conn.commit()
    return conn, ids, members


DOMAIN_POLICIES = [None, {"cutoff": "2023-07-01", "admin": "after", "family": "before",
                          "guest": "after"}]


@pytest.mark.parametrize("policy", DOMAIN_POLICIES)
def test_albums_and_people_count_through_the_gate_as_before(app, furnished, policy):
    conn, _, members = furnished
    if policy:
        db.set_meta(conn, date_policy.KEY, json.dumps(policy))
    for name, user in members.items():
        with app.test_request_context("/api/albums"):
            g.user = user
            viewer = visibility.current()
            old = {"max_visibility": viewer.max_visibility, "scope": viewer.scope}
            assert db.list_albums(conn, gate=viewer, viewer_id=user.id,
                                  is_admin=viewer.is_admin) == db.list_albums(
                conn, viewer.roots, viewer_id=user.id, is_admin=viewer.is_admin, **old), name
            assert db.list_people(conn, gate=viewer) == db.list_people(
                conn, viewer.roots, **old), name
            assert db.list_unnamed_clusters(conn, gate=viewer, min_size=1) == \
                db.list_unnamed_clusters(conn, viewer.roots, min_size=1, **old), name


@pytest.mark.parametrize("policy", DOMAIN_POLICIES)
def test_places_count_through_the_gate_as_before(app, furnished, policy):
    from ninaivu.storage import geo
    conn, _, members = furnished
    if policy:
        db.set_meta(conn, date_policy.KEY, json.dumps(policy))
    world = (-90.0, 90.0, -180.0, 180.0)
    for name, user in members.items():
        with app.test_request_context("/api/geo/places"):
            g.user = user
            viewer = visibility.current()
            old = viewer.limits()
            for extra in ({}, {"year": 2023}, {"person": 1}):
                assert geo.places(conn, None, gate=viewer, **extra) == \
                    geo.places(conn, viewer.roots, **old, **extra), name
                assert geo.clusters(conn, None, zoom=3, gate=viewer, **extra) == \
                    geo.clusters(conn, viewer.roots, zoom=3, **old, **extra), name
                assert geo.photo_ids(conn, None, bounds=world, gate=viewer, **extra) == \
                    geo.photo_ids(conn, viewer.roots, bounds=world, **old, **extra), name
            assert geo.trail(conn, None, year=2023, gate=viewer) == \
                geo.trail(conn, viewer.roots, year=2023, **old), name
            assert db.query_geo_points(conn, gate=viewer) == \
                db.query_geo_points(conn, viewer.roots, **old), name


@pytest.mark.parametrize("policy", DOMAIN_POLICIES)
def test_the_books_years_count_through_the_gate_as_before(app, furnished, policy):
    conn, _, members = furnished
    if policy:
        db.set_meta(conn, date_policy.KEY, json.dumps(policy))
    for name, user in members.items():
        with app.test_request_context("/api/books/ideas"):
            g.user = user
            viewer = visibility.current()
            assert db.picture_years(conn, gate=viewer) == db.picture_years(
                conn, viewer.roots, max_visibility=viewer.max_visibility,
                scope=viewer.scope), name


@pytest.mark.parametrize("policy", DOMAIN_POLICIES)
def test_the_sidebar_counts_through_the_gate_as_before(app, furnished, policy):
    conn, _, members = furnished
    if policy:
        db.set_meta(conn, date_policy.KEY, json.dumps(policy))
    keys = {}
    for name, user in members.items():
        with app.test_request_context("/api/status/stats"):
            g.user = user
            viewer = visibility.current()
            assert db.library_stats(conn, gate=viewer) == db.library_stats(
                conn, viewer.roots, **viewer.limits()), name
            keys[name] = viewer.cache_key()
    # A remembered answer is never handed to somebody with other limits.
    assert len(set(keys.values())) == len(keys)


def test_the_analysed_count_is_of_what_the_viewer_may_see(app, house):
    """Counted by folder alone until 4.0, so a member learnt how many of the
    hidden, flagged and other people's private items had been analysed."""
    conn, ids, members = house
    analysed = ("2023/05/12/shot0.jpg",          # seen by everybody signed in
                "2023/06/11/shot1.jpg",          # hidden
                "2023/08/10/shot3.jpg",          # flagged
                "2023/07/11/shot2.jpg",          # Maya's own "Only to me"
                "2023/09/09/shot4.jpg")          # in the bin
    conn.executemany("INSERT INTO embeddings(asset_id, model, dim, vector) "
                     "VALUES (?, 'm', 1, x'00')", [(ids[rel],) for rel in analysed])
    conn.commit()
    counted = {}
    for name, user in members.items():
        with app.test_request_context("/api/status/stats"):
            g.user = user
            viewer = visibility.current()
            clause, params = viewer.sql("a")
            seen = {r[0] for r in conn.execute(
                f"SELECT a.rel_path FROM assets a WHERE {clause} AND a.trashed = 0 "
                f"AND (a.nsfw = 0 OR ? >= 2)", (*params, viewer.max_visibility))}
            counted[name] = db.library_stats(conn, gate=viewer)["embedded"]
            assert counted[name] == len(seen & set(analysed)), name
    # As the sidebar's other numbers: "Only to me" items are counted apart, in
    # "private", and the bin not at all.
    assert counted["family"] == 1
    assert counted["admin"] == 4        # everything outside the bin
    assert counted["anon"] == 1         # shot0 is public


def test_every_open_read_of_photographs_is_the_gates():
    """The manifest says so for every open route that reads the library."""
    from ninaivu.server import routes
    for endpoint in ("api.status_stats", "api.frame_next", "api.festivals_list",
                     "api.festivals_one", "api.moments_search", "api.our_year_list",
                     "api.our_year_one", "api.then_and_now_all", "api.then_and_now_today"):
        assert routes.OPEN[endpoint] == ("guest", routes.GATE), endpoint
    assert not [e for e, (_, why) in routes.OPEN.items()
                if why == "the route applies the caller's visibility itself"]


@pytest.mark.parametrize("policy", DOMAIN_POLICIES)
def test_young_faces_reads_the_photographs_the_gate_lets_through(app, furnished, policy):
    """The young-faces guard (api_young_faces._guard_sql) is the gate's listing:
    every picture the caller may open (explicit ones for the administrator
    only), not in the bin, not a screenshot."""
    from ninaivu.features.young_faces import routes as api_young_faces
    conn, _, members = furnished
    if policy:
        db.set_meta(conn, date_policy.KEY, json.dumps(policy))
    rows = [dict(r) for r in conn.execute("SELECT * FROM assets")]
    for name, user in members.items():
        with app.test_request_context("/api/young-faces"):
            g.user = user
            viewer = visibility.current()
            guard, params = api_young_faces._guard_sql()
            listed = {r[0] for r in conn.execute(
                f"SELECT a.id FROM assets a WHERE {guard}", params)}
            expected = {r["id"] for r in rows if r["kind"] == "picture" and not r["trashed"]
                        and viewer.may_see(r, own=False)}
            assert listed == expected, name
