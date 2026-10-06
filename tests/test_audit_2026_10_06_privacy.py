"""Privacy and sign-in fixes from the audit of 6 October 2026.

Each test states one finding as it was found: videos of several kinds sent to
guests as they were (A-01), any device passing for this computer behind a
trusted proxy (A-02), videos indexed without their place (A-08), a JPEG's
second frame and trailers surviving the strip (A-09), guests told the town
(A-10), oversized pictures (A-11), session tokens kept as they are (A-12),
the settings file briefly readable (A-32), lockouts forgotten at a restart
(A-33), notices dropped after one long outage (A-44), an unbounded body
(A-45), the desktop panel's readings (A-46) and ffmpeg reading addresses
(A-56).
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu.server import auth
from ninaivu.server.config import VIDEO_EXTS

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")

ORIGINAL = b"\x00\x00\x00\x18ftypmp42 the original, with GPS in it"


# -- A-01: every kind of video, decided by the index, never by a guess ----------

def _video(scanned, ext: str) -> int:
    cfg, conn, _ = scanned
    library = Path(cfg.active_root)
    name = f"clip{ext}"
    (library / "misc" / name).write_bytes(ORIGINAL)
    asset_id = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, duration, "
        "visibility) VALUES(?,?,?,?,?,?,?,?,0)",
        (str(library), f"misc/{name}", name, "misc", ext.lstrip("."), "video",
         len(ORIGINAL), 2.0)).lastrowid
    conn.commit()
    return asset_id


def _no_ffmpeg(monkeypatch):
    from ninaivu.media import stripped_video
    monkeypatch.setattr(stripped_video, "stripped_copy", lambda *a, **k: None)


@pytest.mark.parametrize("ext", sorted(VIDEO_EXTS))
def test_a_guest_is_never_sent_the_original_of_any_video(app, people, as_guest, scanned,
                                                         monkeypatch, ext):
    clip = _video(scanned, ext)
    _no_ffmpeg(monkeypatch)
    answer = as_guest.get(f"/api/file/{clip}")
    assert answer.status_code == 409, (ext, answer.status_code)
    assert ORIGINAL not in answer.data


@pytest.mark.parametrize("ext", sorted(VIDEO_EXTS))
def test_a_share_link_is_never_sent_the_original_of_any_video(app, people, as_family, scanned,
                                                              monkeypatch, ext):
    clip = _video(scanned, ext)
    _no_ffmpeg(monkeypatch)
    token = as_family.post("/api/shares", json={"scope": "asset", "target_id": clip}
                           ).get_json()["token"]
    answer = app.test_client().get(f"/api/share/{token}/file/{clip}")
    assert answer.status_code == 409, (ext, answer.status_code)
    assert ORIGINAL not in answer.data


@pytest.mark.parametrize("ext", sorted(VIDEO_EXTS))
def test_a_guest_and_a_share_link_get_the_stripped_copy_of_any_video(
        app, people, as_guest, as_family, scanned, monkeypatch, tmp_path, ext):
    from ninaivu.media import stripped_video
    clip = _video(scanned, ext)
    copy = tmp_path / f"copy{ext}"
    copy.write_bytes(b"no metadata in this one")
    monkeypatch.setattr(stripped_video, "stripped_copy", lambda *a, **k: copy)
    answer = as_guest.get(f"/api/file/{clip}")
    assert answer.status_code == 200 and answer.data == copy.read_bytes(), ext
    token = as_family.post("/api/shares", json={"scope": "asset", "target_id": clip}
                           ).get_json()["token"]
    shared = app.test_client().get(f"/api/share/{token}/file/{clip}")
    assert shared.status_code == 200 and shared.data == copy.read_bytes(), ext


def test_every_video_type_is_registered_at_start_up(app):
    for ext in VIDEO_EXTS:
        assert (mimetypes.guess_type(f"x{ext}")[0] or "").startswith("video/"), ext


@needs_ffmpeg
def test_a_3gp_from_a_phone_reaches_a_guest_without_its_place(app, people, as_guest, scanned):
    cfg, conn, _ = scanned
    library = Path(cfg.active_root)
    path = library / "misc" / "phone.3gp"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=64x48:rate=5:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-metadata", "location=+48.8577+002.2950/",
                    str(path)], check=True, capture_output=True)
    assert b"loci" in path.read_bytes()                 # 3GPP's own location box
    clip = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, duration, "
        "visibility) VALUES(?,?,?,?,?,?,?,?,0)",
        (str(library), "misc/phone.3gp", "phone.3gp", "misc", "3gp", "video",
         path.stat().st_size, 1.0)).lastrowid
    conn.commit()
    answer = as_guest.get(f"/api/file/{clip}")
    assert answer.status_code == 200
    assert answer.data and b"loci" not in answer.data


# -- A-02: only a proxy is believed about who is calling -----------------------

def _proxied_app(scanned, monkeypatch, listed: str = ""):
    from ninaivu import build_services, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.trusted_proxies = 1
    monkeypatch.setenv("NINAIVU_TRUSTED_PROXY_ADDRESSES", listed)
    services = build_services(cfg)
    services.scanner.stop()
    return create_home_app(services), cfg, conn


@pytest.fixture(autouse=True)
def _fresh_code(monkeypatch):
    monkeypatch.setattr(auth, "_SETUP_CODE", None)


def test_a_device_on_the_network_cannot_say_it_is_this_computer(scanned, monkeypatch):
    app, _, conn = _proxied_app(scanned, monkeypatch)
    lan = app.test_client()
    lan.environ_base["REMOTE_ADDR"] = "192.168.1.60"
    forged = {"X-Forwarded-For": "127.0.0.1"}
    assert lan.get("/api/auth/state", headers=forged).get_json()["setup_code_required"] is True
    body = {"username": "mallory", "password": "correcthorse1", "name": "M"}
    assert lan.post("/api/auth/setup", json=body, headers=forged).status_code == 403
    assert auth.needs_setup(conn)


def test_a_proxy_on_this_computer_is_still_believed(scanned, monkeypatch):
    app, _, _ = _proxied_app(scanned, monkeypatch)
    here = app.test_client()                          # from 127.0.0.1
    state = here.get("/api/auth/state", headers={"X-Forwarded-For": "127.0.0.1"}).get_json()
    assert state["setup_code_required"] is False
    away = here.get("/api/auth/state", headers={"X-Forwarded-For": "100.101.102.103"}).get_json()
    assert away["setup_code_required"] is True


def test_a_forged_address_does_not_buy_fresh_guesses(scanned, monkeypatch):
    from ninaivu.api import accounts_api
    app, _, conn = _proxied_app(scanned, monkeypatch)
    auth.bootstrap_admin(conn, *ADMIN, "Dad")
    lan = app.test_client()
    lan.environ_base["REMOTE_ADDR"] = "192.168.1.60"
    for i in range(accounts_api._MAX_ATTEMPTS + 2):
        lan.post("/api/auth/login", json={"username": ADMIN[0], "password": "wrong"},
                 headers={"X-Forwarded-For": f"127.0.0.{i + 2}"})
    assert not [k for k in accounts_api._ATTEMPTS if k.startswith("127.")]
    assert len(accounts_api._ATTEMPTS[f"192.168.1.60|{ADMIN[0]}"]) == accounts_api._MAX_ATTEMPTS


def test_a_forwarded_name_from_the_network_is_not_believed(scanned, monkeypatch):
    app, _, _ = _proxied_app(scanned, monkeypatch)
    lan = app.test_client()
    lan.environ_base["REMOTE_ADDR"] = "192.168.1.60"
    # A rebinding page's own name in Host, a household name in X-Forwarded-Host.
    answer = lan.get("/api/auth/profiles", headers={"Host": "attacker.example",
                                                    "X-Forwarded-Host": "ninaivu.local"})
    assert answer.status_code == 421
    # From the proxy on this computer, the forwarded name is the one checked.
    proxied = app.test_client().get("/api/auth/profiles", headers={
        "Host": "localhost", "X-Forwarded-Host": "attacker.example"})
    assert proxied.status_code == 421


def test_a_listed_proxy_is_believed_but_not_as_this_computer(scanned, monkeypatch):
    app, _, _ = _proxied_app(scanned, monkeypatch, listed="172.18.0.0/16")
    from_proxy = app.test_client()
    from_proxy.environ_base["REMOTE_ADDR"] = "172.18.0.5"
    # Its own computer is not this one.
    state = from_proxy.get("/api/auth/state",
                           headers={"X-Forwarded-For": "127.0.0.1"}).get_json()
    assert state["setup_code_required"] is True


def test_trusted_proxy_addresses_are_read_from_the_environment():
    from ninaivu.server import hosts
    networks = hosts.trusted_proxy_networks("172.18.0.0/16, 10.0.0.7, nonsense")
    assert hosts.peer_is_trusted_proxy("172.18.3.4", networks)
    assert hosts.peer_is_trusted_proxy("10.0.0.7", networks)
    assert hosts.peer_is_trusted_proxy("::1", networks)
    assert hosts.peer_is_trusted_proxy("::ffff:127.0.0.1", networks)
    assert not hosts.peer_is_trusted_proxy("192.168.1.60", networks)
    assert not hosts.peer_is_trusted_proxy("", networks)


# -- A-08: a video's place is read, and one without is kept at home -------------

@needs_ffmpeg
def test_a_videos_place_is_read_when_it_is_indexed(tmp_path):
    from ninaivu.media import media
    path = tmp_path / "home.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=64x48:rate=5:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-metadata", "location=+13.0827+080.2707+012.000/",
                    str(path)], check=True, capture_output=True)
    info = media.probe_video(path)
    assert info["gps_lat"] == pytest.approx(13.0827)
    assert info["gps_lon"] == pytest.approx(80.2707)


@pytest.mark.parametrize("raw, expected", [
    ("+48.8577+002.2950+035.000/", (48.8577, 2.295)),
    ("-33.8688+151.2093/", (-33.8688, 151.2093)),
    ("+4851.462+00217.700/", (48.8577, 2.295)),
    ("+485127.72+0021712.0/", (48.8577, 2.2867)),
    ("", None), ("somewhere", None), ("+95.0+010.0/", None),
])
def test_iso_6709_is_read(raw, expected):
    from ninaivu.media import media
    found = media.iso6709(raw)
    if expected is None:
        assert found is None
    else:
        assert found == pytest.approx(expected, abs=1e-3)


def test_a_video_with_no_place_on_record_leaves_without_one_in_home_mode(cfg):
    from ninaivu.utils import location
    cfg.strip_location = "home"
    video = {"kind": "video", "ext": "mp4", "gps_lat": None, "gps_lon": None}
    picture = {"kind": "picture", "ext": "jpg", "gps_lat": None, "gps_lon": None}
    # No home zone drawn: nothing is stripped.
    assert not location.should_strip(cfg, video, is_admin=False)
    cfg.home_lat, cfg.home_lon, cfg.home_radius_m = 13.08, 80.27, 300
    assert location.should_strip(cfg, video, is_admin=False)
    assert not location.should_strip(cfg, video, is_admin=True)
    assert not location.should_strip(cfg, picture, is_admin=False)
    far = dict(video, gps_lat=48.85, gps_lon=2.29)
    assert not location.should_strip(cfg, far, is_admin=False)


# -- A-09: a stripped JPEG ends where its first picture does ---------------------

def _gps_exif(image: Image.Image):
    exif = image.getexif()
    exif[0x8825] = {1: "N", 2: (48.0, 51.0, 27.72), 3: "E", 4: (2.0, 17.0, 42.0)}
    return exif


def test_the_second_frame_of_an_mpo_is_left_out():
    from ninaivu.utils.location import strip_jpeg
    first = Image.new("RGB", (64, 48), (200, 10, 10))
    second = Image.new("RGB", (64, 48), (10, 200, 10))
    buffer = io.BytesIO()
    first.save(buffer, "MPO", save_all=True, append_images=[second], exif=_gps_exif(first))
    data = buffer.getvalue()
    assert data.count(b"\xff\xd8\xff") == 2 and b"MPF\x00" in data

    out = strip_jpeg(data)
    assert out.count(b"\xff\xd8\xff") == 1
    assert b"MPF\x00" not in out
    with Image.open(io.BytesIO(out)) as image:
        image.load()
        assert image.format == "JPEG" and image.size == (64, 48)
        assert image.getpixel((5, 5))[0] > 150
        assert not image.getexif().get_ifd(0x8825)


@pytest.mark.parametrize("progressive", [False, True])
def test_a_motion_photos_video_and_the_iptc_block_are_left_out(progressive):
    from ninaivu.utils.location import strip_jpeg
    image = Image.new("RGB", (64, 48), (30, 60, 200))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=_gps_exif(image), progressive=progressive)
    jpeg = buffer.getvalue()
    iptc = b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x0c\x1c\x02\x5a\x00\x07Chennai"
    jpeg = jpeg[:2] + b"\xff\xed" + struct.pack(">H", len(iptc) + 2) + iptc + jpeg[2:]
    motion = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\xa9xyz+13.0827+080.2707/"
    out = strip_jpeg(jpeg + motion)
    assert b"ftypmp42" not in out and b"+13.0827" not in out
    assert b"Chennai" not in out
    assert out.endswith(b"\xff\xd9")
    with Image.open(io.BytesIO(out)) as stripped:
        stripped.load()
        assert stripped.getpixel((5, 5))[2] > 150


# -- A-10: a guest is not told the town, nor can search by it --------------------

def test_a_guest_is_not_told_or_searched_by_the_town(app, people, as_guest, as_family, scanned):
    _, conn, _ = scanned
    row = conn.execute("SELECT id FROM assets WHERE rel_path LIKE 'shared/%' LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET city='Kanchipuram', country='India', camera='Pixel', "
                 "visibility=0 WHERE id=?", (row["id"],))
    conn.commit()

    seen = as_guest.get(f"/api/asset/{row['id']}")
    assert seen.status_code == 200
    assert "city" not in seen.get_json() and "country" not in seen.get_json()
    assert as_family.get(f"/api/asset/{row['id']}").get_json()["city"] == "Kanchipuram"

    assert as_guest.get("/api/assets?q=Kanchipuram").get_json()["total"] == 0
    assert as_guest.get("/api/assets?q=Pixel").get_json()["total"] == 0
    assert as_family.get("/api/assets?q=Kanchipuram").get_json()["total"] >= 1


def test_a_guest_can_still_search_by_name(app, people, as_guest, scanned):
    _, conn, _ = scanned
    conn.execute("UPDATE assets SET visibility=0 WHERE rel_path LIKE 'shared/%'")
    conn.commit()
    found = as_guest.get("/api/assets?q=beach0").get_json()
    assert found["total"] >= 1


def test_a_guest_search_without_full_text_leaves_the_town_out(scanned):
    from ninaivu.storage import db
    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET city='Kanchipuram' WHERE rel_path LIKE 'shared/%'")
    conn.commit()
    original = db.fts_enabled
    try:
        db.fts_enabled = lambda _conn: False
        _, family = db.query_assets(conn, [cfg.active_root], text="kanchi")
        _, guest = db.query_assets(conn, [cfg.active_root], text="kanchi", guest_search=True)
    finally:
        db.fts_enabled = original
    assert family >= 1 and guest == 0


# -- A-11: what arrives in a request is held to less than the library ------------

def _png_header(width: int, height: int) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def test_a_picture_in_a_request_is_held_to_about_250_megapixels(tmp_path):
    from ninaivu.media import media, safe_image                 # noqa: F401
    real = tmp_path / "phone.png"
    real.write_bytes(_png_header(20000, 10000))                  # 200 MP
    assert safe_image.check_untrusted_file(real) is None
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(_png_header(22000, 22000))                  # 484 MP: under 512
    with pytest.raises(ValueError):
        safe_image.check_untrusted_file(bomb)


def test_a_picture_too_large_for_the_memory_free_is_not_decoded(tmp_path, monkeypatch):
    import psutil

    from ninaivu.media import media
    path = tmp_path / "vast.png"
    path.write_bytes(_png_header(22000, 22000))

    class Little:
        available = 512 * 1024 * 1024

    monkeypatch.setattr(psutil, "virtual_memory", lambda: Little())
    with pytest.raises(OSError, match="memory"):
        media.open_for_index(path, 640)


def test_an_ordinary_picture_is_still_opened_for_indexing(tmp_path, monkeypatch):
    import psutil

    from ninaivu.media import media
    path = tmp_path / "small.png"
    Image.new("RGB", (300, 200), "red").save(path)

    class Little:
        available = 1024

    monkeypatch.setattr(psutil, "virtual_memory", lambda: Little())
    image, size = media.open_for_index(path, 640)
    assert size == (300, 200)
    image.close()


# -- A-12 and A-32: the state folder and what is kept in it ----------------------

def test_session_tokens_are_kept_hashed(app, people, scanned):
    _, conn, _ = scanned
    client = login(app.test_client(), *ADMIN)
    token = client.get_cookie(auth.ADMIN_SESSION_COOKIE) or client.get_cookie(auth.SESSION_COOKIE)
    token = token.value
    stored = [r[0] for r in conn.execute("SELECT token FROM sessions")]
    assert token not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored
    assert client.get("/api/me").get_json()["username"] == ADMIN[0]


def test_a_session_saved_before_hashing_still_signs_in(scanned, people):
    import time
    _, conn, _ = scanned
    raw = "x" * 43
    now = time.time()
    conn.execute("INSERT INTO sessions(token, user_id, created_at, seen_at, expires_at, face, "
                 "active_at) VALUES(?,?,?,?,?,?,?)",
                 (raw, people["family"].id, now, now, now + 3600, "home", now))
    conn.commit()
    auth.init_auth_schema(conn)
    assert conn.execute("SELECT 1 FROM sessions WHERE token=?", (raw,)).fetchone() is None
    user = auth.session_user(conn, raw, "home")
    assert user is not None and user.id == people["family"].id
    auth.end_session(conn, raw)
    assert auth.session_user(conn, raw, "home") is None


def test_a_share_link_still_opens(app, people, as_family):
    asset_id = as_family.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    token = as_family.post("/api/shares", json={"scope": "asset", "target_id": asset_id}
                           ).get_json()["token"]
    assert app.test_client().get(f"/api/share/{token}").status_code == 200


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_the_state_folder_is_for_this_account_only(cfg):
    os.chmod(cfg.state_dir, 0o755)
    cfg.ensure_dirs()
    assert cfg.state_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_the_settings_file_is_never_readable_by_others(cfg, monkeypatch):
    from ninaivu.server import config as config_mod
    seen = []
    real_replace = os.replace

    def replace(src, dst):
        seen.append(os.stat(src).st_mode & 0o777)
        return real_replace(src, dst)

    old = cfg.config_path.with_suffix(".json.tmp")
    old.write_text("{}")
    os.chmod(old, 0o644)
    monkeypatch.setattr(config_mod.os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(config_mod.os, "replace", replace)
    cfg.house_name = "Test house"
    cfg.save()
    assert seen == [0o600]


# -- A-33: lockouts outlast a restart --------------------------------------------

def test_a_profiles_pause_outlasts_a_restart(app, people):
    import time

    from ninaivu.api import accounts_api
    key = f"*|profile:{people['family'].id}"
    with app.test_request_context("/"):
        for _ in range(accounts_api._PROFILE_MAX_ATTEMPTS):
            assert accounts_api.reserve([(key, accounts_api._PROFILE_MAX_ATTEMPTS,
                                          accounts_api._PROFILE_WINDOW)])
        accounts_api.strike_if_spent(key, accounts_api._PROFILE_MAX_ATTEMPTS)
        assert accounts_api.locked_out(key)

    # A restart: everything in memory is gone.
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()
    accounts_api._LOADED.clear()
    with app.test_request_context("/"):
        assert accounts_api.locked_out(key)
        assert accounts_api._LOCKOUTS[key][0] == 1
        accounts_api.clear_lockout(key)
    accounts_api._LOCKOUTS.clear()
    accounts_api._LOADED.clear()
    with app.test_request_context("/"):
        assert not accounts_api.locked_out(key)
    assert time.time()


def test_an_accounts_guesses_outlast_a_restart(app, people):
    from ninaivu.api import accounts_api
    client = app.test_client()
    for _ in range(3):
        client.post("/api/auth/login", json={"username": ADMIN[0], "password": "wrong"})
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOADED.clear()
    client.post("/api/auth/login", json={"username": ADMIN[0], "password": "wrong"})
    assert len(accounts_api._ATTEMPTS[f"*|user:{ADMIN[0]}"]) == 4


# -- A-44: a later report gets retries of its own --------------------------------

def test_a_report_after_a_long_outage_is_retried_again():
    from ninaivu.utils import notify
    scheduled = []

    def down(*args):
        raise OSError("down")

    notifier = notify.Notifier(webhook_url="https://example.invalid/hook", transport=down,
                               later=lambda delay, work: scheduled.append((delay, work)))
    notifier.send("cloud_stalled", "The backup has stalled")
    while scheduled:
        scheduled.pop()[1]()
    # Hours later, the same thing again: tried, and retried, not dropped.
    notifier.send("cloud_stalled", "The backup has stalled")
    assert [delay for delay, _ in scheduled] == [notify.RETRY_DELAYS[0]]


# -- A-45: the portrait analysis reads no more than a picture ----------------------

def test_a_portrait_body_without_a_length_is_held_to_its_limit(app, as_family, monkeypatch):
    from ninaivu.api import api_portrait
    from ninaivu.media import portrait
    monkeypatch.setattr(portrait, "available", lambda: True)
    monkeypatch.setattr(api_portrait, "MAX_BODY_BYTES", 1000)
    answer = as_family.post("/api/portrait/analyse", input_stream=io.BytesIO(b"y" * 5000),
                            content_type="application/octet-stream",
                            environ_overrides={"wsgi.input_terminated": True})
    assert answer.status_code == 413


# -- A-46: the desktop panel's readings ----------------------------------------------

def test_the_panel_reads_what_it_can(tmp_path, monkeypatch):
    psutil = pytest.importorskip("psutil")
    from ninaivu.desktop import control

    class Controller:
        root = tmp_path

        def record(self):
            return None

    def broken(*args, **kwargs):
        raise OSError("not here")

    monkeypatch.setattr(psutil, "virtual_memory", broken)
    monkeypatch.setattr(psutil, "sensors_battery", broken)
    monkeypatch.setattr(psutil, "disk_usage", broken)
    reading = control.Monitor(Controller()).sample()
    assert reading["ram_total"] == 0 and reading["battery"] is None
    assert reading["disk_free"] == 0 and reading["running"] is False


# -- A-56: ffmpeg reads a library file from the disk only ---------------------------

def test_every_library_read_by_ffmpeg_is_local_only(tmp_path, monkeypatch):
    from ninaivu.media import audio_art, media, stripped_video
    from ninaivu.utils import proxies
    commands = []

    class Done:
        returncode = 1
        stdout = b""
        stderr = b""

    def run(command, *args, **kwargs):
        commands.append(list(command))
        return Done()

    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(media, "FFPROBE", "ffprobe")
    monkeypatch.setattr(subprocess, "run", run)
    source = tmp_path / "clip.mp4"
    source.write_bytes(ORIGINAL)
    media.ffprobe_info(source)
    media.extract_video_frame(source)
    audio_art.waveform(source)
    stripped_video.stripped_copy(tmp_path / "state", 1, source)
    commands.append(proxies.live_command(str(source)))
    assert len(commands) == 6, commands
    for command in commands:
        assert "-protocol_whitelist" in command, command
        allowed = command[command.index("-protocol_whitelist") + 1]
        assert allowed == "file", command
        if "-i" in command:
            assert command.index("-protocol_whitelist") < command.index("-i"), command
    piped = proxies.live_command("-")
    assert piped[piped.index("-protocol_whitelist") + 1] == "pipe"
