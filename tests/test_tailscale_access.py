"""Checking Ninaivu on the tailnet, and never putting it on the internet.

Tailscale is not driven here: a stand-in answers the commands the tool gives,
so what is checked is what the tool asks Tailscale to do, and what it refuses
to do.
"""
import json
import subprocess

from tools import tailscale_access as ts

NAME = "jagas-mac-mini.tail07dcbf.ts.net"
PORTS = {"port": 443, "admin_port": 3000, "scheme": "https"}

#: What an earlier version of the tool left behind, as `serve status --json`
#: prints it (Tailscale 1.102).
OLD_SERVE = {
    "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
    "Web": {
        f"{NAME}:443": {"Handlers": {"/": {"Proxy": "https+insecure://127.0.0.1:443"}}},
        f"{NAME}:8443": {"Handlers": {"/": {"Proxy": "https+insecure://127.0.0.1:3000"}}},
    },
}


class Tailscale:
    """Records every command; answers as a tailnet would."""

    def __init__(self, *, https=True, running=True, served=None):
        self.commands = []
        self.https, self.running = https, running
        self.served = served or {}

    def __call__(self, command):
        self.commands.append(command[1:])
        if command[1:3] == ["status", "--json"]:
            body = {"BackendState": "Running" if self.running else "Stopped",
                    "Self": {"DNSName": NAME + ".", "TailscaleIPs": ["100.115.249.50"]},
                    "CertDomains": [NAME] if self.https else None}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
        if command[1:4] == ["serve", "status", "--json"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.served), "")
        return subprocess.CompletedProcess(command, 0, "No serve config", "")


def net(tailscale):
    return ts.tailnet("tailscale", tailscale)


def test_the_addresses_follow_ninaivus_ports():
    assert ts.addresses(NAME, PORTS) == [
        ("family app", f"https://{NAME}"), ("admin console", f"https://{NAME}:3000")]
    moved = {"port": 444, "admin_port": 0, "scheme": "https"}
    assert ts.addresses(NAME, moved) == [("family app", f"https://{NAME}:444")]


def test_the_tailnet_is_read_from_status():
    assert net(Tailscale()) == {"running": True, "name": NAME, "https": True,
                                "address": "100.115.249.50"}
    assert net(Tailscale(https=False))["https"] is False


def test_enable_checks_both_addresses_and_never_funnels_or_serves():
    tailscale = Tailscale()
    checked = []
    code = ts.enable("tailscale", PORTS, net(tailscale), runner=tailscale,
                     check=lambda address: checked.append(address) or "200")
    assert code == 0
    assert checked == [f"https://{NAME}", f"https://{NAME}:3000"]
    assert not any("funnel" in part for c in tailscale.commands for part in c)
    assert not any(c[:2] == ["serve", "--bg"] for c in tailscale.commands)


def test_it_takes_off_the_serve_entries_an_earlier_version_made():
    tailscale = Tailscale(served=OLD_SERVE)
    ts.enable("tailscale", PORTS, net(tailscale), runner=tailscale, check=lambda _a: "200")
    assert ["serve", "--https=443", "off"] in tailscale.commands
    assert ["serve", "--https=8443", "off"] in tailscale.commands


def test_somebody_elses_serve_entry_is_left_alone():
    theirs = {"TCP": {"8443": {"HTTPS": True}},
              "Web": {f"{NAME}:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8123"}}}}}
    tailscale = Tailscale(served=theirs)
    ts.enable("tailscale", PORTS, net(tailscale), runner=tailscale, check=lambda _a: "200")
    assert not any(c[-1:] == ["off"] for c in tailscale.commands)


def test_a_wrong_certificate_says_to_restart_ninaivu(capsys):
    tailscale = Tailscale()
    code = ts.enable("tailscale", PORTS, net(tailscale), runner=tailscale,
                     check=lambda _a: "no (CERTIFICATE_VERIFY_FAILED)")
    assert code == 1 and "Restart Ninaivu" in capsys.readouterr().out


def test_without_https_certificates_nothing_is_changed(capsys):
    tailscale = Tailscale(https=False, served=OLD_SERVE)
    code = ts.enable("tailscale", PORTS, net(tailscale), runner=tailscale, check=lambda _a: "200")
    assert code == 2
    assert not any(c[0] == "serve" for c in tailscale.commands)
    assert ts.HTTPS_SETTING in capsys.readouterr().out


def test_not_signed_in_changes_nothing():
    tailscale = Tailscale(running=False, served=OLD_SERVE)
    assert ts.enable("tailscale", PORTS, net(tailscale), runner=tailscale,
                     check=lambda _a: "200") == 2
    assert not any(c[0] == "serve" for c in tailscale.commands)


def test_the_cli_is_found_inside_the_mac_app():
    app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    assert ts.find_cli("darwin", which=lambda _n: None, exists=lambda p: p == app) == app
    assert ts.find_cli("darwin", which=lambda _n: "/usr/bin/tailscale") == "/usr/bin/tailscale"
    assert ts.find_cli("linux", which=lambda _n: None, exists=lambda _p: True) is None


def test_ninaivus_ports_come_from_its_run_file(tmp_path):
    (tmp_path / "ninaivu.run").write_text(json.dumps(
        {"pid": 1, "port": 443, "admin_port": 3000, "scheme": "https", "token": "t"}))
    assert ts.ninaivu_ports(tmp_path) == PORTS
    assert ts.ninaivu_ports(tmp_path / "nowhere") is None


def test_a_request_through_a_proxy_is_not_taken_for_this_computer():
    """Serve, or any proxy on this computer, reaches Ninaivu from 127.0.0.1 and
    adds X-Forwarded-For (as Serve does, measured on Tailscale 1.102). Ninaivu
    must treat that as somebody else, with trusted_proxies left at 0."""
    import flask

    from ninaivu.api import admin_api

    app = flask.Flask(__name__)
    cfg = type("Cfg", (), {"trusted_proxies": 0})()
    headers = {"X-Forwarded-For": "100.93.190.76", "X-Forwarded-Host": NAME,
               "Tailscale-User-Login": "javajaga.usa@gmail.com", "Host": NAME}
    with app.test_request_context("/", headers=headers,
                                  environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        original = admin_api._cfg
        admin_api._cfg = lambda: cfg
        try:
            assert admin_api.from_this_machine() is False
            assert admin_api.from_this_computer() is False
        finally:
            admin_api._cfg = original


def test_a_phone_on_the_tailnet_is_not_this_computer():
    """Directly on the tailnet, the address is the phone's own."""
    import flask

    from ninaivu.api import admin_api

    app = flask.Flask(__name__)
    cfg = type("Cfg", (), {"trusted_proxies": 0})()
    with app.test_request_context("/", headers={"Host": NAME},
                                  environ_base={"REMOTE_ADDR": "100.93.190.76"}):
        original = admin_api._cfg
        admin_api._cfg = lambda: cfg
        try:
            assert admin_api.from_this_machine() is False
            assert admin_api.from_this_computer() is False
        finally:
            admin_api._cfg = original
