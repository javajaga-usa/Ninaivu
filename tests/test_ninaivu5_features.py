"""Comprehensive unit and integration test suite for Ninaivu 5.0 features."""

from __future__ import annotations

import io
import time

import pytest

from ninaivu.server import auth
from ninaivu import create_app
from ninaivu.storage import db
from ninaivu.media import media
from ninaivu.server.config import Config
from ninaivu.cloud import crypto


@pytest.fixture
def temp_env(tmp_path):
    state_dir = tmp_path / "state"
    lib_dir = tmp_path / "library"
    state_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)

    cfg = Config()
    cfg.state_dir = state_dir
    cfg.active_root = str(lib_dir)
    cfg.roots = [str(lib_dir)]
    cfg.port = 5000
    cfg.admin_port = 3000
    cfg.ai_enabled = False
    cfg.ai_engine = "off"
    cfg.watch = False
    cfg.min_media_bytes = 0
    cfg.open_browsing = True
    cfg.ensure_dirs()
    cfg.save()

    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    from ninaivu.server import date_policy
    import json
    db.set_meta(conn, date_policy.KEY, json.dumps(dict(
        date_policy.DEFAULTS, admin="all", family="all", guest="all")))
    admin = auth.bootstrap_admin(conn, "admin", "password123", "Admin User")

    app = create_app(cfg, face="home")
    admin_app = create_app(cfg, face="admin")

    return {
        "cfg": cfg,
        "conn": conn,
        "lib_dir": lib_dir,
        "state_dir": state_dir,
        "admin_id": admin.id,
        "app": app,
        "admin_app": admin_app,
    }


def test_zero_knowledge_crypto():
    passphrase = "SuperSecretFamilyPassphrase!2026"
    raw_media = b"JPEG_IMAGE_RAW_BINARY_DATA_TEST_12345" * 100

    encrypted = crypto.encrypt_bytes(raw_media, passphrase)
    assert encrypted.startswith(crypto.MAGIC)
    assert encrypted != raw_media

    decrypted = crypto.decrypt_bytes(encrypted, passphrase)
    assert decrypted == raw_media

    # Test wrong passphrase
    # Authenticated decryption rejects the wrong key outright. A bare
    # Exception would also pass if decrypt_bytes were simply broken.
    from cryptography.exceptions import InvalidTag
    with pytest.raises(InvalidTag):
        crypto.decrypt_bytes(encrypted, "WrongPassword!")


def test_motion_photo_detection(tmp_path):
    dummy_img = tmp_path / "motion_test.jpg"
    # Create fake JPEG with embedded MotionPhoto XMP tag
    dummy_img.write_bytes(b"\xff\xd8\xff\xe1\x00\x18http://ns.google.com/photos/1.0/camera/ GCamera:MotionPhoto=\"1\"\xff\xd9")
    assert media.detect_motion_photo(dummy_img) is True

    normal_img = tmp_path / "normal_test.jpg"
    normal_img.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\xff\xd9")
    assert media.detect_motion_photo(normal_img) is False


def test_ninaivu5_api_endpoints(temp_env):
    app = temp_env["app"]
    client = app.test_client()

    # Sign in as admin
    login_res = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert login_res.status_code == 200

    # 1. Test Upload API
    fake_img_content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xd9"
    data = {
        "file": (io.BytesIO(fake_img_content), "vacation_photo.jpg")
    }
    upload_res = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert upload_res.status_code == 200
    upload_json = upload_res.get_json()
    assert upload_json["total"] == 1
    asset_id = upload_json["uploaded"][0]["id"]
    assert asset_id > 0
    admin_client = temp_env["admin_app"].test_client()
    admin_client.post('/api/auth/login', json={'username': 'admin', 'password': 'password123'})
    approved = admin_client.post(f'/api/admin/uploads/{asset_id}/approve',
                                 json={'creation_date': '2020-01-01'})
    assert approved.status_code == 200, approved.get_json()
    asset_id = approved.get_json()['item']['id']

    # 2. Test Memories / On This Day API
    memories_res = client.get("/api/memories/on-this-day")
    assert memories_res.status_code == 200
    memories_json = memories_res.get_json()
    assert "items" in memories_json
    assert "years" in memories_json

    # 3. Test Geo Points API
    geo_res = client.get("/api/geo/points")
    assert geo_res.status_code == 200
    geo_json = geo_res.get_json()
    assert "points" in geo_json
    assert "total" in geo_json

    # 4. Test Tokenized Share Link API
    share_res = client.post("/api/shares", json={
        "scope": "asset",
        "target_id": asset_id,
        "expires_in_days": 7,
        "password": "share_secret_pin",
    })
    assert share_res.status_code == 200
    share_json = share_res.get_json()
    token = share_json["token"]
    assert token

    # Access shared asset without password -> 401
    guest_client = app.test_client()
    view_res = guest_client.get(f"/api/share/{token}")
    assert view_res.status_code == 401

    # Access shared asset with correct password -> 200
    view_ok_res = guest_client.get(f"/api/share/{token}", headers={"X-Share-Password": "share_secret_pin"})
    assert view_ok_res.status_code == 200
    view_ok_json = view_ok_res.get_json()
    assert view_ok_json["scope"] == "asset"
    assert view_ok_json["item"]["id"] == asset_id

    # List shares
    list_shares_res = client.get("/api/shares")
    assert list_shares_res.status_code == 200
    assert len(list_shares_res.get_json()["shares"]) >= 1

    # Delete share
    del_share_res = client.delete(f"/api/shares/{token}")
    assert del_share_res.status_code == 200

    # 5. Test the people API.
    #
    # These paths changed when face recognition stopped being a stub. Creating
    # a person is management and moved to /api/faces/person on the console;
    # /api/faces/clusters now means "groups the matcher formed that nobody has
    # named yet", which is a different thing. The old GET was also unauthenticated,
    # so anybody who could reach the port could read every name in the house.
    person_res = client.post("/api/faces/person", json={
        "name": "Sarah",
        "avatar_asset_id": asset_id,
    })
    assert person_res.status_code == 200
    assert person_res.get_json()["person"]["name"] == "Sarah"

    clusters_res = client.get("/api/faces/clusters")
    assert clusters_res.status_code == 200
    assert "clusters" in clusters_res.get_json()


def test_bitrot_scrubber_admin_api(temp_env):
    admin_app = temp_env["admin_app"]
    client = admin_app.test_client()

    # Sign in on admin face
    login_res = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert login_res.status_code == 200

    # Start Scrubber
    start_res = client.post("/api/admin/scrubber/start")
    assert start_res.status_code == 200
    assert start_res.get_json()["ok"] is True

    # Check status
    time.sleep(0.2)
    status_res = client.get("/api/admin/scrubber/status")
    assert status_res.status_code == 200
    status_json = status_res.get_json()
    assert "total_assets" in status_json
    assert "verified" in status_json
    assert "progress" in status_json
