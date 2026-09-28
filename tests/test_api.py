"""API contract and, especially, the security properties we care about.

These exercise the API as an administrator; :mod:`test_roles` covers what the
other two roles are allowed to see and do.
"""

import json

import pytest

from conftest import login


@pytest.fixture()
def client(app, scanned):
    """Signed in as the library's administrator."""
    from ninaivu.server import auth

    _, conn, _ = scanned
    auth.bootstrap_admin(conn, "dad", "correcthorse1", "Dad")
    return login(app.test_client(), "dad", "correcthorse1")


def test_status_reports_library(client):
    data = client.get("/api/status").get_json()
    assert data["root"]
    assert data["stats"]["count"] == 15
    assert "engine" in data["ai"]


@pytest.mark.parametrize("endpoint", ["/api/assets", "/api/segments"])
def test_live_photos_filter_only_returns_live_items(client, scanned, endpoint):
    _, conn, _ = scanned
    asset_id = conn.execute("SELECT id FROM assets ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE assets SET is_live = 1 WHERE id = ?", (asset_id,))
    conn.commit()
    filtered = client.get(endpoint + "?is_live=1").get_json()
    assert filtered["total"] == 1
    assert client.get(endpoint + "?is_live=0").get_json()["total"] == 15


def test_live_src_is_only_offered_when_theres_a_companion_clip(client, scanned):
    """`is_live` alone doesn't promise a companion clip to play.

    A Google Motion Photo mixes its video *inside* the JPEG/HEIC itself; the
    scanner can tell one is there but doesn't extract it, so it can be
    ``is_live`` with no ``live_video_path`` (see the comment in
    ``ninaivu/media/scanner.py``). Serving ``live_src`` regardless of that
    used to point the viewer at ``/api/live-video/<id>``, which always 404s
    with no companion file -- a LIVE badge that failed silently on tap.
    """
    _, conn, _ = scanned
    asset_id = conn.execute("SELECT id FROM assets ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE assets SET is_live = 1 WHERE id = ?", (asset_id,))
    conn.commit()

    def get_item():
        items = client.get("/api/assets?limit=200").get_json()["items"]
        return next(i for i in items if i["id"] == asset_id)

    item = get_item()
    assert item["is_live"] is True
    assert item["live_src"] is None

    conn.execute("UPDATE assets SET live_video_path = ? WHERE id = ?",
                 ("2023/05/12/shot0.mov", asset_id))
    conn.commit()
    assert get_item()["live_src"] == f"/api/live-video/{asset_id}"


