"""HTTP by default; HTTPS only when the administrator chooses it.

The choice is the console's Use HTTPS switch, saved in config.json and
honoured at every start. An install that served HTTPS before keeps it until
somebody switches it off, so an update does not break its phones' addresses.
Everything about certificates — the console's section, /cert, the family
app's hint, the desktop panel's button — exists only while on HTTPS.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from ninaivu.server import https_choice
from ninaivu.utils import tls

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path, https=""):
    saved = []
    return SimpleNamespace(state_dir=tmp_path, https=https,
                           save=lambda: saved.append(True)), saved


def _own_ca(tmp_path):
    path = tls.ca_certificate_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("-----BEGIN CERTIFICATE-----\n")


def test_a_new_install_is_http_and_remembers_it(tmp_path):
    cfg, saved = _cfg(tmp_path)
    assert https_choice.decide(cfg) == (False, None)
    assert cfg.https == "off" and saved


def test_an_install_that_served_https_keeps_it(tmp_path):
    _own_ca(tmp_path)
    cfg, _ = _cfg(tmp_path)
    assert https_choice.decide(cfg) == (True, None)
    assert cfg.https == "on"


def test_the_saved_choice_wins_over_an_old_https_flag(tmp_path):
    _own_ca(tmp_path)
    cfg, _ = _cfg(tmp_path, "off")
    assert https_choice.decide(cfg, https_flag=True) == (False, None)
    cfg.https = "on"
    assert https_choice.decide(cfg) == (True, None)


def test_a_certificate_or_http_flag_decides_and_says_so(tmp_path):
    cfg, _ = _cfg(tmp_path, "off")
    assert https_choice.decide(cfg, cert=True) == (True, "cert")
    cfg.https = "on"
    assert https_choice.decide(cfg, http_flag=True) == (False, "http-flag")


def test_the_family_port_follows_the_scheme():
    assert https_choice.port_for(80, True) == 443
    assert https_choice.port_for(443, False) == 80
    assert https_choice.port_for(5000, True) == 5000


def test_the_restart_rewrites_the_saved_arguments():
    from ninaivu.desktop.relaunch import with_https
    old = ["--host", "0.0.0.0", "--port", "443", "--admin-port", "3000", "--https"]
    assert with_https(old, False) == ["--host", "0.0.0.0", "--port", "80",
                                      "--admin-port", "3000"]
    assert with_https(["--port", "80", "--http"], True) == ["--port", "443"]


def test_start_py_reads_the_same_choice(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ninaivu_start", ROOT / "launcher/start.py")
    start = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(start)
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    assert start.expected_https() is False
    _own_ca(tmp_path)
    assert start.expected_https() is True
    (tmp_path / "config.json").write_text(json.dumps({"https": "off"}))
    assert start.expected_https() is False


# --- the console ---------------------------------------------------------

def test_on_http_there_is_no_certificate_to_fetch(app, as_admin):
    app.config["MV_SCHEME"] = "http"
    app.config["MV_SERVE_LOCAL_CA"] = False
    assert app.test_client().get("/cert").status_code == 404
    state = as_admin.get("/api/admin/server").get_json()["https"]
    assert state["on"] is False and state["own_certificate"] is False


def test_the_switch_saves_and_restarts(app, as_admin, monkeypatch):
    from ninaivu.api import server_api
    calls = []
    monkeypatch.setattr(server_api, "_begin_restart",
                        lambda **kw: (calls.append(kw) or {"restarting": True}, 202))
    reply = as_admin.post("/api/admin/server/https", json={"enabled": True})
    assert reply.status_code == 202 and reply.get_json()["https"] is True
    assert calls[0]["https"] is True
    assert app.config["MV_CONFIG"].https == "on"
    assert as_admin.post("/api/admin/server/https", json={"enabled": "yes"}).status_code == 400


def test_the_switch_says_when_the_command_line_decides(app, as_admin):
    app.config["MV_HTTPS_FIXED_BY"] = "http-flag"
    reply = as_admin.post("/api/admin/server/https", json={"enabled": True})
    assert reply.status_code == 409


def test_only_an_administrator_switches_it(as_family):
    assert as_family.post("/api/admin/server/https", json={"enabled": True}).status_code in (401, 403, 404)


def test_the_family_apps_certificate_hint_starts_hidden():
    page = (ROOT / "ninaivu/templates/index.html").read_text(encoding="utf-8")
    assert 'id="help-cert" hidden' in page
    console = (ROOT / "ninaivu/templates/admin.html").read_text(encoding="utf-8")
    assert 'id="sv-cert-block" hidden' in console
