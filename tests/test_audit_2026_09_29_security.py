"""Regressions for the security findings of the 29 September 2026 product audit.

Each test is the attack the audit demonstrated, run against the fix.
"""
import time

import pytest

from conftest import ADMIN, login
from ninaivu.server import auth, hosts, settings_groups
from ninaivu.server.config import Config


# --- 1. DNS rebinding: the Host header decides whether Ninaivu answers --------

REBIND = {"Host": "attacker.example", "Origin": "http://attacker.example",
          "Sec-Fetch-Site": "same-origin"}


def test_a_rebinding_page_gets_nothing(app, people):
    """A website that points its own domain at this computer used to list the
    profiles, walk into a tap-to-enter one and read the family's originals."""
    conn = people["conn"] if isinstance(people, dict) and "conn" in people else None
    if conn is not None:
        auth.create_user(conn, "kid", "", display_name="Kid", role=auth.ROLE_FAMILY)
    client = app.test_client()
    assert client.get("/api/auth/profiles", headers=REBIND).status_code == 421
    assert client.post("/api/auth/enter", json={"id": 1}, headers=REBIND).status_code == 421
    assert client.get("/api/assets?limit=5", headers=REBIND).status_code == 421


@pytest.mark.parametrize("host", [
    "localhost", "localhost:5000", "127.0.0.1", "[::1]:80", "192.168.1.20", "ninaivu",
    "ninaivu.local", "ninaivu-admin.local:443", "nas.lan", "box.home.arpa",
    "ninaivu.tail1234.ts.net",
])
def test_the_names_a_home_uses_are_answered(host):
    assert hosts.allowed(host, Config())


@pytest.mark.parametrize("host", ["attacker.example", "evil.com:5000", "ninaivu.local.evil.com",
                                  "photos.example.org"])
def test_other_names_are_not(host):
    assert not hosts.allowed(host, Config())


def test_a_name_the_household_set_up_is_answered():
    cfg = Config()
    cfg.remote_hostname = "photos.example.org"
    cfg.allowed_hosts = ["*.family.example"]
    assert hosts.allowed("photos.example.org", cfg)
    assert hosts.allowed("gallery.family.example", cfg)
    assert not hosts.allowed("family.example.evil", cfg)


def test_the_forwarded_name_counts_only_behind_a_trusted_proxy():
    cfg = Config()
    headers = {"X-Forwarded-Host": "attacker.example"}
    assert hosts.checked_host(headers, "localhost", cfg) == "localhost"
    cfg.trusted_proxies = 1
    assert hosts.checked_host(headers, "localhost", cfg) == "attacker.example"


def test_the_ordinary_host_still_works(app, people):
    client = app.test_client()
    assert client.get("/api/auth/profiles").status_code == 200


# --- 2. The console on the family port ----------------------------------------

def test_docker_does_not_route_the_console_onto_the_family_port():
    from pathlib import Path
    dockerfile = (Path(__file__).resolve().parents[1] / "installers" / "docker" / "Dockerfile").read_text()
    assert '"--no-mdns"' in dockerfile


def test_the_nginx_example_never_forwards_the_clients_name_to_the_family_app():
    from pathlib import Path
    conf = (Path(__file__).resolve().parents[1] / "installers" / "nginx" / "ninaivu.conf").read_text()
    family = conf[conf.index("# --- Family App Server Block ---"):conf.index("server_name admin.")]
    assert "proxy_set_header Host $host;" not in family
    assert "default_server" in conf


# --- 3. settings/all takes only what the dedicated pages would -----------------

@pytest.mark.parametrize("change", [
    {"roots": ["/", "/etc"]},
    {"active_root": ["a", "b"]},
    {"lock_roots": False},
    {"network_access": True},
    {"extensions": ["anything"]},
    {"tag_threshold": "nan"},
    {"tag_threshold": 7},
    {"thumb_format": None},
    {"thumb_sizes": []},
    {"page_size": -5},
    {"workers": 0},
    {"video_keyframes": 99},
    {"ai_engine": "turbo"},
    {"hardware_tier": "huge"},
    {"remote_access": "carrier-pigeon"},
    {"remote_networks": ["not-a-network"]},
    {"house_name": ["a"]},
    {"watch": "maybe"},
    {"workers": 3.9},
    {"clip_pretrained": "/tmp/evil.pt"},
    {"notify_webhook": "file:///etc/passwd"},
    {"cloud_window_start": "25:99"},
    {"allowed_hosts": ["not a host"]},
])
def test_a_value_the_dedicated_page_would_refuse_is_refused(change):
    cfg = Config()
    before = {name: getattr(cfg, name) for name in change}
    with pytest.raises(settings_groups.BadValue):
        settings_groups.apply(cfg, change)
    assert {name: getattr(cfg, name) for name in change} == before


