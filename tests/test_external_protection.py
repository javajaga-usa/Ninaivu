"""What the internet can and cannot reach (7 October 2026 review).

An attacker's view rather than a household's: a router forwarding a port, a
tunnel Ninaivu was not told about, Tailscale Funnel, plain HTTP across the
internet, the console from outside, and guessing the administrator's password
for days at a steady pace.
"""

from __future__ import annotations

import pytest

from conftest import ADMIN, login

PUBLIC = "93.184.216.34"


def _tap_profile(people):
    conn = people["conn"]
    conn.execute("UPDATE users SET password=NULL, pin=NULL WHERE id=?", (people["family"].id,))
    conn.commit()
    return people["family"].id


# -- Who counts as the internet -----------------------------------------------

class _Request:
    def __init__(self, addr, headers=None, secure=False, orig=None):
        self.remote_addr = addr
        self.headers = headers or {}
        self.is_secure = secure
        self.environ = {"werkzeug.proxy_fix.orig": orig} if orig else {}


class _Cfg:
    remote_access = "auto"
    trusted_proxies = 0


OWN = frozenset({"127.0.0.1", "::1", "192.168.1.5", "2a02:1234:5678:9abc::5"})


@pytest.mark.parametrize("addr, headers, expected", [
    ("192.168.1.20", {}, False),                                    # the Wi-Fi
    ("100.101.102.103", {}, False),                                 # Tailscale
    ("fd7a:115c:a1e0::1", {}, False),                               # Tailscale IPv6
    ("10.8.0.2", {}, False),                                        # WireGuard
    ("2a02:1234:5678:9abc::77", {}, False),                         # a phone at home, IPv6
    (PUBLIC, {}, True),                                             # a forwarded port
    ("2a03:2880:f12f::1", {}, True),                                # IPv6 from elsewhere
    ("127.0.0.1", {"CF-Connecting-IP": PUBLIC}, True),              # a tunnel nobody named
    ("127.0.0.1", {"X-Forwarded-For": PUBLIC}, True),               # a proxy nobody named
    ("127.0.0.1", {"Tailscale-Funnel-Request": "?1",
                   "X-Forwarded-For": PUBLIC}, True),               # Funnel
    ("127.0.0.1", {"X-Forwarded-For": "100.64.1.2",
                   "Tailscale-User-Login": "amma@example.org"}, False),  # Tailscale Serve
    ("127.0.0.1", {}, False),                                       # this computer
])
def test_the_internet_is_known_whatever_remote_access_says(addr, headers, expected):
    from ninaivu.server import remote
    assert remote.from_the_internet(_Cfg(), _Request(addr, headers), own=OWN) is expected


def test_a_tailnet_identity_header_from_the_internet_is_not_believed():
    from ninaivu.server import remote
    forged = _Request(PUBLIC, {"Tailscale-User-Login": "amma@example.org"})
    assert remote.from_the_internet(_Cfg(), forged, own=OWN)


def test_plain_http_is_judged_by_the_connection_not_the_forwarded_address():
    from ninaivu.server import remote
    assert remote.plain_http_from_internet(_Request(PUBLIC), own=OWN)
    assert not remote.plain_http_from_internet(_Request(PUBLIC, secure=True), own=OWN)
    assert not remote.plain_http_from_internet(_Request("192.168.1.20"), own=OWN)
    # Behind a trusted proxy on the home network: the proxy is the peer.
    behind = _Request(PUBLIC, orig={"REMOTE_ADDR": "192.168.1.2"})
    assert not remote.plain_http_from_internet(behind, own=OWN)


# -- The family app -------------------------------------------------------------

