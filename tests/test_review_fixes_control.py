"""Starting, restarting and installing: fixes from the September review.

* The desktop panel replayed the running server's whole command line on the
  next start - ``--admin USER:PASSWORD`` included, kept in plain text in
  settings.json, which then failed the start because the account existed.
* A restart under a service manager (Task Scheduler, systemd, a container)
  started the next server outside that manager's reach.
* The Network access switch, turned off in a container, left nothing able to
  reach the server.
* A stuck installer was never timed out, and never killed.
* The launcher started a second server when relaying the first one's output
  failed.
* The Gemini key and the stop token were briefly world-readable, and Gemini
  opened uploads without the pixel cap.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --- one-shot options are not carried into the next start ---------------------------

def test_one_shot_options_are_left_out_in_either_form():
    from ninaivu.desktop.control import without_one_shot_options

    arguments = ["/photos", "--admin", "anna:secret", "--host", "0.0.0.0",
                 "--admin=bob:hunter2", "--rescan", "--open", "--supervised",
                 "--admin-host", "127.0.0.1", "--admin-port", "3000",
                 "--admin-name", "family-admin", "--workers", "4"]
    kept = without_one_shot_options(arguments)
    assert kept == ["/photos", "--host", "0.0.0.0", "--admin-host", "127.0.0.1",
                    "--admin-port", "3000", "--admin-name", "family-admin",
                    "--workers", "4"]
    assert not any("secret" in a or "hunter2" in a for a in kept)
    assert without_one_shot_options(None) == []


def _controller(tmp_path, monkeypatch, record=None, cmdline=None):
    from ninaivu.desktop import control as module

    monkeypatch.setattr(module.Controller, "record", lambda _: record)
    monkeypatch.setattr(module, "_server_process", lambda record: None)
    if cmdline is not None:
        monkeypatch.setattr(module, "psutil", SimpleNamespace(
            Process=lambda pid: SimpleNamespace(cmdline=lambda: list(cmdline))))
    cfg = SimpleNamespace(state_dir=tmp_path / "state", host="127.0.0.1", port=5000,
                          admin_port=3000, ai_engine="off")
    return module.Controller(root=tmp_path, cfg=cfg)


def test_a_running_servers_password_is_not_captured(tmp_path, monkeypatch):
    record = {"pid": 42, "port": 5000, "admin_port": 3000}
    command = ["python", "-m", "ninaivu", "/photos", "--admin", "anna:secret",
               "--rescan", "--open", "--host", "0.0.0.0"]
    controller = _controller(tmp_path, monkeypatch, record=record, cmdline=command)
    assert controller.settings["arguments"] == ["/photos", "--host", "0.0.0.0"]
    controller.save_mode("standard")
    assert "secret" not in controller.settings_path.read_text()


def test_a_settings_file_saved_before_the_fix_is_cleaned(tmp_path, monkeypatch):
    runtime = tmp_path / ".ninaivu-control"
    runtime.mkdir()
    (runtime / "settings.json").write_text(json.dumps({
        "mode": "standard",
        "arguments": ["--admin", "anna:secret", "--rescan", "--port", "5000"],
    }))
    controller = _controller(tmp_path, monkeypatch)
    assert controller.settings["arguments"] == ["--port", "5000"]
    controller.save_mode("standard")
    saved = json.loads(controller.settings_path.read_text())
    assert saved["arguments"] == ["--port", "5000"]


# --- who starts the server again -----------------------------------------------------

def _args(*words):
    from ninaivu.__main__ import build_parser

    return build_parser().parse_args(list(words))


def test_the_scheduled_task_says_it_is_supervised():
    from ninaivu.__main__ import supervisor_of

    never = lambda path: False                                   # noqa: E731
    assert supervisor_of(_args("--supervised"), {}, never, lambda: 4242) == "service"
    assert supervisor_of(_args(), {}, never, lambda: 4242) is None


def test_systemd_counts_only_when_it_started_the_process():
    from ninaivu.__main__ import supervisor_of

    never = lambda path: False                                   # noqa: E731
    env = {"INVOCATION_ID": "abc"}
    assert supervisor_of(_args(), env, never, lambda: 1) == "systemd"
    # A terminal opened from a desktop session can inherit INVOCATION_ID.
    assert supervisor_of(_args(), env, never, lambda: 4242) is None


def test_a_container_is_recognised_and_can_be_told():
    from ninaivu.server.runfile import in_container

    docker = lambda path: path == "/.dockerenv"                  # noqa: E731
    never = lambda path: False                                   # noqa: E731
    assert in_container({}, docker) is True
    assert in_container({}, never) is False
    assert in_container({"NINAIVU_IN_CONTAINER": "1"}, never) is True
    assert in_container({"NINAIVU_IN_CONTAINER": "0"}, docker) is False


def test_the_state_folder_can_be_given_on_the_command_line():
    assert _args("--state-dir", "C:/Users/anna/.ninaivu").state_dir == "C:/Users/anna/.ninaivu"


# --- network access in a container -------------------------------------------------

def test_switched_off_is_ignored_in_a_container():
    from ninaivu.__main__ import resolve_hosts

    args = _args("--host", "0.0.0.0")
    cfg = SimpleNamespace(host="0.0.0.0", admin_host=None, network_access=False)
    resolve_hosts(cfg, args, container=True)
    assert cfg.host == "0.0.0.0" and args.no_mdns is False

    args = _args("--host", "0.0.0.0")
    cfg = SimpleNamespace(host="0.0.0.0", admin_host=None, network_access=False)
    resolve_hosts(cfg, args, container=False)
    assert (cfg.host, cfg.admin_host) == ("127.0.0.1", "127.0.0.1")


def test_the_console_refuses_to_switch_it_off_in_a_container(as_admin, app, monkeypatch):
    from ninaivu.api import server_api

    monkeypatch.setattr(server_api.runfile, "in_container", lambda *a, **k: True)
    cfg = app.config["MV_CONFIG"]
    cfg.network_access = True
    reply = as_admin.post("/api/admin/server/network", json={"enabled": False})
    assert reply.status_code == 409
    assert "container" in reply.get_json()["error"]
    assert cfg.network_access is True


# --- a restart under a service manager ----------------------------------------------

@pytest.fixture()
def supervised(app, monkeypatch):
    from ninaivu.api import server_api

    asked = []
    spawned = []
    monkeypatch.setattr(server_api, "_restart_process", None)
    monkeypatch.setattr(server_api, "_spawn_relauncher",
                        lambda *a, **k: spawned.append(a) or None)
    app.config["MV_STOP_TOKEN"] = "token"
    app.config["MV_SUPERVISOR"] = "systemd"
    app.config["MV_RESTART"] = lambda: asked.append(True)
    yield SimpleNamespace(asked=asked, spawned=spawned)
    app.config.pop("MV_SUPERVISOR", None)
    app.config.pop("MV_RESTART", None)


def test_a_supervised_restart_exits_instead_of_starting_a_helper(as_admin, supervised):
    reply = as_admin.post("/api/admin/server/restart", json={})
    assert reply.status_code == 202, reply.get_json()
    deadline = time.time() + 3
    while not supervised.asked and time.time() < deadline:
        time.sleep(0.05)
    assert supervised.asked == [True]
    assert supervised.spawned == [], "a helper would start a server outside systemd"
    assert as_admin.post("/api/admin/server/restart", json={}).status_code == 409


def test_a_supervised_server_cannot_change_mode_by_restarting(as_admin, supervised):
    from ninaivu.utils.resources import budget

    other = next(m for m in ("standard", "performance", "power-saving")
                 if m != budget()["mode"])
    reply = as_admin.post("/api/admin/server/restart", json={"mode": other})
    assert reply.status_code == 409
    assert supervised.asked == [] and supervised.spawned == []


def test_a_supervised_server_is_not_stopped_from_the_console(as_admin, app, supervised):
    # The supervisor would only start it again; the reply says where to stop it.
    stopped = []
    app.config["MV_SHUTDOWN"] = lambda: stopped.append(True)
    try:
        assert as_admin.get("/api/admin/server").get_json()["can_stop"] is False
        reply = as_admin.post("/api/admin/server/stop", json={})
        assert reply.status_code == 409 and "systemctl stop" in reply.get_json()["error"]
        app.config["MV_SUPERVISOR"] = "service"
        reply = as_admin.post("/api/admin/server/stop", json={})
        assert reply.status_code == 409
        assert "install-service.ps1 -Action Stop" in reply.get_json()["error"]
        time.sleep(0.6)
        assert stopped == []
    finally:
        app.config.pop("MV_SHUTDOWN", None)


# --- a stuck installer --------------------------------------------------------------

class StuckInstaller:
    """Says one line, then neither ends its output nor exits until killed."""

    def __init__(self):
        self.killed = threading.Event()
        self.said = False

    def __call__(self, command, **kwargs):
        return self

    @property
    def stdout(self):
        return self

    def readline(self):
        if not self.said:
            self.said = True
            return "Waiting for a prompt nobody sees\n"
        self.killed.wait(10)
        return ""

    def wait(self, timeout=None):
        if self.killed.wait(timeout):
            return -9
        raise subprocess.TimeoutExpired("installer", timeout)

    def kill(self):
        self.killed.set()


def test_a_stuck_installer_is_stopped_at_the_timeout(monkeypatch):
    from ninaivu.media import components

    monkeypatch.setattr(components, "refresh_tools", lambda: None)
    monkeypatch.setattr(components, "INSTALL_TIMEOUT", 0.3)
    runner = StuckInstaller()
    assert components.installs.start("heif", ["pip", "install", "x"], runner=runner)
    deadline = time.time() + 8
    while components.installs.running("heif") and time.time() < deadline:
        time.sleep(0.02)
    state = components.installs.state("heif")
    assert state["status"] == "failed"
    assert runner.killed.is_set(), "the installer was left running"
    assert "Waiting for a prompt" in "\n".join(state["log"])


# --- the launcher starts one server ----------------------------------------------

@pytest.fixture()
def launcher():
    sys.path.insert(0, str(ROOT / "launcher"))
    import start                                                 # noqa: PLC0415
    return start


class BrokenSink:
    def write(self, text):
        raise OSError("the window is gone")

    def flush(self):
        pass


class Collect:
    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass


def test_a_failing_output_is_dropped_and_the_rest_still_relayed(launcher):
    kept = Collect()
    launcher.relay(iter(["one\n", "two\n"]), (BrokenSink(), kept))
    assert kept.lines == ["one\n", "two\n"]


def test_a_broken_pipe_waits_for_the_server_rather_than_starting_another(launcher, monkeypatch):
    class Lines:
        def __iter__(self):
            return self

        def __next__(self):
            raise OSError("pipe broken")

    class Server:
        stdout = Lines()
        waited = None

        def wait(self, timeout=None):
            Server.waited = timeout
            return 3

    started = []
    monkeypatch.setattr(launcher.subprocess, "call", lambda *a, **k: started.append(a) or 0)
    assert launcher.follow(Server(), (Collect(),)) == 3
    assert Server.waited is None, "it gave up waiting on a running server"
    assert started == []


# --- secrets are never briefly readable ------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_run_file_is_created_owner_only(tmp_path, monkeypatch):
    from ninaivu.server import runfile

    created = []
    real_open = os.open

    def watch(path, flags, mode=0o777, *a, **k):
        if flags & os.O_CREAT:
            created.append(mode)
        return real_open(path, flags, mode, *a, **k)

    monkeypatch.setattr(runfile.os, "open", watch)
    runfile.write(tmp_path, port=5000, admin_port=3000)
    assert created == [0o600]
    assert (runfile.path_for(tmp_path).stat().st_mode & 0o777) == 0o600


# --- Gemini opens uploads with the pixel cap ----------------------------------------

def _png_claiming(width, height):
    """A PNG header declaring *width* x *height*, with no picture behind it."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def test_gemini_refuses_an_image_that_claims_too_many_pixels():
    gemini = pytest.importorskip("ninaivu_gemini.gemini")

    with pytest.raises(ValueError):
        gemini._open_upload(_png_claiming(100_000, 100_000))


def test_gemini_turns_a_decompression_bomb_into_a_refusal(monkeypatch):
    gemini = pytest.importorskip("ninaivu_gemini.gemini")

    def bomb(data):
        raise gemini.Image.DecompressionBombError("too many pixels")

    monkeypatch.setattr(gemini, "open_untrusted", bomb)
    with pytest.raises(ValueError):
        gemini._open_upload(b"whatever")
