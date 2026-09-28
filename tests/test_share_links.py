"""Share links, end to end — the feature the audit found was entirely dead.

Two separate defects were behind that, and both are covered here: the URL the
viewer hands out had no route at all, and the photograph behind it was refused
to the very person the link was minted for.
"""

import pytest


def _share(client, asset_id, **extra):
    body = {"scope": "asset", "target_id": asset_id, **extra}
    response = client.post("/api/shares", json=body)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _first_asset(client):
    return client.get("/api/assets?limit=1").get_json()["items"][0]["id"]


@pytest.mark.parametrize("changes", [
    {"scope": "other"}, {"scope": []},
    {"target_id": "oops"}, {"target_id": []}, {"target_id": True},
    {"target_id": 1.5}, {"target_id": -1}, {"target_id": 2**63},
    {"expires_in_days": "NaN"}, {"expires_in_days": "Infinity"},
    {"expires_in_days": -1}, {"expires_in_days": []},
    {"expires_in_days": True}, {"password": ["secret"]},
])
def test_invalid_share_settings_are_rejected(as_family, changes):
    body = {"scope": "asset", "target_id": _first_asset(as_family), **changes}
    response = as_family.post("/api/shares", json=body)
    assert response.status_code == 400
    assert as_family.get("/api/shares").get_json()["shares"] == []


def test_share_settings_require_an_object(as_family):
    assert as_family.post("/api/shares", json=[1]).status_code == 400


# ---------------------------------------------------------------------------
# The link opens
# ---------------------------------------------------------------------------

def test_the_url_the_viewer_hands_out_actually_opens(app, people, as_family):
    """`/share/<token>` was a 404 — the link was dead the moment it was copied."""
    share = _share(as_family, _first_asset(as_family))
    assert share["share_url"] == f"/share/{share['token']}"

    page = app.test_client().get(share["share_url"])
    assert page.status_code == 200
    assert b"<html" in page.data.lower()
    assert b"<script>" not in page.data
    assert b'/static/js/share.js?v=' in page.data
    assert "script-src 'self'" in page.headers['Content-Security-Policy']


def test_shared_preview_converts_and_preserves_token_access(app, as_family, scanned):
    from pathlib import Path
    from PIL import Image
    _, conn, _ = scanned
    asset_id = _first_asset(as_family)
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    path = Path(row['root']) / row['rel_path']
    Image.new('RGB', (80, 60), 'orange').save(path, format='TIFF')
    conn.execute("UPDATE assets SET ext='.tiff' WHERE id=?", (asset_id,))
    conn.commit()
    share = _share(as_family, asset_id, password='private-link')
    anon = app.test_client()
    url = f"/api/share/{share['token']}/preview/{asset_id}"
    assert anon.get(url).status_code == 401
    assert anon.post(f"/api/share/{share['token']}/unlock", json={'password': 'private-link'}).status_code == 200
    item = anon.get(f"/api/share/{share['token']}").get_json()['item']
    assert item['view'] == url
    response = anon.get(url)
    assert response.status_code == 200
    assert response.mimetype == 'image/jpeg'
    assert anon.get(f"/api/share/{share['token']}/preview/999999").status_code == 404
    as_family.delete(f"/api/shares/{share['token']}")
    assert anon.get(url).status_code == 404


def test_a_stranger_can_see_the_photograph_itself(app, people, as_family):
    """Serving the metadata and then 404ing every image is not a share."""
    share = _share(as_family, _first_asset(as_family))
    anon = app.test_client()

    body = anon.get(f"/api/share/{share['token']}").get_json()
    item = body["item"]
    assert item["src"].startswith(f"/api/share/{share['token']}/file/")
    assert item["thumb"].startswith(f"/api/share/{share['token']}/thumb/")

    assert anon.get(item["src"]).status_code == 200
    assert anon.get(item["thumb"]).status_code == 200


def test_a_shared_album_serves_every_photograph_in_it(app, people, as_family):
    made = as_family.post("/api/albums", json={"name": "Holiday"}).get_json()
    album_id = made.get("id") or made.get("album", {}).get("id")
    ids = [i["id"] for i in
           as_family.get("/api/assets?limit=3").get_json()["items"]]
    as_family.post(f"/api/albums/{album_id}/items", json={"ids": ids})

    share = as_family.post("/api/shares",
                           json={"scope": "album", "target_id": album_id}).get_json()
    anon = app.test_client()
    body = anon.get(f"/api/share/{share['token']}").get_json()
    assert body["scope"] == "album"
    assert body["items"], "a shared album with photographs in it must list them"
    for item in body["items"]:
        assert anon.get(item["thumb"]).status_code == 200


