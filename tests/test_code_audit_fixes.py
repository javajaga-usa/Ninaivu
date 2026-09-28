"""Regressions for the 15 September 2026 code audit.

docs/audits/2026-09-15-code-audit/REPORT.md describes each finding; the number
in each section heading here is its number there.
"""

import io
import json
import struct
import threading
import time
import zlib
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, login


def first_id(client):
    return client.get("/api/assets?limit=1").get_json()["items"][0]["id"]


def huge_png_header(width=30000, height=30000) -> bytes:
    """A PNG that *declares* a vast image — all a decompression bomb needs."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# 1. Removing a library folder never widens anybody
# ---------------------------------------------------------------------------

def test_removing_a_library_leaves_sibling_folders_assignments_alone(app, people, as_admin,
                                                                     scanned, tmp_path):
    from ninaivu.server import auth
    cfg, conn, _ = scanned
    kids, private = tmp_path / "kids", tmp_path / "kids-private"
    kids.mkdir(), private.mkdir()
    cfg.roots += [str(kids), str(private)]
    auth.update_profile(conn, people["family"].id, library=str(private))

    # "kids" is a string prefix of "kids-private", but not its parent folder.
    response = as_admin.delete(f"/api/admin/libraries?path={kids}")
    assert response.status_code == 200, response.get_json()
    assert auth.get_user(conn, people["family"].id).library == str(private)


def test_a_member_whose_folder_is_removed_sees_nothing_rather_than_everything(
        app, people, as_admin, scanned, tmp_path):
    from ninaivu.server import auth
    cfg, conn, _ = scanned
    kids = tmp_path / "kids"
    kids.mkdir()
    cfg.roots.append(str(kids))
    auth.update_profile(conn, people["family"].id, library=str(kids))

    assert as_admin.delete(f"/api/admin/libraries?path={kids}&force=1").status_code == 200
    maya = login(app.test_client(), *FAMILY)
    assert maya.get("/api/assets?limit=200").get_json()["total"] == 0


# ---------------------------------------------------------------------------
# 2 and 5. A share link never shows more than its creator could see today
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    "UPDATE assets SET visibility=2 WHERE id=?",
    "UPDATE assets SET nsfw=1 WHERE id=?",
    "UPDATE assets SET kind='audio' WHERE id=?",
])
def test_a_photo_link_stops_when_the_photo_is_hidden_or_flagged(app, people, as_family,
                                                                 scanned, change):
    _, conn, _ = scanned
    asset_id = first_id(as_family)
    token = as_family.post("/api/shares", json={"scope": "asset", "target_id": asset_id}
                           ).get_json()["token"]
    anon = app.test_client()
    assert anon.get(f"/api/share/{token}/file/{asset_id}").status_code == 200

    conn.execute(change, (asset_id,))
    conn.commit()
    assert anon.get(f"/api/share/{token}").status_code == 404
    assert anon.get(f"/api/share/{token}/file/{asset_id}").status_code == 404
    assert anon.get(f"/api/share/{token}/thumb/{asset_id}").status_code == 404


def test_an_admin_may_still_share_a_hidden_photo_on_purpose(app, people, as_admin, scanned):
    _, conn, _ = scanned
    asset_id = first_id(as_admin)
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (asset_id,))
    conn.commit()
    token = as_admin.post("/api/shares", json={"scope": "asset", "target_id": asset_id}
                          ).get_json()["token"]
    assert app.test_client().get(f"/api/share/{token}/file/{asset_id}").status_code == 200


def test_a_folder_limited_member_cannot_share_an_ownerless_album_beyond_their_folder(
        app, people, as_admin, as_family, scanned):
    from ninaivu.server import auth
    _, conn, _ = scanned
    ids = [i["id"] for i in as_admin.get("/api/assets?limit=200").get_json()["items"]]
    album = as_admin.post("/api/albums", json={"name": "Old", "ids": ids}).get_json()["id"]
    conn.execute("UPDATE albums SET created_by=NULL WHERE id=?", (album,))
    conn.commit()
    auth.update_profile(conn, people["family"].id, scope="shared")

    visible = {i["id"] for i in as_family.get("/api/assets?limit=200").get_json()["items"]}
    token = as_family.post("/api/shares", json={"scope": "album", "target_id": album}
                           ).get_json()["token"]
    served = {i["id"] for i in app.test_client().get(f"/api/share/{token}").get_json()["items"]}
    assert served and served <= visible


def test_an_album_link_serves_its_own_photos_one_at_a_time_and_no_others(app, people,
                                                                         as_family):
    ids = [i["id"] for i in as_family.get("/api/assets?limit=5").get_json()["items"]]
    album = as_family.post("/api/albums", json={"name": "Trip", "ids": ids[:3]}).get_json()["id"]
    token = as_family.post("/api/shares", json={"scope": "album", "target_id": album}
                           ).get_json()["token"]
    anon = app.test_client()
    for asset_id in ids[:3]:
        assert anon.get(f"/api/share/{token}/thumb/{asset_id}").status_code == 200
    for asset_id in ids[3:]:
        assert anon.get(f"/api/share/{token}/thumb/{asset_id}").status_code == 404
        assert anon.get(f"/api/share/{token}/file/{asset_id}").status_code == 404


def test_a_link_dies_with_its_creators_profile(app, people, as_family, scanned):
    from ninaivu.server import auth
    _, conn, _ = scanned
    asset_id = first_id(as_family)
    token = as_family.post("/api/shares", json={"scope": "asset", "target_id": asset_id}
                           ).get_json()["token"]
    auth.set_active(conn, people["family"].id, False)
    assert app.test_client().get(f"/api/share/{token}/file/{asset_id}").status_code == 404


# ---------------------------------------------------------------------------
# 3. The archive hand-off obeys --lock-roots
# ---------------------------------------------------------------------------

def test_adopting_an_archive_outside_the_allowed_folders_is_refused(app, people, as_admin,
                                                                    scanned, tmp_path):
    cfg, _, _ = scanned
    cfg.lock_roots = True
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    response = as_admin.post("/api/archive/adopt", json={"path": str(outside)})
    assert response.status_code == 403
    assert str(outside.resolve()) not in cfg.roots


# ---------------------------------------------------------------------------
# 4. Writes from another page are refused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "http://localhost:8188"},
    {"Origin": "null"},
])
def test_a_forged_form_post_is_refused(app, people, as_admin, headers):
    response = as_admin.post("/api/archive/reset", data="x=1", content_type="text/plain",
                             headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://localhost"},
    {},
])
def test_the_consoles_own_requests_still_work(app, people, as_admin, headers):
    response = as_admin.post("/api/scan/stop", json={}, headers=headers)
    assert response.status_code == 200


def test_reads_are_not_affected(app, people, as_admin):
    response = as_admin.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 6. Backups carry the certificate authority and the cloud key
# ---------------------------------------------------------------------------

def test_backups_include_the_tls_folder_and_cloud_credentials(scanned, tmp_path):
    import tarfile
    from ninaivu.storage import backup
    from ninaivu.utils import tls
    cfg, _, _ = scanned
    folder = tls.tls_dir(cfg.state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ninaivu-ca.key").write_text("ca key")
    (folder / "ninaivu-ca.crt").write_text("ca")
    (cfg.state_dir / "cloud-encryption.json").write_text("{}")
    (cfg.state_dir / "google.json").write_text("{}")

    bundle = backup.snapshot(cfg.state_dir, tmp_path / "out")
    names = set(tarfile.open(bundle).getnames())
    assert {"state/tls/ninaivu-ca.key", "state/tls/ninaivu-ca.crt",
            "state/cloud-encryption.json", "state/google.json"} <= names
    restored = backup.extract(bundle, tmp_path / "restored")
    backup.verify_state(restored)


def test_restoring_an_older_bundle_keeps_this_machines_certificate_authority(tmp_path):
    from tools import backup_restore as restore
    existing, extracted, candidate = tmp_path / "live", tmp_path / "bundle", tmp_path / "new"
    (existing / "tls").mkdir(parents=True)
    (existing / "tls" / "ninaivu-ca.key").write_text("current CA")
    (existing / "google.json").write_text("current sign-in")
    extracted.mkdir()
    (extracted / "config.json").write_text("{}")
    restore._prepare_replacement(existing, extracted, candidate)
    assert (candidate / "tls" / "ninaivu-ca.key").read_text() == "current CA"
    assert (candidate / "google.json").read_text() == "current sign-in"

    (extracted / "tls").mkdir()
    (extracted / "tls" / "ninaivu-ca.key").write_text("backed-up CA")
    newer = tmp_path / "newer"
    restore._prepare_replacement(existing, extracted, newer)
    assert (newer / "tls" / "ninaivu-ca.key").read_text() == "backed-up CA"


# ---------------------------------------------------------------------------
# 7. Video conversions are bounded and cannot hang on their own errors
# ---------------------------------------------------------------------------

def test_no_more_than_the_allowed_number_of_conversions_run_at_once(tmp_path, monkeypatch):
    from ninaivu.utils import proxies
    store = proxies.ProxyStore(tmp_path)
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: "ffmpeg")
    running, peak, release = [0], [0], threading.Event()
    lock = threading.Lock()

    def convert(*_args, **_kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(5)
        with lock:
            running[0] -= 1

    monkeypatch.setattr(store, "_convert", convert)
    for asset_id in range(1, 7):
        source = tmp_path / f"clip{asset_id}.avi"
        source.write_bytes(b"x")
        store.start(asset_id, source)
    time.sleep(0.5)
    assert peak[0] == proxies.MAX_CONVERSIONS
    release.set()


def test_ffmpeg_errors_are_not_written_into_an_undrained_pipe(tmp_path, monkeypatch):
    import subprocess
    from ninaivu.utils import proxies
    seen = {}

    class FakeProcess:
        returncode = 1
        stdout = iter(())

        def __init__(self, command, **kwargs):
            seen.update(kwargs)

        def wait(self):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(proxies.subprocess, "Popen", FakeProcess)
    source = tmp_path / "clip.avi"
    source.write_bytes(b"not a video")
    store = proxies.ProxyStore(tmp_path)
    store.start(1, source)
    for _ in range(50):
        if store.status(1).state != "building":
            break
        time.sleep(0.05)
    assert seen.get("stderr") is not subprocess.PIPE
    assert store.status(1).state == "failed"


# ---------------------------------------------------------------------------
# 8. An image's declared size is checked before it is decoded
# ---------------------------------------------------------------------------

def test_an_avatar_declaring_a_gigapixel_is_refused_before_decoding(app, people, monkeypatch):
    from PIL import ImageFile
    decoded = []
    real_load = ImageFile.ImageFile.load
    monkeypatch.setattr(ImageFile.ImageFile, "load",
                        lambda self: decoded.append(self.size) or real_load(self))
    guest = login(app.test_client(), "neighbour", "visitingpass9")
    response = guest.post("/api/me/avatar", data={
        "avatar": (io.BytesIO(huge_png_header()), "bomb.png", "image/png")})
    assert response.status_code == 400
    assert decoded == [], "the pixels must never be decoded"


def test_open_untrusted_refuses_oversized_images():
    from ninaivu.media.safe_image import open_untrusted
    with pytest.raises(ValueError):
        open_untrusted(huge_png_header())
    small = io.BytesIO()
    Image.new("RGB", (8, 8)).save(small, "PNG")
    with open_untrusted(small.getvalue()) as img:
        assert img.size == (8, 8)


# ---------------------------------------------------------------------------
# 9. Approving an upload does not copy gigabytes inside the database lock
# ---------------------------------------------------------------------------

def test_a_cross_drive_approval_copies_before_taking_the_write_lock(scanned, monkeypatch):
    from ninaivu.media import date_edit, upload_review
    from ninaivu.server import auth
    from ninaivu.storage import db
    cfg, conn, _ = scanned
    auth.init_auth_schema(conn)
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(image, "JPEG")

    class Upload:
        def save(self, stream):
            stream.write(image.getvalue())

    staged = upload_review.stage(conn, cfg, Upload(), "big-video.jpg", cfg.active_root, "", None)
    copies = []
    real_copy = date_edit.copy_exclusive

    def watched_copy(source, target):
        copies.append(db._write_lock.locked())
        return real_copy(source, target)

    monkeypatch.setattr(upload_review, "_same_device", lambda *_: False)
    monkeypatch.setattr(date_edit, "copy_exclusive", watched_copy)
    asset = upload_review.approve(conn, cfg, staged["id"], None, "2020-01-02")
    assert copies == [False]
    published = Path(asset["root"]) / asset["rel_path"]
    assert published.read_bytes() == image.getvalue()
    assert not list(published.parent.glob(".ninaivu-approve-*"))
    row = upload_review.get(conn, staged["id"])
    assert not upload_review.source_path(cfg, row).exists()


def test_publishing_works_on_drives_without_hard_links(tmp_path, monkeypatch):
    """exFAT and FAT32 external drives cannot link; publishing must still work."""
    import os
    from ninaivu.media import date_edit

    def no_links(*_args, **_kwargs):
        raise OSError(1, "Operation not permitted")

    staged, target = tmp_path / ".ninaivu-approve-1.part", tmp_path / "photo.jpg"
    staged.write_bytes(b"photo")
    monkeypatch.setattr(os, "link", no_links)
    date_edit.publish_exclusive(staged, target)
    assert target.read_bytes() == b"photo" and not staged.exists()

    again = tmp_path / ".ninaivu-approve-2.part"
    again.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        date_edit.publish_exclusive(again, target)
    assert target.read_bytes() == b"photo"


# ---------------------------------------------------------------------------
# 10 and 11. Small correctness fixes
# ---------------------------------------------------------------------------

def test_the_folder_view_calls_public_items_public(app, people, as_admin, scanned):
    _, conn, _ = scanned
    row = conn.execute("SELECT id FROM assets WHERE folder='misc'").fetchone()
    conn.execute("UPDATE assets SET visibility=0 WHERE id=?", (row["id"],))
    conn.commit()
    items = as_admin.get("/api/admin/folder?folder=misc").get_json()["items"]
    assert next(i for i in items if i["id"] == row["id"])["visibility_name"] == "public"


def test_bad_numbers_are_a_400_not_a_500(app, people, as_family, as_admin, scanned):
    cfg, _, _ = scanned
    asset_id = first_id(as_family)
    assert as_family.post(f"/api/asset/{asset_id}", json={"rating": "five"}).status_code == 400
    assert as_family.post("/api/assets/bulk", json={"ids": [asset_id], "rating": []}
                          ).status_code == 400
    assert as_admin.post("/api/visibility/undo", json={"batch_id": "x"}).status_code == 400
    assert as_admin.get("/api/admin/problems?limit=lots").status_code == 200
    assert as_admin.get("/api/admin/large-files?limit=lots").status_code == 200
    before = cfg.notify_smtp_host
    response = as_admin.post("/api/admin/notifications",
                             json={"smtp_host": "mail.example", "smtp_port": "abc"})
    assert response.status_code == 400
    assert cfg.notify_smtp_host == before
    assert as_admin.post("/api/admin/large-files/keep", json={"ids": ["x"]}).status_code == 400


# ---------------------------------------------------------------------------
# 12. Ninaivu's certificate authority can only vouch for home-network names
# ---------------------------------------------------------------------------

def test_a_new_ca_is_name_constrained_and_its_leaf_fits_inside(tmp_path):
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID
    from ninaivu.utils import tls

    cert, _ = tls.ensure_certificate(tmp_path, extra_hosts=("photos.example.com", "ninaivu.lan"))
    ca = x509.load_pem_x509_certificate(tls.ca_certificate_path(tmp_path).read_bytes())
    constraint = ca.extensions.get_extension_for_oid(ExtensionOID.NAME_CONSTRAINTS).value
    permitted = {getattr(t.value, "value", t.value) for t in constraint.permitted_subtrees}
    assert "local" in permitted

    leaf = x509.load_pem_x509_certificate(cert.read_bytes())
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = san.get_values_for_type(x509.DNSName)
    assert "ninaivu.lan" in names and "photos.example.com" not in names
    constraints = tls._load_constraints(tls.tls_dir(tmp_path))
    assert all(tls.permitted_name(n, constraints) for n in names)
    assert all(tls.permitted_address(str(a), constraints)
               for a in san.get_values_for_type(x509.IPAddress))


def test_a_ca_made_before_constraints_keeps_every_name(tmp_path):
    from cryptography import x509
    from ninaivu.utils import tls

    tls.ensure_certificate(tmp_path)
    (tls.tls_dir(tmp_path) / tls.CONSTRAINTS_FILE).unlink()      # as an older Ninaivu made it
    cert, _ = tls.ensure_certificate(tmp_path, extra_hosts=("photos.example.com",))
    leaf = x509.load_pem_x509_certificate(cert.read_bytes())
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "photos.example.com" in san.get_values_for_type(x509.DNSName)


# ---------------------------------------------------------------------------
# 13. A PIN cannot be walked from many addresses
# ---------------------------------------------------------------------------

def test_a_pin_profile_pauses_after_repeated_failures_from_anywhere(app, people, scanned):
    from ninaivu.api import accounts_api
    from ninaivu.server import auth
    _, conn, _ = scanned
    accounts_api._ATTEMPTS.clear()
    auth.set_pin(conn, people["family"].id, "4826")
    client = app.test_client()
    user_id = people["family"].id
    for i in range(accounts_api._PROFILE_MAX_ATTEMPTS):
        response = client.post("/api/auth/enter", json={"id": user_id, "secret": f"{i:04d}"},
                               environ_overrides={"REMOTE_ADDR": f"10.0.{i}.1"})
        assert response.status_code == 401
    # The right PIN, from a fresh address, still has to wait.
    response = client.post("/api/auth/enter", json={"id": user_id, "secret": "4826"},
                           environ_overrides={"REMOTE_ADDR": "10.9.9.9"})
    assert response.status_code == 429
    accounts_api._ATTEMPTS.clear()


# ---------------------------------------------------------------------------
# 14. Live-photo clips get the same checks as any other file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("companion", ["../../outside.mov", "clip.html"])
def test_live_video_is_confined_to_the_library_and_to_video_types(app, people, as_family,
                                                                  scanned, tmp_path, companion):
    cfg, conn, _ = scanned
    asset_id = first_id(as_family)
    row = conn.execute("SELECT root, folder FROM assets WHERE id=?", (asset_id,)).fetchone()
    target = (Path(row["root"]) / row["folder"] / companion).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"<script>alert(1)</script>")
    rel = f"{row['folder']}/{companion}" if row["folder"] else companion
    conn.execute("UPDATE assets SET is_live=1, live_video_path=? WHERE id=?", (rel, asset_id))
    conn.commit()
    assert as_family.get(f"/api/live-video/{asset_id}").status_code == 404


# ---------------------------------------------------------------------------
# 15. A forwarded request is not "this machine"
# ---------------------------------------------------------------------------

def test_shutdown_through_a_proxy_is_refused(app):
    from ninaivu import create_admin_app
    admin = create_admin_app(app.config["MV_SERVICES"])
    admin.config["MV_STOP_TOKEN"] = "the-right-token"
    called = []
    admin.config["MV_SHUTDOWN"] = lambda: called.append(True)
    response = admin.test_client().post(
        "/api/admin/shutdown", json={"token": "the-right-token"},
        headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 404 and called == []


# ---------------------------------------------------------------------------
# 17. Signing in takes the same work whether or not the name exists
# ---------------------------------------------------------------------------

def test_login_runs_one_scrypt_for_unknown_and_passwordless_names(scanned, monkeypatch):
    from ninaivu.server import auth
    _, conn, _ = scanned
    auth.create_user(conn, "dad", ADMIN[1], role="admin")
    auth.create_user(conn, "kid", "", role="family")
    auth._decoy_hash()                                  # made once, up front
    calls = []
    real = auth.kdf.scrypt
    monkeypatch.setattr(auth.kdf, "scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))

    for name, password in (("dad", "wrong-password"), ("nobody", "wrong-password"),
                           ("kid", "wrong-password")):
        calls.clear()
        assert auth.authenticate(conn, name, password) is None
        assert len(calls) == 1, name


def test_the_kept_note_is_json(tmp_path):
    """Kept-original notes are plain JSON, readable without Ninaivu."""
    from ninaivu.storage import recycle
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(root / "a" / "p.jpg")
    kept = recycle.keep_original(root, "a/p.jpg")
    note = json.loads(kept.with_name(kept.name + ".ninaivu-source").read_text())
    assert {"ino", "dev", "version"} <= set(note)