def test_ordinary_values_still_go_through():
    cfg = Config()
    changed = settings_groups.apply(cfg, {
        "thumb_quality": "70", "watch": "off", "ai_engine": "light", "thumb_format": "jpeg",
        "remote_networks": "10.8.0.0/24\n10.9.0.0/24", "house_name": "  The Kumar Home ",
        "allowed_hosts": ["photos.example.org"], "cloud_window_start": "23:00",
    })
    assert cfg.thumb_quality == 70 and cfg.watch is False and cfg.thumb_format == "JPEG"
    assert cfg.remote_networks == ["10.8.0.0/24", "10.9.0.0/24"], "one per line, not split on commas"
    assert cfg.house_name == "The Kumar Home"
    assert set(changed) >= {"thumb_quality", "watch", "ai_engine", "remote_networks"}


def test_a_folder_name_with_a_comma_stays_one_folder():
    cfg = Config()
    settings_groups.apply(cfg, {"ignore_dirs": "Smith, John\nold"})
    assert set(cfg.ignore_dirs) >= {"Smith, John", "old"}


def test_the_page_says_where_the_managed_ones_are_changed():
    described = settings_groups.describe(Config())
    items = {s["name"]: s for g in described["groups"] for s in g["settings"]}
    assert items["roots"]["managed_by"] == "Library settings"
    assert items["lock_roots"]["managed_by"]
    assert items["ai_engine"]["choices"] == ["auto", "clip", "light", "off"]
    assert "roots" not in described["first_screen"]


def test_the_endpoint_refuses_them_too(scanned):
    from ninaivu import build_services, create_admin_app
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    admin = login(create_admin_app(services).test_client(), *ADMIN)
    response = admin.post("/api/admin/settings/all", json={"settings": {"roots": ["/"]}})
    assert response.status_code == 400 and "Library settings" in response.get_json()["error"]
    body = admin.get("/api/admin/settings/all")
    assert body.status_code == 200 and b"NaN" not in body.data


# --- 4. Sign-in limits hold across addresses and under concurrency -------------

def test_rotating_addresses_does_not_reset_the_limit_on_an_account(app, people):
    client = app.test_client()
    refused = 0
    for attempt in range(40):
        address = f"2001:db8::{attempt // 4 + 1:x}"
        response = client.post("/api/auth/login", json={"username": ADMIN[0], "password": "wrong"},
                               environ_base={"REMOTE_ADDR": address})
        refused += response.status_code == 429
    assert refused >= 15, "the account's own ceiling was never reached"


