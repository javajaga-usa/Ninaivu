"""Turning a photograph the right way up, and having it stay that way.

Any automatic guess needs a correction path, or it is worse than no guess at
all. The viewer has had a rotate button for a long time and it has never been
worth much: it was a CSS transform that reset the moment the photograph was
closed, so a scanned print that came out sideways came out sideways again
tomorrow, and the day after.

This is the version that sticks. Household members may correct a shared photo;
guests retain a temporary viewer-only turn and cannot rewrite what the
household sees.
"""

import pytest
from PIL import Image

from conftest import ADMIN, ids_of, login


def oblong(client):
    """An asset that is not square, so a quarter turn is visible in its shape.

    The fixture library holds a square PNG and it sorts to the front, which
    makes every shape assertion below vacuous if the first id is taken blindly.
    """
    items = client.get("/api/assets?limit=200").get_json()["items"]
    for item in items:
        if item["kind"] == "picture" and item["width"] != item["height"]:
            return item["id"]
    raise AssertionError("the fixture library has no oblong photograph")


def rotation_of(client, asset_id):
    body = client.get(f"/api/asset/{asset_id}").get_json()
    return body["rotation"], body["rotation_source"]


# --- who may ---------------------------------------------------------------

def test_a_family_member_can_save_a_rotation(as_family):
    target = ids_of(as_family)[0]
    response = as_family.post(f"/api/asset/{target}/rotate",
                              json={"rotation": 90})
    assert response.status_code == 200, response.get_json()
    assert rotation_of(as_family, target) == (90, "manual")


def test_a_guest_cannot_save_a_rotation(as_admin, as_guest):
    target = ids_of(as_admin)[0]
    response = as_guest.post(f"/api/asset/{target}/rotate",
                             json={"rotation": 90})
    assert response.status_code in (401, 403), response.status_code


def test_a_family_member_cannot_rotate_hidden_media(as_admin, as_family):
    target = ids_of(as_admin)[0]
    hidden = as_admin.post("/api/visibility",
                           json={"ids": [target], "visibility": "hidden"})
    assert hidden.status_code == 200, hidden.get_json()
    response = as_family.post(f"/api/asset/{target}/rotate",
                              json={"rotation": 90})
    assert response.status_code == 404


def test_an_anonymous_visitor_cannot_rotate(anon):
    assert anon.post("/api/asset/1/rotate",
                     json={"rotation": 90}).status_code in (401, 403)


# --- what counts as a rotation ---------------------------------------------

@pytest.mark.parametrize("bad", [45, 1, -30, "sideways", None, 3600.5])
def test_only_the_four_quarter_turns_are_accepted(as_admin, bad):
    target = ids_of(as_admin)[0]
    response = as_admin.post(f"/api/asset/{target}/rotate",
                             json={"rotation": bad})
    assert response.status_code == 400, (bad, response.status_code)


@pytest.mark.parametrize("value,expected", [
    (0, 0), (90, 90), (180, 180), (270, 270), (360, 0), (450, 90), (-90, 270),
])
def test_a_turn_is_normalised_to_the_compass(as_admin, value, expected):
    target = ids_of(as_admin)[0]
    response = as_admin.post(f"/api/asset/{target}/rotate",
                             json={"rotation": value})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["rotation"] == expected


# --- it sticks -------------------------------------------------------------

def test_a_rotation_is_remembered(as_admin):
    target = ids_of(as_admin)[0]
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 90})
    assert rotation_of(as_admin, target) == (90, "manual")


def test_the_last_rotation_survives_a_service_restart(as_admin, scanned):
    """Persistence means a new process sees the turn, not just this client.

    Closing every thread-local SQLite connection catches an update that only
    appeared to work because uncommitted state remained visible on the request
    connection. Rebuilding the shared services also exercises the same schema
    initialisation path a real Ninaivu restart takes.
    """
    from ninaivu import build_services, create_home_app
    from ninaivu.storage import db

    cfg, _, _ = scanned
    target = ids_of(as_admin)[0]
    response = as_admin.post(
        f"/api/asset/{target}/rotate", json={"rotation": 270})
    assert response.status_code == 200, response.get_json()

    db.close_all()
    restarted = build_services(cfg)
    restarted.scanner.stop()
    fresh_client = login(create_home_app(restarted).test_client(), *ADMIN)

    assert rotation_of(fresh_client, target) == (270, "manual")


def test_the_gallery_carries_it_too(as_admin):
    target = ids_of(as_admin)[0]
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 180})
    listing = as_admin.get("/api/assets?limit=200").get_json()["items"]
    row = next(i for i in listing if i["id"] == target)
    assert row["rotation"] == 180
    assert row["rotation_source"] == "manual"


def test_rotating_swaps_the_recorded_shape(as_admin):
    """The grid lays a cell out from these numbers, so they have to follow the
    turn or every rotated photograph gets a cell of the wrong shape."""
    target = oblong(as_admin)
    before = as_admin.get(f"/api/asset/{target}").get_json()
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 90})
    after = as_admin.get(f"/api/asset/{target}").get_json()
    assert (after["width"], after["height"]) == (before["height"], before["width"])


