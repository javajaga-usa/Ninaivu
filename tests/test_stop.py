"""Stopping Ninaivu from outside the window it is running in.

Ctrl+C is right for a server you are looking at. Everything else — a desktop
shortcut, a scheduled task, a batch file that restarts after copying new
photographs in — needs a way to say *stop* to a process it did not start. That
is what the run file and the shutdown endpoint are for, and what is tested
here is mostly what must **not** work: the endpoint has to be unreachable from
the network, unreachable without the token, and absent from the family app.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ninaivu.server import runfile


# ---------------------------------------------------------------------------
# The run file
# ---------------------------------------------------------------------------

def test_the_run_file_says_where_ninaivu_is(tmp_path):
    token = runfile.write(tmp_path, port=5000, admin_port=3000,
                          host="0.0.0.0", version="5.0.0", scheme="https")
    record = runfile.read(tmp_path)
    assert record["pid"] == os.getpid()
    assert record["port"] == 5000
    assert record["admin_port"] == 3000
    assert record["token"] == token
    assert record["scheme"] == "https"
    assert record["started_at"] > 0


def test_the_token_is_long_enough_to_be_a_secret(tmp_path):
    token = runfile.write(tmp_path, port=1, admin_port=2)
    assert len(token) >= 32
    assert token != runfile.write(tmp_path, port=1, admin_port=2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_run_file_is_readable_only_by_this_account(tmp_path):
    # The token is the whole of the security on the shutdown endpoint, so the
    # file holding it must not be world-readable.
    runfile.write(tmp_path, port=1, admin_port=2)
    mode = (runfile.path_for(tmp_path).stat().st_mode & 0o777)
    assert mode & 0o077 == 0, oct(mode)


def test_a_missing_or_broken_run_file_is_not_a_crash(tmp_path):
    assert runfile.read(tmp_path) is None
    runfile.path_for(tmp_path).write_text("{ not json")
    assert runfile.read(tmp_path) is None
    runfile.path_for(tmp_path).write_text(json.dumps({"port": 5000}))
    assert runfile.read(tmp_path) is None      # no pid: useless


def test_clearing_it_twice_is_fine(tmp_path):
    runfile.write(tmp_path, port=1, admin_port=2)
    runfile.clear(tmp_path)
    runfile.clear(tmp_path)
    assert runfile.read(tmp_path) is None


def test_shutdown_does_not_clear_another_servers_record(tmp_path):
    runfile.write(tmp_path,port=1,admin_port=2)
    record=runfile.read(tmp_path);record['pid']=os.getpid()+1
    runfile.path_for(tmp_path).write_text(json.dumps(record))
    runfile.clear(tmp_path)
    assert runfile.read(tmp_path)==record


def test_server_lock_rejects_duplicate_and_releases_on_exit(tmp_path):
    with runfile.server_lock(tmp_path):
        with pytest.raises(RuntimeError,match='already using'):
            with runfile.server_lock(tmp_path):pass
    with runfile.server_lock(tmp_path):pass


def test_duplicate_cli_exits_before_startup_or_configuration_changes(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from ninaivu import __main__ as entry
    monkeypatch.setattr(entry.Config,'load',lambda:SimpleNamespace(state_dir=tmp_path))
    monkeypatch.setattr(entry,'_run',lambda *_:pytest.fail('Duplicate must not start'))
    with runfile.server_lock(tmp_path):
        assert entry.main(['--ai','off'])==1


def test_a_process_that_is_not_there_is_reported_as_such():
    assert runfile.is_running(os.getpid()) is True
    assert runfile.is_running(0) is False
    assert runfile.is_running(-1) is False


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

@pytest.fixture()
def console(app):
    """The admin console app, with a stop hook wired up as the server does."""
    from ninaivu import create_admin_app

    admin = create_admin_app(app.config["MV_SERVICES"])
    admin.config["MV_STOP_TOKEN"] = "the-right-token"
    called = []
    admin.config["MV_SHUTDOWN"] = lambda: called.append(True)
    return admin, called


def test_the_right_token_stops_it(console):
    admin, called = console
    client = admin.test_client()
    response = client.post("/api/admin/shutdown",
                           json={"token": "the-right-token"})
    assert response.status_code == 200
    assert response.get_json()["stopping"] is True
    assert called == [True]


def test_the_wrong_token_is_a_404_not_a_403(console):
    # A route that answers "wrong token" confirms it exists and invites
    # guessing at the right one.
    admin, called = console
    client = admin.test_client()
    response = client.post("/api/admin/shutdown", json={"token": "guess"})
    assert response.status_code == 404
    assert called == []


def test_no_token_at_all_stops_nothing(console):
    admin, called = console
    response = admin.test_client().post("/api/admin/shutdown", json={})
    assert response.status_code == 404
    assert called == []


def test_the_token_may_come_in_a_header(console):
    admin, called = console
    response = admin.test_client().post(
        "/api/admin/shutdown", headers={"X-Ninaivu-Token": "the-right-token"})
    assert response.status_code == 200
    assert called == [True]


def test_a_request_from_the_network_is_refused(console):
    # The endpoint is for a script on this machine. A server the network can
    # stop is a server anybody on the network can stop.
    admin, called = console
    response = admin.test_client().post(
        "/api/admin/shutdown", json={"token": "the-right-token"},
        environ_overrides={"REMOTE_ADDR": "192.168.1.44"})
    assert response.status_code == 404
    assert called == []


def test_a_server_with_no_stop_hook_answers_404(app):
    from ninaivu import create_admin_app

    admin = create_admin_app(app.config["MV_SERVICES"])
    response = admin.test_client().post("/api/admin/shutdown",
                                        json={"token": "anything"})
    assert response.status_code == 404


def test_the_family_app_has_no_shutdown_route_at_all(app):
    # Absent, not forbidden — the same rule every other management route
    # follows. Nothing reachable from the sofa can stop the server.
    #
    # The real family app is create_home_app; the `app` fixture is the
    # single-port factory, which mounts everything on one face on purpose.
    from ninaivu import create_home_app

    home = create_home_app(app.config["MV_SERVICES"])
    home.config["MV_STOP_TOKEN"] = "the-right-token"
    home.config["MV_SHUTDOWN"] = lambda: pytest.fail("the family app stopped it")
    response = home.test_client().post("/api/admin/shutdown",
                                       json={"token": "the-right-token"})
    assert response.status_code == 404
    assert "/api/admin/shutdown" not in {
        str(rule) for rule in home.url_map.iter_rules()}


# ---------------------------------------------------------------------------
# Stopping the work, rather than killing it
# ---------------------------------------------------------------------------

def test_stopping_asks_every_part_to_stop_and_reports_what_would_not(app):
    services = app.config["MV_SERVICES"]

    asked = []

    class Refuses:
        def stop(self, *a, **kw):
            raise RuntimeError("busy")

    services.scanner.stop = lambda join=False: asked.append("scan")
    services.cloud.pause = lambda: asked.append("cloud")
    services.power.stop = lambda: asked.append("power")
    services.guardian = Refuses()

    problems = services.stop()
    assert "scan" in asked and "cloud" in asked and "power" in asked
    # One part refusing must not stop the rest being asked, and must be said
    # out loud rather than swallowed.
    assert any("guardian" in p and "busy" in p for p in problems), problems


def test_the_stop_tool_exists():
    """The launchers that wrapped it are gone; the tool itself is the product's
    stop command until the installers provide one."""
    root = Path(__file__).resolve().parents[1]
    assert (root / "tools" / "stop.py").is_file()


# ---------------------------------------------------------------------------
# Stopping without the rest of Ninaivu installed
# ---------------------------------------------------------------------------

def test_stopping_does_not_need_the_web_framework(tmp_path):
    """`stop.bat` runs on whatever Python is on PATH, which is often not the
    virtual environment. Importing `ninaivu` pulls in Flask, so a stop used to
    fail on an import that has nothing to do with stopping anything."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    state = tmp_path / "state"
    state.mkdir()
    (state / "ninaivu.run").write_text(json.dumps(
        {"pid": 999_999, "port": 5000, "admin_port": 3000, "token": "x"}))

    # Make `import flask` fail for the child, the way a bare interpreter does.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "flask.py").write_text("raise ImportError('no flask here')\n")

    env = {**os.environ, "NINAIVU_STATE_DIR": str(state),
           "PYTHONPATH": str(blocked)}
    done = subprocess.run([sys.executable, str(root / "tools" / "stop.py")],
                          env=env, capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    assert "not running" in done.stdout.lower()
    assert not (state / "ninaivu.run").exists(), "the stale run file is cleared"


def test_the_state_directory_is_found_without_ninaivu(tmp_path, monkeypatch):
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_stop_tool", root / "tools" / "stop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path / "elsewhere"))
    assert module._state_dir(None) == (tmp_path / "elsewhere").resolve()
    assert module._state_dir(str(tmp_path / "given")) == tmp_path / "given"