def test_a_link_reaches_only_what_it_was_minted_for(app, people, as_family):
    """The token is the authority, so it must not be a skeleton key."""
    ids = [i["id"] for i in
           as_family.get("/api/assets?limit=3").get_json()["items"]]
    share = _share(as_family, ids[0])
    anon = app.test_client()
    assert anon.get(f"/api/share/{share['token']}/file/{ids[0]}").status_code == 200
    for other in ids[1:]:
        assert anon.get(
            f"/api/share/{share['token']}/file/{other}").status_code == 404
        assert anon.get(
            f"/api/share/{share['token']}/thumb/{other}").status_code == 404


def test_an_unknown_token_is_a_404_not_a_page(app, people):
    client = app.test_client()
    assert client.get("/share/not-a-real-token").status_code == 404
    assert client.get("/api/share/not-a-real-token").status_code == 404


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def test_share_responses_do_not_expose_password_hash(as_family, scanned):
    from ninaivu.storage import db

    share = _share(as_family, _first_asset(as_family), password="open-sesame")
    assert "password" not in share["share"]
    _, conn, _ = scanned
    assert db.get_share(conn, share["token"])["password"]
    assert all("password" not in item for item in
               as_family.get("/api/shares").get_json()["shares"])


@pytest.mark.parametrize("payload", [None, [], [1], "secret", 42,
                                     {"password": []}, {"password": 123},
                                     {"password": None}])
def test_unlock_rejects_malformed_credentials(app, as_family, payload):
    share = _share(as_family, _first_asset(as_family), password="open-sesame")
    anon = app.test_client()
    url = f"/api/share/{share['token']}"
    assert anon.post(url + "/unlock", json=payload).status_code == 400
    assert anon.get(url).status_code == 401


def test_non_ascii_unlock_cookie_does_not_crash(app, as_family):
    from ninaivu.api.api_share import _share_unlock_cookie

    share = _share(as_family, _first_asset(as_family), password="open-sesame")
    anon = app.test_client()
    anon.set_cookie(_share_unlock_cookie(share["token"]), "caf\u00e9")
    assert anon.get(f"/api/share/{share['token']}").status_code == 401
    assert anon.post(f"/api/share/{share['token']}/unlock",
                     json={"password": "open-sesame"}).status_code == 200
    assert anon.get(f"/api/share/{share['token']}").status_code == 200


def test_a_password_protected_link_asks_before_it_shows(app, people, as_family):
    share = _share(as_family, _first_asset(as_family), password="open-sesame")
    anon = app.test_client()
    response = anon.get(f"/api/share/{share['token']}")
    assert response.status_code == 401
    assert response.get_json()["password_required"] is True


def test_answering_the_password_opens_it_and_keeps_it_open(app, people, as_family):
    asset_id = _first_asset(as_family)
    share = _share(as_family, asset_id, password="open-sesame")
    anon = app.test_client()

    assert anon.post(f"/api/share/{share['token']}/unlock",
                     json={"password": "wrong"}).status_code == 401
    assert anon.post(f"/api/share/{share['token']}/unlock",
                     json={"password": "open-sesame"}).status_code == 200
    # the cookie carries it from here, including to the image itself
    assert anon.get(f"/api/share/{share['token']}").status_code == 200
    assert anon.get(
        f"/api/share/{share['token']}/file/{asset_id}").status_code == 200


def test_the_password_is_not_read_from_the_query_string(app, people, as_family):
    """A password in a URL is in the proxy log, the history and the Referer."""
    share = _share(as_family, _first_asset(as_family), password="open-sesame")
    anon = app.test_client()
    response = anon.get(f"/api/share/{share['token']}?pwd=open-sesame")
    assert response.status_code == 401


