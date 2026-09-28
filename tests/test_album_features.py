"""Tests for comprehensive album functionality:
- Empty albums remain visible to creator and admins
- Filtering assets (?album=ID) and segments (?album=ID)
- GET /api/albums/<id> endpoint
- PATCH /api/albums/<id> endpoint
- Album access control and ownership
"""

from conftest import ADMIN, FAMILY, GUEST, login


def test_empty_album_visible_to_creator_and_admin(app, people, scanned):
    """An empty album must be visible to its creator and admins, but not to other viewers."""
    admin_c = login(app.test_client(), *ADMIN)
    fam_c = login(app.test_client(), *FAMILY)

    # Family creates an empty album
    created = fam_c.post("/api/albums", json={"name": "Family Empty Album"}).get_json()
    album_id = created["id"]
    assert album_id

    # Creator (family) should see their empty album
    fam_albums = fam_c.get("/api/albums").get_json().get("albums", [])
    fam_names = [a["name"] for a in fam_albums]
    assert "Family Empty Album" in fam_names
    fam_album = next(a for a in fam_albums if a["name"] == "Family Empty Album")
    assert fam_album["n"] == 0

    # Admin should also see it
    admin_albums = admin_c.get("/api/albums").get_json().get("albums", [])
    admin_names = [a["name"] for a in admin_albums]
    assert "Family Empty Album" in admin_names

    # Admin creates an empty album
    admin_c.post("/api/albums", json={"name": "Admin Private Album"})
    fam_albums_after = fam_c.get("/api/albums").get_json().get("albums", [])
    assert "Admin Private Album" not in [a["name"] for a in fam_albums_after]


def test_filter_assets_and_segments_by_album(app, people, scanned):
    """Querying /api/assets?album=ID and /api/segments?album=ID only returns items in that album."""
    fam_c = login(app.test_client(), *FAMILY)

    # Get some assets
    all_assets = fam_c.get("/api/assets?limit=5").get_json()["items"]
    assert len(all_assets) >= 3
    target_ids = [all_assets[0]["id"], all_assets[1]["id"]]

    # Create album with target_ids
    album = fam_c.post("/api/albums", json={"name": "Selected Shots", "ids": target_ids}).get_json()
    album_id = album["id"]

    # Filter /api/assets?album=album_id
    filtered = fam_c.get(f"/api/assets?album={album_id}").get_json()
    filtered_ids = [item["id"] for item in filtered["items"]]
    assert sorted(filtered_ids) == sorted(target_ids)
    assert filtered["total"] == 2

    # Filter /api/segments?album=album_id
    segments_data = fam_c.get(f"/api/segments?album={album_id}").get_json()
    segment_item_ids = []
    for segment in segments_data.get("segments", []):
        for item in segment["items"]:
            segment_item_ids.append(item[0])  # item is [id, aspect, kind, flags, duration, thumb_v]
    assert sorted(segment_item_ids) == sorted(target_ids)


def test_get_and_patch_album(app, people, scanned):
    """GET /api/albums/<id> and PATCH /api/albums/<id>."""
    fam_c = login(app.test_client(), *FAMILY)

    all_assets = fam_c.get("/api/assets?limit=5").get_json()["items"]
    target_ids = [all_assets[0]["id"], all_assets[1]["id"]]

    # Create album
    album = fam_c.post("/api/albums", json={"name": "Vacation 2024", "ids": target_ids}).get_json()
    album_id = album["id"]

    # GET /api/albums/<id>
    res = fam_c.get(f"/api/albums/{album_id}")
    assert res.status_code == 200
    data = res.get_json()["album"]
    assert data["name"] == "Vacation 2024"
    assert sorted(data["item_ids"]) == sorted(target_ids)
    assert data["n"] == 2

    # PATCH /api/albums/<id> to rename and set cover
    patch_res = fam_c.patch(f"/api/albums/{album_id}", json={
        "name": "Summer Vacation 2024",
        "cover_id": target_ids[1],
    })
    assert patch_res.status_code == 200
    updated = patch_res.get_json()["album"]
    assert updated["name"] == "Summer Vacation 2024"
    assert updated["cover_id"] == target_ids[1]

    # Another family member (or non-owner non-admin) cannot PATCH
    # Log in as a different user or test guest
    guest_c = login(app.test_client(), *GUEST)
    assert guest_c.get(f"/api/albums/{album_id}").status_code == 403
    assert guest_c.patch(f"/api/albums/{album_id}", json={"name": "Hacked"}).status_code == 403


def test_album_share_and_delete(app, people, scanned):
    """Creating a share link for an album and deleting an album."""
    fam_c = login(app.test_client(), *FAMILY)

    all_assets = fam_c.get("/api/assets?limit=5").get_json()["items"]
    target_ids = [all_assets[0]["id"], all_assets[1]["id"]]

    # Create album
    album = fam_c.post("/api/albums", json={"name": "Shareable Album", "ids": target_ids}).get_json()
    album_id = album["id"]

    # Share the album
    share = fam_c.post("/api/shares", json={
        "scope": "album",
        "target_id": album_id,
        "expires_in_days": 30,
    }).get_json()
    assert share["token"]
    assert share["share_url"] == f"/share/{share['token']}"

    # Public client accesses shared album
    anon = app.test_client()
    view = anon.get(f"/api/share/{share['token']}").get_json()
    assert view["scope"] == "album"
    assert view["album"]["name"] == "Shareable Album"
    assert len(view["items"]) == 2

    # Delete the album
    del_res = fam_c.delete(f"/api/albums/{album_id}")
    assert del_res.status_code == 200
    assert fam_c.get(f"/api/albums/{album_id}").status_code == 404


def test_ui_elements_in_template():
    """Verify index.html contains the necessary album UI blocks and controls."""
    from pathlib import Path
    html = (Path("ninaivu") / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="albums-block"' in html
    assert 'id="new-album-btn"' in html
    assert 'id="album-list"' in html
    assert 'id="sel-album"' in html
    assert 'id="sel-album-remove"' in html
    assert 'id="v-album"' in html
    assert 'id="album-modal"' in html
    assert 'id="album-create-btn"' in html