def test_a_half_turn_leaves_the_shape_alone(as_admin):
    target = oblong(as_admin)
    before = as_admin.get(f"/api/asset/{target}").get_json()
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 180})
    after = as_admin.get(f"/api/asset/{target}").get_json()
    assert (after["width"], after["height"]) == (before["width"], before["height"])


def test_turning_it_back_to_zero_is_allowed(as_admin):
    target = ids_of(as_admin)[0]
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 90})
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 0})
    assert rotation_of(as_admin, target)[0] == 0


# --- the thumbnails follow -------------------------------------------------

def test_the_thumbnail_is_rewritten_the_new_way_up(as_admin, scanned):
    """The gallery shows thumbnails, so a rotation nobody baked in is a
    rotation nobody can see."""
    cfg, _, _ = scanned
    target = oblong(as_admin)
    detail = as_admin.get(f"/api/asset/{target}").get_json()
    if not detail["has_thumb"]:
        pytest.skip("this asset has no thumbnail to check")

    def thumb_size():
        hits = sorted(cfg.thumbs_dir.rglob("*_256.webp"),
                      key=lambda p: p.stat().st_mtime)
        return Image.open(hits[-1]).size

    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 90})
    turned = thumb_size()
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 0})
    straight = thumb_size()
    assert turned[0] < turned[1] or straight[0] > straight[1], (turned, straight)
    assert turned == (straight[1], straight[0]), (turned, straight)


# --- and a rescan does not undo it ----------------------------------------

def test_a_rescan_keeps_a_rotation_set_by_hand(as_admin, scanned):
    """The whole point of "manual". A guess should be replaced by a better
    guess next time; a decision a person made should not be."""
    from ninaivu.storage import db

    cfg, conn, scanner = scanned
    target = ids_of(as_admin)[0]
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 270})

    scanner._run(cfg.active_root and __import__("pathlib").Path(cfg.active_root),
                 full=True)
    row = db.get_asset(conn, target)
    assert row is not None, "the rescan lost the row entirely"
    assert row["rotation"] == 270
    assert row["rot_source"] == "manual"


# --- the thumbnail's version has to move every single time ----------------

def test_two_turns_in_the_same_second_are_two_versions(as_admin):
    """The bug this exists for.

    Thumbnails are cached `immutable` for a year, so the URL has to change when
    the file does. A version built only from a whole-second timestamp does not
    change between two rotations a moment apart — and turning ninety degrees
    and then back is exactly how somebody uses the button. The second turn
    would have been shown with the first turn's picture.
    """
    target = oblong(as_admin)
    first = as_admin.post(f"/api/asset/{target}/rotate",
                          json={"rotation": 90}).get_json()["thumb_v"]
    second = as_admin.post(f"/api/asset/{target}/rotate",
                           json={"rotation": 0}).get_json()["thumb_v"]
    assert first != second, (first, second)


def test_the_version_the_gallery_sees_matches_the_one_the_turn_returned(as_admin):
    target = oblong(as_admin)
    returned = as_admin.post(f"/api/asset/{target}/rotate",
                             json={"rotation": 180}).get_json()["thumb_v"]
    listed = as_admin.get(f"/api/asset/{target}").get_json()["thumb_v"]
    assert listed == returned, (listed, returned)


def test_the_same_picture_keeps_the_same_version(as_admin):
    """Turning back to where it started is the same photograph again, so the
    browser is right to reuse what it already has."""
    target = oblong(as_admin)
    start = as_admin.get(f"/api/asset/{target}").get_json()["thumb_v"]
    as_admin.post(f"/api/asset/{target}/rotate", json={"rotation": 90})
    back = as_admin.post(f"/api/asset/{target}/rotate",
                         json={"rotation": 0}).get_json()["thumb_v"]
    # The token is "<indexed_at>r<turn>t<recipe>" — the recipe names the
    # thumbnail settings, so read the turn out rather than pinning the shape.
    turn_of = lambda token: token.split("r", 1)[1].split("t", 1)[0]   # noqa: E731
    assert turn_of(back) == "0" and turn_of(start) == "0", (start, back)


# --- quick clicks ----------------------------------------------------------

def test_turns_of_one_photograph_do_not_overlap(app, as_admin, monkeypatch):
    """Three clicks in a second sent three turns at once. They rewrote the
    same thumbnails through the same temporary names, and on Windows two of
    them renaming one file is a sharing violation: a 500, and an angle decided
    by whichever thread finished last."""
    import threading
    import time

    from ninaivu.media import media

    target = oblong(as_admin)
    real = media.write_thumbnails
    inside, overlaps = [0], []
    count = threading.Lock()

    def slow_write(*args, **kwargs):
        with count:
            inside[0] += 1
            if inside[0] > 1:
                overlaps.append(inside[0])
        try:
            time.sleep(0.05)
            return real(*args, **kwargs)
        finally:
            with count:
                inside[0] -= 1

    monkeypatch.setattr(media, "write_thumbnails", slow_write)
    clients = [login(app.test_client(), *ADMIN) for _ in range(3)]
    statuses = []

    def turn(client, rotation):
        statuses.append(client.post(f"/api/asset/{target}/rotate",
                                    json={"rotation": rotation}).status_code)

    threads = [threading.Thread(target=turn, args=(c, r))
               for c, r in zip(clients, (90, 180, 270))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [200, 200, 200], statuses
    assert not overlaps, "two turns rewrote the same thumbnails at once"