def test_a_tap_profile_does_not_open_through_a_forwarded_port(app, cfg, people):
    family = _tap_profile(people)
    assert cfg.remote_access == "auto"
    browser = app.test_client()
    outside = browser.post("/api/auth/enter", json={"id": family},
                           base_url="https://photos.example.org",
                           environ_base={"REMOTE_ADDR": PUBLIC})
    assert outside.status_code in (403, 421)
    cfg.allowed_hosts = ["photos.example.org"]
    outside = browser.post("/api/auth/enter", json={"id": family},
                           base_url="https://photos.example.org",
                           environ_base={"REMOTE_ADDR": PUBLIC})
    assert outside.status_code == 403
    assert "only at home" in outside.get_json()["error"]
    assert browser.post("/api/auth/enter", json={"id": family},
                        environ_base={"REMOTE_ADDR": "192.168.1.20"}).status_code == 200


def test_a_tap_profile_does_not_open_through_an_unnamed_tunnel(app, cfg, people):
    family = _tap_profile(people)
    answer = app.test_client().post("/api/auth/enter", json={"id": family},
                                    headers={"CF-Connecting-IP": PUBLIC})
    assert answer.status_code == 403


def test_browsing_without_signing_in_is_closed_to_a_forwarded_port(app, cfg, people):
    cfg.open_browsing = True
    cfg.allowed_hosts = ["photos.example.org"]
    browser = app.test_client()
    outside = browser.get("/api/assets", base_url="https://photos.example.org",
                          environ_base={"REMOTE_ADDR": PUBLIC})
    assert outside.status_code == 401
    assert browser.get("/api/assets", environ_base={"REMOTE_ADDR": "192.168.1.20"}
                       ).status_code == 200


def test_plain_http_from_the_internet_is_not_answered(app, people):
    browser = app.test_client()
    answer = browser.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
                          environ_base={"REMOTE_ADDR": PUBLIC})
    assert answer.status_code == 403
    assert "plain HTTP" in answer.get_json()["error"]
    assert "Set-Cookie" not in answer.headers


# -- The console ------------------------------------------------------------------

@pytest.fixture()
def console(scanned, people):
    from ninaivu import build_services, create_admin_app
    cfg, _, _ = scanned
    services = build_services(cfg)
    services.scanner.stop()
    return cfg, create_admin_app(services)


def test_the_console_does_not_open_from_the_internet(console):
    cfg, app = console
    browser = app.test_client()
    tunnel = browser.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
                          headers={"CF-Connecting-IP": PUBLIC})
    assert tunnel.status_code == 403
    assert "console_from_internet" in tunnel.get_json()["error"]
    # At home it signs in as ever, and over Tailscale too.
    login(app.test_client(), *ADMIN)
    tailnet = app.test_client().post(
        "/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
        environ_base={"REMOTE_ADDR": "100.101.102.103"})
    assert tailnet.status_code == 200


def test_the_household_can_open_the_console_to_a_tunnel_on_purpose(console):
    cfg, app = console
    cfg.console_from_internet = True
    answer = app.test_client().post(
        "/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
        headers={"CF-Connecting-IP": PUBLIC})
    assert answer.status_code == 200


# -- Guessing the administrator's password ----------------------------------------

def test_guessing_the_admin_password_is_paused_for_longer_each_time(app, people, monkeypatch):
    from ninaivu.api import accounts_api
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()
    client = app.test_client()
    everywhere = f"*|user:{ADMIN[0]}"
    codes = []
    for attempt in range(accounts_api._PROFILE_MAX_ATTEMPTS + 2):
        address = f"192.168.7.{attempt // 4 + 10}"
        codes.append(client.post("/api/auth/login",
                                 json={"username": ADMIN[0], "password": "wrong"},
                                 environ_base={"REMOTE_ADDR": address}).status_code)
    assert codes[-1] == 429
    assert accounts_api.locked_out(everywhere)
    strikes, until = accounts_api._LOCKOUTS[everywhere]
    assert strikes == 1

    # The window passing is not enough: the pause outlasts it.
    import time as _time
    real = _time.time
    monkeypatch.setattr(accounts_api.time, "time",
                        lambda: real() + accounts_api._PROFILE_WINDOW - 60)
    refused = client.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
                          environ_base={"REMOTE_ADDR": "192.168.7.99"})
    assert refused.status_code == 429
    monkeypatch.setattr(accounts_api.time, "time", real)

    # The administrator at the computer Ninaivu runs on still gets in, and a
    # right password does not clear the pause for everyone else from there.
    login(app.test_client(), *ADMIN)
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()


