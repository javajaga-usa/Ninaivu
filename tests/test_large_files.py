"""The largest-files worklist on the admin console.

Not a filter and not a verdict — everything here still shows up everywhere
else in the library exactly as it did before. "Keep" only ever means "stop
asking" (see db.mark_large_files_reviewed); nothing about the file itself
changes. Deleting goes through the ordinary password-gated /api/delete, so
those rules are covered in test_delete_and_folders.py, not repeated here.
"""

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.storage import db


def set_asset(conn, asset_id, **fields):
    columns = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE assets SET {columns} WHERE id=?",
                 (*fields.values(), asset_id))
    conn.commit()


def an_asset_id(conn, root):
    return int(conn.execute(
        "SELECT id FROM assets WHERE root=? LIMIT 1", (root,)).fetchone()["id"])


MB = 1024 * 1024


# --- who may look ------------------------------------------------------

@pytest.mark.parametrize("who", ["family", "guest"])
def test_only_an_admin_can_list_large_files(app, people, who):
    client = login(app.test_client(), *(FAMILY if who == "family" else GUEST))
    response = client.get("/api/admin/large-files")
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("who", ["family", "guest"])
def test_only_an_admin_can_keep_a_large_file(app, people, who):
    client = login(app.test_client(), *(FAMILY if who == "family" else GUEST))
    response = client.post("/api/admin/large-files/keep", json={"ids": [1]})
    assert response.status_code in (401, 403)


# --- the floor -----------------------------------------------------------

def test_files_under_the_floor_are_not_listed(as_admin, scanned):
    _, conn, _ = scanned
    big = an_asset_id(conn, scanned[0].active_root)
    set_asset(conn, big, size=600 * MB)

    body = as_admin.get("/api/admin/large-files").get_json()
    assert [i["id"] for i in body["items"]] == [big]


def test_the_default_floor_is_five_hundred_megabytes(as_admin, scanned):
    _, conn, _ = scanned
    just_under = an_asset_id(conn, scanned[0].active_root)
    set_asset(conn, just_under, size=499 * MB)

    body = as_admin.get("/api/admin/large-files").get_json()
    assert body["items"] == []
    assert body["floor_mb"] == 500


def test_a_custom_floor_is_honoured(as_admin, scanned):
    _, conn, _ = scanned
    modest = an_asset_id(conn, scanned[0].active_root)
    set_asset(conn, modest, size=150 * MB)

    low = as_admin.get("/api/admin/large-files?min_mb=100").get_json()
    assert modest in [i["id"] for i in low["items"]]
    assert low["floor_mb"] == 100

    high = as_admin.get("/api/admin/large-files?min_mb=500").get_json()
    assert modest not in [i["id"] for i in high["items"]]


