"""The console's Server page: the desktop control panel, served by Ninaivu itself.

What the page reads (machine and process readings, the resource modes, the
server log a piece at a time) and what it does (restart through a detached
helper, stop) - and that none of it is open to anyone but an admin.
"""
import time

import pytest

from ninaivu.api import server_api


# --- readings ----------------------------------------------------------------

def test_the_page_reads_the_machine_and_the_modes(as_admin):
    client = as_admin
    data = client.get("/api/admin/server").get_json()
    assert {m["id"] for m in data["modes"]} == {"standard", "performance", "power-saving"}
    assert data["mode"] in {"standard", "performance", "power-saving"}
    assert data["pid"] > 0
    metrics = data["metrics"]
    assert 0 <= metrics["cpu"] <= 100
    assert metrics["memory"]["total"] > 0
    assert metrics["process"]["threads"] >= 1
    assert isinstance(data["busy"], list)


def test_ninaivus_own_cpu_is_measured_between_readings(as_admin):
    client = as_admin
    first = client.get("/api/admin/server").get_json()["metrics"]["process"]
    time.sleep(0.05)
    second = client.get("/api/admin/server").get_json()["metrics"]["process"]
    assert second["cpu"] is not None
    assert first["started_at"] == second["started_at"]


@pytest.mark.parametrize("method,url", [
    ("get", "/api/admin/server"),
    ("get", "/api/admin/server/log"),
    ("post", "/api/admin/server/restart"),
    ("post", "/api/admin/server/stop"),
])
def test_only_an_admin_may_use_it(anon, as_family, method, url):
    assert getattr(anon, method)(url).status_code in (401, 403)
    assert getattr(as_family, method)(url).status_code == 403


def test_the_family_app_has_no_server_page(scanned):
    from ninaivu import build_services, create_home_app

    cfg, _, _ = scanned
    cfg.watch = False
    home = create_home_app(build_services(cfg))
    rules = {rule.rule for rule in home.url_map.iter_rules()}
    assert not any(rule.startswith("/api/admin/server") for rule in rules)


# --- the log -------------------------------------------------------------------

@pytest.fixture()
def server_log(tmp_path, monkeypatch):
    path = tmp_path / "server.log"
    monkeypatch.setattr(server_api, "SERVER_LOG", path)
    return path


def test_no_log_says_so(as_admin, server_log):
    client = as_admin
    data = client.get("/api/admin/server/log").get_json()
    assert data["available"] is False


def test_the_log_is_followed_a_piece_at_a_time(as_admin, server_log):
    client = as_admin
    server_log.write_bytes(b"one\n\x1b[32mtwo\x1b[0m\r\nthr")
    first = client.get("/api/admin/server/log").get_json()
    # Colour codes gone, and the unfinished line held back until it ends.
    assert first["text"] == "one\ntwo\n"
    with server_log.open("ab") as fh:
        fh.write(b"ee\nfour\n")
    second = client.get(f"/api/admin/server/log?after={first['position']}").get_json()
    assert second["reset"] is False
    assert second["text"] == "three\nfour\n"


def test_a_long_log_starts_at_a_whole_recent_line(as_admin, server_log, monkeypatch):
    client = as_admin
    monkeypatch.setattr(server_api, "LOG_CHUNK", 64)
    server_log.write_text("".join(f"line {n:04d}\n" for n in range(100)))
    data = client.get("/api/admin/server/log").get_json()
    assert data["truncated"] is True
    assert data["text"].startswith("line ")
    assert data["text"].endswith("line 0099\n")


def test_a_replaced_log_is_read_again_from_its_end(as_admin, server_log):
    client = as_admin
    server_log.write_text("a much longer first log\n" * 3)
    position = client.get("/api/admin/server/log").get_json()["position"]
    server_log.write_text("new\n")
    data = client.get(f"/api/admin/server/log?after={position}").get_json()
    assert data["reset"] is True and data["text"] == "new\n"


# --- restart and stop ----------------------------------------------------------

class FakeHelper:
    def __init__(self):
        self.running = True

    def poll(self):
        return None if self.running else 0


@pytest.fixture()
def helpers(app, monkeypatch):
    spawned = []

    def spawn(mode, network=None):
        spawned.append(mode if network is None else (mode, network))
        return FakeHelper()

    monkeypatch.setattr(server_api, "_spawn_relauncher", spawn)
    monkeypatch.setattr(server_api, "_restart_process", None)
    app.config["MV_STOP_TOKEN"] = "token"
    return spawned


