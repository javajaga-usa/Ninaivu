"""The first administrator's setup code can always be found.

The page asked for a code that the person could not see: a server started by
the Control Panel, the tray or the sign-in task has no window to print it in,
the one line at the top of the banner scrolled away under it, and a restart
made a new code. And the owner was asked at all because the Control Panel
opens ``ninaivu.local``, which reaches the server from the computer's own
network address rather than from loopback.
"""

import os
import sys

import pytest

from ninaivu.server import auth, workload

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _fresh_code(monkeypatch):
    monkeypatch.setattr(auth, "_SETUP_CODE", None)


def _home_app(scanned):
    from ninaivu import build_services, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    return create_home_app(services), cfg, conn


def test_the_code_is_kept_in_the_state_folder_and_survives_a_restart(tmp_path, monkeypatch):
    code = auth.setup_code(tmp_path)
    path = auth.setup_code_path(tmp_path)
    assert path.read_text(encoding="utf-8").splitlines()[0] == code
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(auth, "_SETUP_CODE", None)          # a new process
    assert auth.setup_code(tmp_path) == code


def test_the_code_is_removed_once_it_has_done_its_job(tmp_path):
    auth.setup_code(tmp_path)
    auth.forget_setup_code(tmp_path)
    assert not auth.setup_code_path(tmp_path).exists()
    auth.forget_setup_code(tmp_path)                        # and again: no error


def test_a_damaged_file_gets_a_new_code(tmp_path):
    auth.setup_code_path(tmp_path).write_text("not a code\n", encoding="utf-8")
    code = auth.setup_code(tmp_path)
    assert len(code) == 10
    assert auth.setup_code_path(tmp_path).read_text(encoding="utf-8").startswith(code)


def test_opened_by_this_computers_own_address_no_code_is_asked(scanned, monkeypatch):
    app, cfg, conn = _home_app(scanned)
    monkeypatch.setattr(workload, "own_addresses",
                        lambda: frozenset({"127.0.0.1", "::1", "192.168.1.20"}))
    client = app.test_client()
    here = {"REMOTE_ADDR": "192.168.1.20"}
    assert client.get("/api/auth/state", environ_base=here).get_json()["setup_code_required"] is False
    mapped = {"REMOTE_ADDR": "::ffff:192.168.1.20"}
    assert client.get("/api/auth/state", environ_base=mapped).get_json()["setup_code_required"] is False
    # Another device is still asked, and so is anything through a proxy.
    far = {"REMOTE_ADDR": "192.168.1.21"}
    assert client.get("/api/auth/state", environ_base=far).get_json()["setup_code_required"] is True
    proxied = client.get("/api/auth/state", environ_base=here,
                         headers={"X-Real-IP": "203.0.113.9"}).get_json()
    assert proxied["setup_code_required"] is True

    auth.setup_code(cfg.state_dir)
    body = {"username": "dad", "password": "correcthorse1", "name": "Dad"}
    assert client.post("/api/auth/setup", json=body, environ_base=here).status_code == 200
    assert not auth.needs_setup(conn)
    assert not auth.setup_code_path(cfg.state_dir).exists(), "removed after setup"


def test_another_device_can_use_the_code_from_the_file(scanned):
    app, cfg, conn = _home_app(scanned)
    auth.setup_code(cfg.state_dir)
    saved = auth.setup_code_path(cfg.state_dir).read_text(encoding="utf-8").split()[0]
    client = app.test_client()
    far = {"REMOTE_ADDR": "192.168.1.51"}
    body = {"username": "mum", "password": "correcthorse1", "name": "Mum"}
    refused = client.post("/api/auth/setup", json=body, environ_base=far)
    assert refused.status_code == 403
    assert auth.SETUP_CODE_FILE in refused.get_json()["error"]
    assert str(cfg.state_dir) not in refused.get_json()["error"], "no path to a stranger"
    body["setup_code"] = saved
    assert client.post("/api/auth/setup", json=body, environ_base=far).status_code == 200
    assert not auth.setup_code_path(cfg.state_dir).exists()


def test_the_banner_ends_with_the_code_and_where_it_is_kept():
    with open(os.path.join(ROOT, "ninaivu", "__main__.py"), encoding="utf-8") as fh:
        source = fh.read()
    first_run = source.index('First run — open {home_url}')
    assert source.index('Setup code:  {setup_code}') > first_run
    assert "auth.setup_code(cfg.state_dir)" in source
    assert "auth.forget_setup_code(cfg.state_dir)" in source