def test_guessing_a_share_password_is_rate_limited(app, people, as_family):
    """Sixty wrong answers used to be sixty 401s and no complaint."""
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS.clear()
    share = _share(as_family, _first_asset(as_family), password="open-sesame")
    anon = app.test_client()
    codes = {anon.post(f"/api/share/{share['token']}/unlock",
                       json={"password": f"wrong-{i}"}).status_code
             for i in range(30)}
    assert 429 in codes, "a share password must not be guessable without limit"


def test_the_right_password_still_works_for_somebody_else_after_a_lockout(
        app, people, as_family):
    """The limit is per caller and per link, not a global door-bar."""
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS.clear()
    other = _share(as_family, _first_asset(as_family), password="hunter-two")
    client = app.test_client()
    assert client.post(f"/api/share/{other['token']}/unlock",
                       json={"password": "hunter-two"}).status_code == 200


# ---------------------------------------------------------------------------
# Withdrawal
# ---------------------------------------------------------------------------

def test_deleting_a_share_closes_the_door_immediately(app, people, as_family):
    asset_id = _first_asset(as_family)
    share = _share(as_family, asset_id)
    anon = app.test_client()
    assert anon.get(f"/api/share/{share['token']}").status_code == 200

    as_family.delete(f"/api/shares/{share['token']}")
    assert anon.get(f"/api/share/{share['token']}").status_code == 404
    assert anon.get(f"/share/{share['token']}").status_code == 404
    assert anon.get(
        f"/api/share/{share['token']}/file/{asset_id}").status_code == 404


def test_an_expired_link_says_so(app, people, as_family, scanned):
    import time

    _, conn, _ = scanned
    share = _share(as_family, _first_asset(as_family))
    conn.execute("UPDATE shares SET expires_at=? WHERE token=?",
                 (time.time() - 60, share["token"]))
    conn.commit()
    assert app.test_client().get(
        f"/api/share/{share['token']}").status_code == 410


def test_a_trashed_photograph_is_no_longer_shared(app, people, as_family, scanned):
    _, conn, _ = scanned
    asset_id = _first_asset(as_family)
    share = _share(as_family, asset_id)
    conn.execute("UPDATE assets SET trashed=1 WHERE id=?", (asset_id,))
    conn.commit()
    anon = app.test_client()
    assert anon.get(f"/api/share/{share['token']}").status_code == 404
    assert anon.get(
        f"/api/share/{share['token']}/file/{asset_id}").status_code == 404


#: What the family app knows about a photograph and a stranger holding a link
#: has no business reading: where it sits in the household's folders, who may
#: see it and why, and anything that says where it was taken.
PRIVATE_FIELDS = ("folder", "visibility", "visibility_source", "tags", "caption",
                  "city", "country", "gps", "camera", "lens", "favorite", "rating",
                  "quality", "download", "live_src", "rotation_source")


@pytest.mark.parametrize("opener", ["stranger", "family"])
def test_a_link_tells_its_holder_only_what_the_page_shows(app, people, as_family,
                                                           scanned, opener):
    """Whoever opens it — nobody, or a family member signed in on the same
    browser — the answer is the same thin one."""
    _, conn, _ = scanned
    asset_id = _first_asset(as_family)
    conn.execute("UPDATE assets SET folder='Medical/2023', city='Chennai', "
                 "country='India', caption='at the clinic' WHERE id=?", (asset_id,))
    conn.commit()
    share = _share(as_family, asset_id)
    holder = as_family if opener == "family" else app.test_client()

    item = holder.get(f"/api/share/{share['token']}").get_json()["item"]
    leaked = [key for key in PRIVATE_FIELDS if key in item]
    assert not leaked, f"a share link told its holder {leaked}"
    assert "Medical" not in str(item) and "Chennai" not in str(item)
    assert item["id"] == asset_id and item["name"] and item["kind"]


def test_a_shared_album_names_itself_and_nothing_else(app, people, as_family):
    made = as_family.post("/api/albums", json={"name": "Holiday"}).get_json()
    ids = [i["id"] for i in as_family.get("/api/assets?limit=2").get_json()["items"]]
    as_family.post(f"/api/albums/{made['id']}/items", json={"ids": ids})
    share = as_family.post("/api/shares",
                           json={"scope": "album", "target_id": made["id"]}).get_json()

    body = app.test_client().get(f"/api/share/{share['token']}").get_json()
    assert body["album"] == {"name": "Holiday"}
    for item in body["items"]:
        assert not [key for key in PRIVATE_FIELDS if key in item]