def test_restart_hands_over_to_a_helper_once(as_admin, helpers):
    client = as_admin
    reply = client.post("/api/admin/server/restart", json={"mode": "power-saving"})
    assert reply.status_code == 202, reply.get_json()
    assert helpers == ["power-saving"]
    # A second press while the first is under way does not start another.
    again = client.post("/api/admin/server/restart", json={})
    assert again.status_code == 409
    assert helpers == ["power-saving"]
    assert client.get("/api/admin/server").get_json()["restarting"] is True


def test_restart_refuses_an_unknown_mode(as_admin, helpers):
    client = as_admin
    assert client.post("/api/admin/server/restart", json={"mode": "turbo"}).status_code == 400
    assert helpers == []


def test_a_server_without_a_stop_token_cannot_restart(as_admin, app, helpers):
    client = as_admin
    app.config.pop("MV_STOP_TOKEN")
    assert client.post("/api/admin/server/restart", json={}).status_code == 409
    assert client.get("/api/admin/server").get_json()["can_restart"] is False
    assert helpers == []


def test_stop_asks_the_server_to_stop_properly(as_admin, app):
    client = as_admin
    stopped = []
    app.config["MV_SHUTDOWN"] = lambda: stopped.append(True)
    assert client.post("/api/admin/server/stop", json={}).status_code == 202
    deadline = time.time() + 3
    while not stopped and time.time() < deadline:
        time.sleep(0.05)
    assert stopped == [True]


# --- the helper that restarts ----------------------------------------------------

def test_the_helper_waits_out_a_slow_stop_and_starts_in_the_chosen_mode(monkeypatch):
    from ninaivu.desktop import control, relaunch

    events = []

    class FakeController:
        mode = "performance"

        def __init__(self):
            self.stops = 0

        def stop(self):
            self.stops += 1
            if self.stops == 1:
                raise RuntimeError("Ninaivu is still finishing active work.")
            # The real stop() re-reads the running settings and can put the
            # old mode back.
            self.mode = "performance"
            events.append("stopped")
            return "Ninaivu stopped cleanly."

        def save_mode(self, mode):
            self.mode = mode
            events.append(f"mode {mode}")

        def start(self):
            events.append(f"started {self.mode}")
            return "Ninaivu is running."

    monkeypatch.setattr(control, "Controller", FakeController)
    monkeypatch.setattr(relaunch.time, "sleep", lambda s: None)
    assert relaunch.main(["--mode", "standard"]) == 0
    assert events == ["stopped", "mode standard", "started standard"]


# --- network access ----------------------------------------------------------------

def test_the_page_says_whether_ninaivu_is_on_the_network(as_admin, app):
    cfg = app.config["MV_CONFIG"]
    cfg.host, cfg.admin_host = "0.0.0.0", "0.0.0.0"
    network = as_admin.get("/api/admin/server").get_json()["network"]
    assert network["enabled"] is True
    assert network["family_on_network"] is True and network["console_on_network"] is True

    cfg.host = cfg.admin_host = "127.0.0.1"
    data = as_admin.get("/api/admin/server").get_json()
    assert data["network"]["family_on_network"] is False
    # Local only: the one address that works is this computer's own.
    assert all("localhost" in url for url in data["endpoints"]["family_urls"])
    assert all("localhost" in url for url in data["endpoints"]["admin_urls"])


def test_turning_network_access_off_saves_it_and_restarts_local(as_admin, app, helpers):

    cfg = app.config["MV_CONFIG"]
    reply = as_admin.post("/api/admin/server/network", json={"enabled": False})
    assert reply.status_code == 202, reply.get_json()
    assert reply.get_json()["applied"] is True
    assert helpers == [(None, False)]
    assert cfg.network_access is False
    # Written to config.json, so every launcher's next start honours it.
    import json
    assert json.loads(cfg.config_path.read_text())["network_access"] is False


def test_network_access_is_saved_even_when_it_cannot_restart_from_here(as_admin, app, helpers):
    app.config.pop("MV_STOP_TOKEN")
    reply = as_admin.post("/api/admin/server/network", json={"enabled": False})
    body = reply.get_json()
    assert reply.status_code == 200 and body["saved"] is True and body["applied"] is False
    assert app.config["MV_CONFIG"].network_access is False
    assert helpers == []


def test_network_access_needs_a_plain_yes_or_no(as_admin, helpers):
    assert as_admin.post("/api/admin/server/network", json={"enabled": "off"}).status_code == 400
    assert helpers == []


