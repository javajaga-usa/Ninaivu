"""Ninaivu starts again when somebody signs in to the computer.

A Mac restarted by an update, or after a power cut, came back without Ninaivu,
and the family app was down until somebody opened it.
"""
import plistlib
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ninaivu.desktop import autostart, control


def test_the_launch_agent_starts_ninaivu_once_at_sign_in(tmp_path):
    root = tmp_path / "Ninaivu"
    agent = autostart.launch_agent(root)
    assert agent["RunAtLoad"] is True
    assert "KeepAlive" not in agent, "launchd would start it again the moment it was stopped"
    assert agent["ProgramArguments"] == [str(root / ".venv/bin/python"), "-m",
                                         "ninaivu.desktop.autostart", "--start"]
    assert agent["WorkingDirectory"] == str(root)
    assert agent["StandardOutPath"] == str(root / ".ninaivu-control/autostart.log")


def test_turning_it_on_and_off_on_a_mac(tmp_path):
    home, root = tmp_path / "home", tmp_path / "Ninaivu"
    assert not autostart.enabled(home, "darwin")
    autostart.enable(root, home, "darwin")
    path = home / "Library/LaunchAgents/local.ninaivu.start.plist"
    assert autostart.enabled(home, "darwin")
    assert plistlib.loads(path.read_bytes())["Label"] == "local.ninaivu.start"
    autostart.disable(home, "darwin")
    assert not autostart.enabled(home, "darwin") and not path.exists()
    autostart.disable(home, "darwin")                 # already off: not an error


@pytest.mark.skipif(shutil.which("plutil") is None, reason="macOS's plist checker")
def test_launchd_would_accept_the_file(tmp_path):
    autostart.enable(tmp_path / "Ninaivu", tmp_path, "darwin")
    checked = subprocess.run(["plutil", "-lint", str(autostart.agent_path(tmp_path))],
                             capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_windows_starts_it_without_a_console_window(tmp_path):
    assert autostart.command(tmp_path, "win32")[0].endswith("pythonw.exe")


def test_elsewhere_it_says_what_to_use_instead(tmp_path):
    assert not autostart.supported("linux")
    with pytest.raises(RuntimeError, match="systemd"):
        autostart.enable(tmp_path, tmp_path, "linux")


def test_waiting_gives_up_after_its_patience():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    assert autostart.wait_for(lambda: False, patience=10, step=2,
                              clock=lambda: now[0], sleep=sleep) is False
    assert sum(slept) == 10
    answers = iter([False, False, True])
    assert autostart.wait_for(lambda: next(answers), patience=10, step=2,
                              clock=lambda: now[0], sleep=sleep) is True


def test_it_waits_for_the_network_and_the_drive_then_starts_as_the_panel_does():
    """Without an address at start, the name is never announced for the whole
    run; without the drive, the library starts missing."""
    asked = []

    def wait(ready):
        asked.append(ready())
        return True

    started = SimpleNamespace(start=lambda: "Ninaivu is running.")
    said = autostart.start(controller=started, addresses=lambda: ["192.168.0.229"],
                           libraries_here=lambda: True, wait=wait)
    assert said == "Ninaivu is running." and asked == [True, True]


def test_it_starts_anyway_when_they_never_come(capsys):
    started = SimpleNamespace(start=lambda: "Ninaivu is running.")
    autostart.start(controller=started, addresses=lambda: [], libraries_here=lambda: False,
                    wait=lambda ready: ready())
    out = capsys.readouterr().out
    assert "no network address" in out and "library folder is still not there" in out


def test_a_start_that_fails_says_so_in_its_log(monkeypatch, capsys):
    def broken(**_kwargs):
        raise RuntimeError("Ninaivu could not start. Use View logs for details.")
    monkeypatch.setattr(autostart, "start", broken)
    monkeypatch.setattr(autostart.os, "chdir", lambda _path: None)
    assert autostart.main(["--start"]) == 1
    assert "could not start Ninaivu" in capsys.readouterr().out


# -- the tools a server started from Finder or launchd can find ----------------

def test_a_server_started_outside_a_terminal_finds_homebrews_ffmpeg():
    env = control.with_tool_folders({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, "darwin",
                                    exists=lambda folder: folder == "/opt/homebrew/bin")
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"


def test_a_path_that_has_them_is_left_as_it_is():
    path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin"
    env = control.with_tool_folders({"PATH": path}, "darwin", exists=lambda _f: True)
    assert env["PATH"] == path


def test_other_systems_are_left_alone():
    env = {"PATH": r"C:\Windows"}
    assert control.with_tool_folders(env, "win32", exists=lambda _f: True) == env


def test_the_control_panel_starts_the_server_with_them(tmp_path, monkeypatch):
    monkeypatch.setattr(control.Controller, "record", lambda _: None)
    c = control.Controller(root=tmp_path, cfg=SimpleNamespace(
        state_dir=tmp_path / "state", host="127.0.0.1", port=5000, admin_port=3000,
        ai_engine="off"))
    python = tmp_path / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    seen = {}

    def popen(command, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")
    monkeypatch.setattr(control.subprocess, "Popen", popen)
    monkeypatch.setattr(c, "_start_ollama", lambda _env: None)
    monkeypatch.setattr(control, "with_tool_folders",
                        lambda env: {**env, "PATH": "tools-added"})
    with pytest.raises(RuntimeError, match="stop here"):
        c.start()
    assert seen["env"]["PATH"] == "tools-added"