def test_concurrent_guesses_cannot_overshoot_the_limit():
    import threading
    from ninaivu.api import accounts_api
    accounts_api._ATTEMPTS.clear()
    key = "*|user:race-test"
    admitted = []

    def guess():
        admitted.append(accounts_api.reserve([(key, 20, 1800.0)]))
    threads = [threading.Thread(target=guess) for _ in range(64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert admitted.count(True) == 20
    accounts_api._ATTEMPTS.clear()


# --- 5. The first administrator ------------------------------------------------

def test_the_first_administrator_needs_the_code_from_another_device(scanned):
    from ninaivu import build_services, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    client = create_home_app(services).test_client()
    far = {"REMOTE_ADDR": "192.168.1.50"}
    state = client.get("/api/auth/state", environ_base=far).get_json()
    assert state["setup_required"] and state["setup_code_required"]
    body = {"username": "mallory", "password": "correcthorse1", "name": "M"}
    assert client.post("/api/auth/setup", json=body, environ_base=far).status_code == 403
    assert auth.needs_setup(conn)
    body["setup_code"] = auth.setup_code().lower()
    assert client.post("/api/auth/setup", json=body, environ_base=far).status_code == 200


def test_on_this_computer_no_code_is_asked(scanned):
    from ninaivu import build_services, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    client = create_home_app(services).test_client()
    assert client.get("/api/auth/state").get_json()["setup_code_required"] is False
    response = client.post("/api/auth/setup",
                           json={"username": "dad", "password": "correcthorse1", "name": "Dad"})
    assert response.status_code == 200


# --- 6. Smaller ones ---------------------------------------------------------------

def test_a_webhook_is_only_ever_http():
    from ninaivu.utils.notify import Notifier
    notifier = Notifier(webhook_url="file:///etc/passwd")
    assert "http" in notifier._post("t", "d", "test")


def test_mail_is_sent_over_a_verified_connection():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "ninaivu" / "utils"
    for name in ("notify.py", "digest.py"):
        text = (root / name).read_text()
        assert "starttls(context=ssl.create_default_context())" in text
        assert "server.starttls()\n" not in text


def test_the_ai_server_must_be_on_the_home_network():
    pytest.importorskip("ninaivu_studio")
    from ninaivu_studio.ai_server.comfyui import network_scope
    assert network_scope("http://8.8.8.8:8188") == "public"
    assert network_scope("http://100.101.102.103:8188") == "home", "Tailscale addresses are home"
    assert network_scope("http://192.168.1.9:8188") == "home"


# --- 7. The product audit, same day -----------------------------------------------

def test_the_console_port_asks_for_the_code_from_another_device_too(scanned):
    """A new install used to let anybody at home make themselves administrator
    through the console port: the console counted as local by itself."""
    from ninaivu import build_services, create_admin_app
    cfg, conn, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    client = create_admin_app(services).test_client()
    far = {"REMOTE_ADDR": "192.168.1.77"}
    body = {"username": "mallory", "password": "correcthorse1", "name": "M"}
    assert client.get("/api/auth/state", environ_base=far).get_json()["setup_code_required"]
    assert client.post("/api/auth/setup", json=body, environ_base=far).status_code == 403
    assert auth.needs_setup(conn)
    body["setup_code"] = auth.setup_code()
    assert client.post("/api/auth/setup", json=body, environ_base=far).status_code == 200


def test_the_setup_code_is_long_and_guesses_are_limited(scanned):
    from ninaivu import build_services, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    assert len(auth.setup_code()) == 10
    client = create_home_app(services).test_client()
    far = {"REMOTE_ADDR": "192.168.1.78"}
    body = {"username": "mallory", "password": "correcthorse1", "name": "M", "setup_code": "0000000000"}
    codes = [client.post("/api/auth/setup", json=body, environ_base=far).status_code for _ in range(12)]
    assert codes[:10] == [403] * 10 and codes[10:] == [429, 429]


def test_the_console_listens_on_this_computer_unless_opened(tmp_path):
    from ninaivu.__main__ import build_parser, resolve_hosts
    from ninaivu.server.config import Config
    cfg = Config()
    resolve_hosts(cfg, build_parser().parse_args(["--host", "0.0.0.0"]))
    assert cfg.admin_host == "127.0.0.1"


def test_a_pin_that_keeps_being_wrong_pauses_for_longer_each_time():
    from ninaivu.api import accounts_api
    key = "*|profile:99"
    accounts_api._ATTEMPTS[key] = [time.time()] * accounts_api._PROFILE_MAX_ATTEMPTS
    accounts_api.strike_if_spent(key, accounts_api._PROFILE_MAX_ATTEMPTS)
    first = accounts_api._LOCKOUTS[key][1] - time.time()
    accounts_api._ATTEMPTS[key] = [time.time()] * accounts_api._PROFILE_MAX_ATTEMPTS
    accounts_api.strike_if_spent(key, accounts_api._PROFILE_MAX_ATTEMPTS)
    second = accounts_api._LOCKOUTS[key][1] - time.time()
    assert accounts_api.locked_out(key)
    assert second > first * 1.8, "the second pause is twice the first"
    accounts_api.clear_lockout(key)
    assert not accounts_api.locked_out(key)