def test_switched_off_outranks_the_launchers_host():
    """start.py and the desktop panel pass --host 0.0.0.0 on every start."""
    from types import SimpleNamespace
    from ninaivu.__main__ import build_parser, resolve_hosts

    args = build_parser().parse_args(["--host", "0.0.0.0", "--admin-host", "0.0.0.0"])
    cfg = SimpleNamespace(host="0.0.0.0", admin_host=None, network_access=False)
    resolve_hosts(cfg, args)
    assert (cfg.host, cfg.admin_host, args.no_mdns) == ("127.0.0.1", "127.0.0.1", True)

    args = build_parser().parse_args(["--host", "0.0.0.0"])
    cfg = SimpleNamespace(host="127.0.0.1", admin_host=None, network_access=True)
    resolve_hosts(cfg, args)
    assert (cfg.host, cfg.admin_host, args.no_mdns) == ("0.0.0.0", "127.0.0.1", False), \
        "the console stays on this computer unless the household opens it"

    args = build_parser().parse_args(["--host", "0.0.0.0"])
    cfg = SimpleNamespace(host="127.0.0.1", admin_host=None, network_access=True,
                          console_on_network=True)
    resolve_hosts(cfg, args)
    assert cfg.admin_host == "0.0.0.0", "opened on the Server page"


def test_switched_on_still_respects_a_local_only_start():
    from types import SimpleNamespace
    from ninaivu.__main__ import build_parser, resolve_hosts

    args = build_parser().parse_args(["--local-only"])
    cfg = SimpleNamespace(host="0.0.0.0", admin_host=None, network_access=True)
    resolve_hosts(cfg, args)
    assert cfg.host == "127.0.0.1"


def test_the_setting_survives_a_save_and_load(tmp_path):
    from ninaivu.server.config import Config

    cfg = Config()
    cfg.state_dir = tmp_path
    cfg.network_access = False
    cfg.save()
    loaded = Config()
    loaded.state_dir = tmp_path
    import json
    for key, value in json.loads(loaded.config_path.read_text()).items():
        setattr(loaded, key, value)
    assert loaded.network_access is False


def test_the_restart_rewrites_only_the_family_apps_binding():
    from ninaivu.desktop.relaunch import with_network

    running = ["--host", "0.0.0.0", "--port", "443", "--admin-port", "3000",
               "--admin-host", "127.0.0.1", "--https", "--workers", "12"]
    off = with_network(running, False)
    assert "--local-only" in off and "0.0.0.0" not in off
    # A console deliberately kept to this machine stays that way.
    assert off[off.index("--admin-host") + 1] == "127.0.0.1"
    assert off[off.index("--port") + 1] == "443" and "--https" in off

    on = with_network(off, True)
    assert "--local-only" not in on
    assert on[on.index("--host") + 1] == "0.0.0.0"
    assert with_network(["--host=127.0.0.1", "--https"], True) == ["--https", "--host", "0.0.0.0"]


def test_the_helper_applies_the_network_choice_to_what_it_starts(monkeypatch):
    from ninaivu.desktop import control, relaunch

    started = {}

    class FakeController:
        mode = "standard"

        def __init__(self):
            self.settings = {"arguments": ["--host", "0.0.0.0", "--https"]}

        def stop(self):
            return "Ninaivu stopped cleanly."

        def save_mode(self, mode):
            self.mode = mode

        def start(self):
            started["arguments"] = list(self.settings["arguments"])
            return "Ninaivu is running."

    monkeypatch.setattr(control, "Controller", FakeController)
    monkeypatch.setattr(relaunch.time, "sleep", lambda s: None)
    assert relaunch.main(["--network", "off"]) == 0
    assert started["arguments"] == ["--https", "--local-only"]


def test_the_tailnet_addresses_are_listed_once_ninaivu_has_its_certificate(as_admin, app):
    """So the household can be given the address that works away from home."""
    import json

    from ninaivu.utils import tls

    cfg = app.config["MV_CONFIG"]
    cfg.host, cfg.admin_host = "0.0.0.0", "0.0.0.0"
    app.config["MV_SCHEME"] = "https"
    data = as_admin.get("/api/admin/server").get_json()["endpoints"]
    assert data["tailnet_family_urls"] == [] and data["tailnet_admin_urls"] == []

    folder = tls.tls_dir(cfg.state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tailnet.crt").write_text("-----BEGIN CERTIFICATE-----\n")
    (folder / "tailnet.json").write_text(json.dumps({"name": "box.tail1.ts.net"}))
    data = as_admin.get("/api/admin/server").get_json()["endpoints"]
    family = "https://box.tail1.ts.net" + ("" if cfg.port == 443 else f":{cfg.port}")
    assert data["tailnet_family_urls"] == [family]
    assert data["tailnet_admin_urls"] == [f"https://box.tail1.ts.net:{cfg.admin_port}"]