def test_largest_first(as_admin, scanned):
    cfg, conn, _ = scanned
    rows = conn.execute(
        "SELECT id FROM assets WHERE root=? ORDER BY id LIMIT 3", (cfg.active_root,)
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    set_asset(conn, ids[0], size=600 * MB)
    set_asset(conn, ids[1], size=900 * MB)
    set_asset(conn, ids[2], size=700 * MB)

    body = as_admin.get("/api/admin/large-files").get_json()
    assert [i["id"] for i in body["items"]] == [ids[1], ids[2], ids[0]]


# --- the "no camera info" hint --------------------------------------------

def test_a_picture_with_no_camera_metadata_is_flagged(as_admin, scanned):
    cfg, conn, _ = scanned
    target = an_asset_id(conn, cfg.active_root)
    set_asset(conn, target, size=600 * MB, kind="picture",
              camera=None, date_source="mtime")

    body = as_admin.get("/api/admin/large-files").get_json()
    row = next(i for i in body["items"] if i["id"] == target)
    assert row["no_camera"] is True


def test_a_picture_with_camera_metadata_is_not_flagged(as_admin, scanned):
    cfg, conn, _ = scanned
    target = an_asset_id(conn, cfg.active_root)
    set_asset(conn, target, size=600 * MB, kind="picture",
              camera="Acme", date_source="exif")

    body = as_admin.get("/api/admin/large-files").get_json()
    row = next(i for i in body["items"] if i["id"] == target)
    assert row["no_camera"] is False


def test_a_long_video_with_no_camera_metadata_is_flagged(as_admin, scanned):
    cfg, conn, _ = scanned
    target = an_asset_id(conn, cfg.active_root)
    set_asset(conn, target, size=600 * MB, kind="video",
              camera=None, date_source="mtime", duration=1800)

    body = as_admin.get("/api/admin/large-files").get_json()
    row = next(i for i in body["items"] if i["id"] == target)
    assert row["no_camera"] is True


def test_a_short_video_with_no_camera_metadata_is_not_flagged(as_admin, scanned):
    """A short clip with no EXIF is an ordinary phone video — most phone
    video never carries a 'camera' tag the way photos do — so duration is
    what keeps this from flagging every home movie in the library."""
    cfg, conn, _ = scanned
    target = an_asset_id(conn, cfg.active_root)
    set_asset(conn, target, size=600 * MB, kind="video",
              camera=None, date_source="mtime", duration=45)

    body = as_admin.get("/api/admin/large-files").get_json()
    row = next(i for i in body["items"] if i["id"] == target)
    assert row["no_camera"] is False


# --- keeping ---------------------------------------------------------------

def test_keeping_a_file_removes_it_from_the_list(as_admin, scanned):
    _, conn, _ = scanned
    target = an_asset_id(conn, scanned[0].active_root)
    set_asset(conn, target, size=600 * MB)

    response = as_admin.post("/api/admin/large-files/keep", json={"ids": [target]})
    assert response.status_code == 200
    assert response.get_json()["kept"] == 1

    body = as_admin.get("/api/admin/large-files").get_json()
    assert target not in [i["id"] for i in body["items"]]


def test_keeping_does_not_touch_the_file_or_its_visibility(as_admin, scanned):
    """Keep is a note to Ninaivu, not an action on the file — trashed status,
    size and visibility are all untouched."""
    cfg, conn, _ = scanned
    target = an_asset_id(conn, cfg.active_root)
    set_asset(conn, target, size=600 * MB, visibility=1)

    as_admin.post("/api/admin/large-files/keep", json={"ids": [target]})

    row = conn.execute("SELECT * FROM assets WHERE id=?", (target,)).fetchone()
    assert row["trashed"] == 0
    assert row["size"] == 600 * MB
    assert row["visibility"] == 1
    assert row["large_file_reviewed_at"] is not None


def test_keeping_requires_at_least_one_id(as_admin):
    response = as_admin.post("/api/admin/large-files/keep", json={"ids": []})
    assert response.status_code == 400


def test_a_kept_file_can_still_be_found_and_deleted_the_ordinary_way(as_admin, scanned):
    """Keep only removes a file from this one worklist — it is not a second,
    quieter kind of hidden. The gallery's own delete still sees it."""
    _, conn, _ = scanned
    target = an_asset_id(conn, scanned[0].active_root)
    set_asset(conn, target, size=600 * MB)
    as_admin.post("/api/admin/large-files/keep", json={"ids": [target]})

    response = as_admin.post("/api/delete", json={"ids": [target], "password": ADMIN[1]})
    assert response.status_code == 200, response.get_json()


# --- the column itself survives an upgrade --------------------------------

def test_the_reviewed_column_is_healed_onto_a_database_that_predates_it(tmp_path):
    """Same class of bug the visibility columns already guard against — a
    database from before this feature shipped must gain the column rather
    than have every large-files call fail with 'no such column'."""
    path = tmp_path / "assets.db"
    conn = db.init_db(path)
    conn.execute("ALTER TABLE assets DROP COLUMN large_file_reviewed_at")
    conn.commit()
    assert "large_file_reviewed_at" not in db.columns(conn, "assets")

    assert db.heal_schema(conn) == {"assets": ["large_file_reviewed_at"]}
    assert "large_file_reviewed_at" in db.columns(conn, "assets")