def test_stop_tool_accepts_https_flags_and_passthroughs(tmp_path):
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, str(root / "tools" / "stop.py"), "--https", "--state-dir", str(tmp_path), "--unrecognized-extra-flag"],
        capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stderr
    assert "not appear to be running" in done.stdout.lower()


def test_ask_to_stop_succeeds_over_https(tmp_path):
    pytest.importorskip("cryptography")
    import threading
    import time
    from ninaivu import build_services, create_admin_app
    from ninaivu.server.config import Config
    from ninaivu.server.http import make_threaded_server
    from ninaivu.utils import tls
    from tools.stop import ask_to_stop

    cfg = Config.load()
    ssl_files = tls.ensure_certificate(tmp_path)
    services = build_services(cfg)
    admin = create_admin_app(services)
    token = "test-https-stop-token"
    admin.config["MV_STOP_TOKEN"] = token
    stopped = []
    admin.config["MV_SHUTDOWN"] = lambda: stopped.append(True)

    server = make_threaded_server("127.0.0.1", 0, admin, ssl_files)
    port = int(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        time.sleep(0.3)
        assert ask_to_stop(port, token, scheme="https") is True
        assert stopped == [True]
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# The root of the repository
# ---------------------------------------------------------------------------

def test_the_root_holds_the_launcher_and_only_what_tooling_needs_there():
    """One file to double-click, and the files Git, GitHub and pip look for by
    name. Everything else is in a folder for what it is."""
    root = Path(__file__).resolve().parents[1]
    allowed = {"start.cmd", "README.md", "LICENSE", "pyproject.toml", ".gitignore",
               ".gitattributes", ".editorconfig", ".pre-commit-config.yaml", ".dockerignore"}
    files = {p.name for p in root.iterdir() if p.is_file()}
    assert files <= allowed, f"loose at the root: {sorted(files - allowed)}"
    assert "start.cmd" in files


def test_start_cmd_runs_the_launcher_with_windows_line_endings():
    root = Path(__file__).resolve().parents[1]
    raw = (root / "start.cmd").read_bytes()
    assert b"\r\n" in raw and raw.replace(b"\r\n", b"").count(b"\n") == 0, "start.cmd mixes line endings"
    assert b"launcher\\start.py" in raw
    assert (root / "launcher" / "start.py").is_file()
    assert b"launcher/start.py" in (root / "launcher" / "start.sh").read_bytes()
