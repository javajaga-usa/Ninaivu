"""The portable Windows zip: Ninaivu.exe, app/ and logs/ and nothing else.

The zip is made by installers/windows/build-portable.ps1 from what the
installer build assembled, and Ninaivu.exe (installers/windows/portable/
Ninaivu.cs) is the only program in it a person sees. These tests build the
launcher with the compiler that ships with Windows, stand a fake private
Python in the layout the zip has, and check what the launcher does with it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "installers" / "windows"
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"

needs_csc = pytest.mark.skipif(sys.platform != "win32" or not CSC.is_file(),
                               reason="needs the .NET Framework compiler that ships with Windows")

#: Stands in for pythonw.exe: writes what it was started with where the log goes.
FAKE_PYTHON = r"""
using System;
using System.Collections;
using System.IO;
using System.Text;
static class Fake {
    static int Main(string[] args) {
        var sb = new StringBuilder("{");
        string[] keys = { "NINAIVU_HOME", "NINAIVU_STATE_DIR", "NINAIVU_AI_MODELS_DIR",
                          "NINAIVU_LOG_DIR", "NINAIVU_PORTABLE", "PYTHONHOME", "PYTHONPATH" };
        foreach (var key in keys) {
            var value = Environment.GetEnvironmentVariable(key);
            sb.Append('"').Append(key).Append("\":").Append(value == null ? "null"
                : "\"" + value.Replace("\\", "\\\\") + "\"").Append(',');
        }
        sb.Append("\"cwd\":\"").Append(Directory.GetCurrentDirectory().Replace("\\", "\\\\")).Append("\",");
        sb.Append("\"args\":[");
        for (int i = 0; i < args.Length; i++) {
            if (i > 0) sb.Append(',');
            sb.Append('"').Append(args[i].Replace("\\", "\\\\").Replace("\"", "\\\"")).Append('"');
        }
        sb.Append("]}");
        File.WriteAllText(Path.Combine(Environment.GetEnvironmentVariable("NINAIVU_LOG_DIR"), "probe.json"), sb.ToString());
        return 0;
    }
}
"""


def _compile(target: str, output: Path, *sources: Path) -> None:
    done = subprocess.run([str(CSC), "-nologo", f"-target:{target}", "-platform:x64",
                           f"-out:{output}", *map(str, sources)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The launcher and the fake Python, compiled once for the whole module."""
    folder = tmp_path_factory.mktemp("portable-build")
    launcher, fake_python = folder / "Ninaivu.exe", folder / "pythonw.exe"
    _compile("winexe", launcher, WINDOWS / "portable" / "Ninaivu.cs")
    source = folder / "fake.cs"
    source.write_text(FAKE_PYTHON, encoding="utf-8")
    _compile("winexe", fake_python, source)
    return launcher, fake_python


@pytest.fixture
def portable(built, tmp_path):
    """A folder laid out as the zip is, with a fake Python in app/python."""
    launcher, fake_python = built
    folder = tmp_path / "Ninaivu portable"
    (folder / "app" / "python").mkdir(parents=True)
    shutil.copy(launcher, folder / "Ninaivu.exe")
    shutil.copy(fake_python, folder / "app" / "python" / "pythonw.exe")
    return folder


def _probe(folder: Path) -> dict:
    path = folder / "logs" / "probe.json"
    for _ in range(100):
        if path.is_file() and path.stat().st_size:
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(0.1)
    raise AssertionError("the launcher never started the program")


@needs_csc
def test_ninaivu_exe_starts_the_control_panel_with_the_portable_folders(portable):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NINAIVU_")}
    env["PYTHONHOME"] = r"C:\somewhere\else"
    subprocess.run([str(portable / "Ninaivu.exe"), "--hello", "two words"], env=env,
                   check=True, timeout=30)
    seen = _probe(portable)

    app = str(portable / "app")
    assert seen["NINAIVU_HOME"] == app
    assert seen["NINAIVU_STATE_DIR"] == str(portable / "app" / "data"), "the index travels with the folder"
    assert seen["NINAIVU_AI_MODELS_DIR"] == str(portable / "app" / "ai-models")
    assert seen["NINAIVU_LOG_DIR"] == str(portable / "logs"), "the log is where a person can find it"
    assert seen["NINAIVU_PORTABLE"] == "1"
    assert seen["PYTHONHOME"] is None, "another Python's settings must not steer the private one"
    assert seen["cwd"] == app
    assert seen["args"] == ["-m", "ninaivu.desktop.app", "--hello", "two words"]


@needs_csc
def test_running_it_leaves_nothing_else_at_the_top(portable):
    subprocess.run([str(portable / "Ninaivu.exe")], check=True, timeout=30)
    _probe(portable)
    assert sorted(p.name for p in portable.iterdir()) == ["Ninaivu.exe", "app", "logs"]
    assert (portable / "app" / "data").is_dir()