def test_made_up_names_are_paused_like_real_ones_and_leave_nothing_behind_for_ever(
        app, people, monkeypatch):
    """A made-up name used to leave no pause behind, so that the table did not
    fill with names anybody on the internet cared to invent. But a pause that
    only real names got told a guesser which names were real (the 10 October
    2026 audit, L6): now both are paused alike, and what keeps the table from
    growing without limit is that a pause is forgotten — in memory and in the
    index — once it has been over for the longest pause there is."""
    from ninaivu.api import accounts_api
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()
    client = app.test_client()
    everywhere = "*|user:nobody-here"
    codes = []
    for attempt in range(accounts_api._PROFILE_MAX_ATTEMPTS + 2):
        codes.append(client.post("/api/auth/login",
                                 json={"username": "nobody-here", "password": "x"},
                                 environ_base={"REMOTE_ADDR": f"192.168.8.{attempt // 4 + 10}"}
                                 ).status_code)
    # The same shape as the administrator's name gets, one test above.
    assert codes[-1] == 429
    assert accounts_api.locked_out(everywhere)
    strikes, until = accounts_api._LOCKOUTS[everywhere]
    assert strikes == 1
    conn = people["conn"]
    assert conn.execute("SELECT strikes FROM auth_limits WHERE key=?",
                        (everywhere,)).fetchone()[0] == 1

    # Once the pause has been over for the longest pause there is, the next
    # sweep forgets it and the next save of any account-wide key prunes the
    # row: nothing is kept for ever for a name that is nobody's.
    import time as _time
    real = _time.time
    monkeypatch.setattr(accounts_api.time, "time",
                        lambda: until + accounts_api._LONGEST_PAUSE + 1)
    accounts_api._last_sweep = 0.0
    assert not accounts_api.rate_limited("anything")
    assert everywhere not in accounts_api._LOCKOUTS
    with app.test_request_context("/"):
        accounts_api._ATTEMPTS["*|user:other"] = [accounts_api.time.time()]
        accounts_api._save("*|user:other")
    assert conn.execute("SELECT 1 FROM auth_limits WHERE key=?",
                        (everywhere,)).fetchone() is None
    monkeypatch.setattr(accounts_api.time, "time", real)
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()


# -- Smaller things -----------------------------------------------------------------

def test_signing_out_empties_the_browsers_cache(as_family):
    answer = as_family.post("/api/auth/logout")
    assert answer.headers.get("Clear-Site-Data") == '"cache"'


@pytest.mark.parametrize("content", [
    b"#EXTM3U\n#EXTINF:1,\nfile:///etc/hosts.mp4\n",
    b"\xef\xbb\xbfffconcat version 1.0\nfile ../Private/x.mp4\n",
    b"  #EXT-X-VERSION:3\n",
])
def test_a_playlist_dressed_as_a_video_is_refused(as_family, content):
    import io
    answer = as_family.post("/api/upload", data={"file": (io.BytesIO(content), "IMG_1.mp4")})
    body = answer.get_json()
    assert not body.get("uploaded"), body
    assert "not recognised" in body["errors"][0]["error"]


def test_an_upload_is_refused_when_the_disk_is_nearly_full(as_family, monkeypatch):
    import io
    from collections import namedtuple
    from PIL import Image
    from ninaivu.media import upload_review
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(upload_review.shutil, "disk_usage",
                        lambda _path: usage(10 ** 12, 10 ** 12, 1024 ** 3))
    picture = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(picture, "PNG")
    picture.seek(0)
    body = as_family.post("/api/upload", data={"file": (picture, "a.png")}).get_json()
    assert not body.get("uploaded")
    assert "free space" in body["errors"][0]["error"]