def test_readiness_reports_core_dependencies_without_leaking_paths(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["checks"] == {
        "database": "ok", "library": "ok",
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert "root" not in response.get_data(as_text=True).lower()


def test_readiness_allows_initial_setup(client, cfg):
    cfg.roots = []
    cfg.active_root = None

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json()["checks"]["library"] == "not-configured"


def test_readiness_supports_legacy_active_root_only(client, cfg):
    cfg.roots = []

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.get_json()["checks"]["library"] == "ok"


def test_readiness_fails_when_configured_library_is_unavailable(
        client, cfg, tmp_path):
    missing = tmp_path / "unplugged-drive"
    cfg.roots = [str(missing)]
    cfg.active_root = str(missing)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json()["ok"] is False
    assert response.get_json()["checks"] == {
        "database": "ok", "library": "unavailable",
    }
    assert str(missing) not in response.get_data(as_text=True)


def test_readiness_fails_closed_when_database_check_errors(client, monkeypatch):
    from ninaivu.api import api

    def unavailable():
        raise OSError("private database location")

    monkeypatch.setattr(api, "_conn", unavailable)
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.get_json()["checks"]["database"] == "unavailable"
    assert "private database location" not in response.get_data(as_text=True)


def test_segments_payload_is_compact_and_complete(client):
    data = client.get("/api/segments").get_json()
    assert data["total"] == 15
    items = [item for segment in data["segments"] for item in segment["items"]]
    assert len(items) == 15
    for asset_id, aspect, kind, flags, duration, thumb_v, swatch in items:
        assert isinstance(asset_id, int)
        assert 30 <= aspect <= 400          # aspect ratio × 100
        assert kind in (0, 1, 2)
        assert flags & 2                    # every fixture item has a thumb
        assert duration == 0
        # The thumbnail's version. Thumbnails are cached for a year and marked
        # immutable, so the URL has to change when one is rewritten — without a
        # version here a rotated photograph kept its old thumbnail on screen.
        # "<indexed_at>r<rotation>": both, because the content is the file that
        # was read *and* the turn applied to it.
        assert isinstance(thumb_v, str) and "r" in thumb_v, thumb_v
        # The tile's colour until its picture arrives, as three hex digits:
        # a fast scroll shows the right colours rather than a wall of grey.
        assert len(swatch) == 3 and int(swatch, 16) >= 0, swatch


@pytest.mark.parametrize("stored, sent", [
    ("#a3b4c5", "abc"), ("#000000", "000"), ("#ffffff", "fff"),
    (None, ""), ("", ""), ("red", ""), ("#abc", ""),
])
def test_a_colour_is_sent_in_three_characters(stored, sent):
    from ninaivu.api.api import _swatch
    assert _swatch(stored) == sent


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_segments_nonpositive_limit_cannot_disable_pagination(client, limit):
    response = client.get(f"/api/segments?limit={limit}")
    assert response.status_code == 200
    data = response.get_json()
    assert data["total"] == 15
    assert sum(len(segment["items"]) for segment in data["segments"]) == 1


def test_segments_group_by_day(client):
    data = client.get("/api/segments").get_json()
    keys = [segment["key"] for segment in data["segments"]]
    assert keys == sorted(keys, reverse=True)
    assert "2023-05-12" in keys


def test_assets_pagination(client):
    page = client.get("/api/assets?limit=3&offset=0").get_json()
    assert len(page["items"]) == 3
    assert page["total"] == 15
    second = client.get("/api/assets?limit=3&offset=3").get_json()
    assert {i["id"] for i in page["items"]} & {i["id"] for i in second["items"]} == set()


def test_public_payload_never_leaks_paths(client):
    item = client.get("/api/assets?limit=1").get_json()["items"][0]
    blob = json.dumps(item)
    assert "/tmp" not in blob
    assert "root" not in item
    assert "rel_path" not in item
    assert item["src"].startswith("/api/file/")


def test_kind_and_favorite_filters(client):
    ids = [i["id"] for i in client.get("/api/assets?limit=50").get_json()["items"]]
    client.post(f"/api/asset/{ids[0]}", json={"favorite": True})
    favorites = client.get("/api/assets?favorites=1").get_json()
    assert favorites["total"] == 1
    assert favorites["items"][0]["id"] == ids[0]

    pictures = client.get("/api/assets?kind=picture").get_json()
    assert pictures["total"] == 15
    videos = client.get("/api/assets?kind=video").get_json()
    assert videos["total"] == 0


def test_search_matches_filename(client):
    data = client.get("/api/assets?q=shot0").get_json()
    names = {i["name"] for i in data["items"]}
    assert "shot0.jpg" in names


def test_rating_round_trip_and_clamping(client):
    asset_id = client.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    assert client.post(f"/api/asset/{asset_id}", json={"rating": 4}).get_json()["rating"] == 4
    assert client.post(f"/api/asset/{asset_id}", json={"rating": 99}).get_json()["rating"] == 5
    assert client.post(f"/api/asset/{asset_id}", json={"rating": -3}).get_json()["rating"] == 0


def test_bulk_update(client):
    ids = [i["id"] for i in client.get("/api/assets?limit=4").get_json()["items"]]
    result = client.post("/api/assets/bulk", json={"ids": ids, "favorite": True}).get_json()
    assert result["updated"] == len(ids)
    assert client.get("/api/assets?favorites=1").get_json()["total"] == len(ids)


def test_thumbnails_are_cacheable(client):
    asset_id = client.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    response = client.get(f"/api/thumb/{asset_id}?s=256")
    assert response.status_code == 200
    assert response.headers["Content-Type"] in ("image/webp", "image/jpeg")
    assert "immutable" in response.headers["Cache-Control"]
    assert response.headers["ETag"]


def test_originals_support_range_requests(client):
    asset_id = client.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    full = client.get(f"/api/file/{asset_id}")
    assert full.headers["Accept-Ranges"] == "bytes"
    partial = client.get(f"/api/file/{asset_id}", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert len(partial.data) == 100


def test_download_sets_attachment(client):
    asset_id = client.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    response = client.get(f"/api/download/{asset_id}")
    assert "attachment" in response.headers["Content-Disposition"]


def test_unknown_ids_404(client):
    for path in ("/api/asset/99999", "/api/thumb/99999", "/api/file/99999"):
        assert client.get(path).status_code == 404


# -- security ---------------------------------------------------------------

def test_no_path_based_media_route_exists(client):
    """The old /media/<path> route is gone; ids are the only handle."""
    assert client.get("/media/../../etc/passwd").status_code == 404
    assert client.get("/api/file/../../../etc/passwd").status_code == 404
    assert client.get("/api/thumb/..%2f..%2fetc%2fpasswd").status_code == 404


def test_visibility_is_reported_on_every_item(client):
    item = client.get("/api/assets?limit=1").get_json()["items"][0]
    assert item["visibility"] == "family"


def test_set_root_accepts_any_ordinary_folder(client, tmp_path):
    """An admin may point the library anywhere their own media lives.
    See test_library_folder.py for the full rules, including --lock-roots."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    response = client.post("/api/library/root", json={"path": str(outside)})
    assert response.status_code == 200


def test_set_root_still_refuses_system_folders(client):
    response = client.post("/api/library/root", json={"path": "/etc"})
    assert response.status_code == 403
    assert "system folder" in response.get_json()["error"]


def test_set_root_rejects_files_and_missing_paths(client, tmp_path):
    missing = client.post("/api/library/root", json={"path": str(tmp_path / "nope")})
    assert missing.status_code == 400
    assert client.post("/api/library/root", json={"path": ""}).status_code == 400


def test_browse_lists_folders_and_offers_shortcuts(client):
    inside = client.get("/api/library/browse").get_json()
    assert inside["path"]
    assert inside["shortcuts"]
    # Kernel filesystems stay off limits even to an admin.
    assert client.get("/api/library/browse?path=/proc").status_code == 403


def test_security_headers_present(client):
    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_api_responses_vary_on_cookie(client):
    """Responses depend on who is signed in, so caches must not share them."""
    response = client.get("/api/assets?limit=1")
    assert "Cookie" in response.headers.get("Vary", "")
    assert "private" in client.get("/api/thumb/1").headers.get("Cache-Control", "")


def test_api_errors_are_json(client):
    response = client.get("/api/asset/424242")
    assert response.status_code == 404
    assert response.get_json()["status"] == 404


# -- discovery --------------------------------------------------------------

def test_facets_and_timeline(client):
    facets = client.get("/api/facets").get_json()
    assert {"tags", "folders", "cameras", "years"} <= facets.keys()
    assert any(c["name"] == "Acme TestCam" for c in facets["cameras"])

    timeline = client.get("/api/timeline").get_json()["days"]
    assert timeline and timeline[0]["date"] >= timeline[-1]["date"]


def test_duplicates_endpoint(client):
    data = client.get("/api/duplicates").get_json()
    names = {i["name"] for g in data["groups"] for i in g["items"]}
    assert {"shot0.jpg", "shot0_copy.jpg"} <= names
    assert data["total_wasted"] > 0


def test_similar_without_embeddings_is_graceful(client):
    asset_id = client.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    data = client.get(f"/api/similar/{asset_id}").get_json()
    assert data["items"] == []
    assert data["reason"] == "no-embedding"


def test_albums_round_trip(client):
    ids = [i["id"] for i in client.get("/api/assets?limit=3").get_json()["items"]]
    album = client.post("/api/albums", json={"name": "Trip", "ids": ids}).get_json()
    listing = client.get("/api/albums").get_json()["albums"]
    assert listing[0]["name"] == "Trip"
    assert listing[0]["n"] == 3

    client.post(f"/api/albums/{album['id']}/items",
                json={"ids": ids[:1], "remove": True})
    assert client.get("/api/albums").get_json()["albums"][0]["n"] == 2
    client.delete(f"/api/albums/{album['id']}")
    assert client.get("/api/albums").get_json()["albums"] == []


class _Ranker:
    """An engine that ranks the whole library, most recently indexed first."""

    semantic = True
    search_floor = 0.0

    def encode_text(self, text):
        import numpy as np
        return np.array([1, 0, 0, 0], "float32")


def test_search_filters_apply_before_the_results_are_paged(app, client, scanned):
    """Filtering each page of the ranking, rather than the ranking, gave a
    search among four holiday photos pages of none and a total of fifteen."""
    import numpy as np
    from ninaivu.storage import db

    _, conn, _ = scanned
    ids = [r["id"] for r in conn.execute("SELECT id FROM assets ORDER BY id")]
    for rank, asset_id in enumerate(ids):
        vector = np.array([1, rank / len(ids), 0, 0], "float32")
        db.store_embedding(conn, asset_id, "m", 4,
                           (vector / np.linalg.norm(vector)).tobytes())
    app.config["MV_SERVICES"].engine = _Ranker()

    pages = [client.get(f"/api/assets?q=beach&folder=shared&limit=2&offset={o}")
             .get_json() for o in (0, 2, 4)]

    assert all(p["semantic"] for p in pages), pages[0]
    assert [p["total"] for p in pages] == [4, 4, 4]
    assert [len(p["items"]) for p in pages] == [2, 2, 0]
    seen = [i["name"] for p in pages for i in p["items"]]
    assert sorted(seen) == [f"beach{n}.jpg" for n in range(4)]
    scores = [i["score"] for p in pages for i in p["items"]]
    assert scores == sorted(scores, reverse=True), "the ranking was lost"