def test_the_build_script_holds_the_zip_to_three_things_at_its_top():
    script = (WINDOWS / "build-portable.ps1").read_text(encoding="utf-8")
    assert '"app,logs,Ninaivu.exe"' in script
    # Written entry by entry with "/" in every name, whichever PowerShell makes
    # it: the .NET Framework's CreateFromDirectory writes backslashes.
    assert "[IO.Compression.ZipFile]::CreateFromDirectory(" not in script
    assert "CreateEntry(" in script


def test_the_release_builds_checksums_and_attaches_the_portable_zip():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "build-portable.ps1" in workflow
    assert "installers/windows/build/portable/*.zip" in workflow, "uploaded from the Windows job"
    assert "windows/portable/*.zip" in workflow, "in SHA256SUMS.txt"
    assert "out/windows/portable/*.zip" in workflow, "attached to the release"


@needs_csc
def test_the_sign_in_start_goes_through_the_launcher_with_the_same_folders(portable):
    """Started bare at sign-in, the portable build would have used ~/.ninaivu
    and a root of app/pkgs: a second, empty Ninaivu."""
    subprocess.run([str(portable / "Ninaivu.exe"), "--autostart"], check=True, timeout=30)
    seen = _probe(portable)
    assert seen["args"] == ["-m", "ninaivu.desktop.autostart", "--start"]
    assert seen["NINAIVU_STATE_DIR"] == str(portable / "app" / "data")
    assert seen["NINAIVU_LOG_DIR"] == str(portable / "logs")


def test_the_portable_build_registers_the_launcher_to_start_at_sign_in(tmp_path, monkeypatch):
    from ninaivu.desktop import autostart

    (tmp_path / "app").mkdir()
    (tmp_path / "Ninaivu.exe").write_bytes(b"")
    monkeypatch.setenv("NINAIVU_PORTABLE", "1")
    monkeypatch.setenv("NINAIVU_HOME", str(tmp_path / "app"))
    assert autostart.command(tmp_path / "app", "win32") == [str(tmp_path / "Ninaivu.exe"), "--autostart"]

    # An installed Ninaivu is as it was.
    monkeypatch.delenv("NINAIVU_PORTABLE")
    assert autostart.command(tmp_path / "app", "win32")[-3:] == ["-m", "ninaivu.desktop.autostart", "--start"]


@needs_csc
def test_arguments_reach_the_program_as_they_were_typed(portable):
    """Backslashes are literal on a Windows command line except in front of a
    quote; doubling every one turned a path with a space into another path."""
    typed = ["C:\\a b", "\\\\nas\\share folder\\", 'say "hi"', "plain", "trailing\\"]
    subprocess.run([str(portable / "Ninaivu.exe"), *typed], check=True, timeout=30)
    assert _probe(portable)["args"][2:] == typed


@needs_csc
def test_extracted_at_the_root_of_a_drive_the_folders_are_still_right(built, tmp_path):
    """The root of a drive, trimmed of its slash, is "E:": the current folder
    on E:, and every path made from it points somewhere else. A USB stick is a
    plausible place to extract to."""
    letter = next((c for c in "ZYXWVUTSR" if not Path(f"{c}:\\").exists()), None)
    if letter is None:
        pytest.skip("no free drive letter")
    folder = tmp_path / "stick"
    (folder / "app" / "python").mkdir(parents=True)
    launcher, fake_python = built
    shutil.copy(launcher, folder / "Ninaivu.exe")
    shutil.copy(fake_python, folder / "app" / "python" / "pythonw.exe")
    done = subprocess.run(["subst", f"{letter}:", str(folder)], capture_output=True, text=True)
    if done.returncode:
        pytest.skip(f"subst is not available here: {done.stderr or done.stdout}")
    try:
        subprocess.run([f"{letter}:\\Ninaivu.exe"], check=True, timeout=30)
        seen = _probe(folder)
    finally:
        subprocess.run(["subst", f"{letter}:", "/d"], capture_output=True)
    assert seen["NINAIVU_HOME"] == f"{letter}:\\app"
    assert seen["NINAIVU_STATE_DIR"] == f"{letter}:\\app\\data"
    assert seen["NINAIVU_LOG_DIR"] == f"{letter}:\\logs"


def test_the_portable_sign_in_start_has_a_registry_value_of_its_own(monkeypatch):
    from ninaivu.desktop import autostart

    monkeypatch.delenv("NINAIVU_PORTABLE", raising=False)
    assert autostart.run_value() == "Ninaivu"
    monkeypatch.setenv("NINAIVU_PORTABLE", "1")
    assert autostart.run_value() == "Ninaivu (portable)"
    assert autostart.run_value() != autostart.RUN_VALUE


def test_the_server_page_names_the_restart_log_where_it_really_is(app, people):
    """The page used to name a file in .ninaivu-control whatever the log folder
    was, and the portable build keeps it in its own logs folder."""
    from conftest import ADMIN, login
    from ninaivu.api import server_api

    admin = login(app.test_client(), *ADMIN)
    state = admin.get("/api/admin/server").get_json()
    assert state["restart_log"] == str(server_api.RESTART_LOG)
    assert state["restart_log"].endswith("restart.log")
