"""The Linux and Mac portable launchers, run: with a stand-in Python.

The Windows one (Ninaivu.exe) is run in test_portable_build.py. These run
installers/linux/portable/ninaivu and installers/macos/launcher.sh with sh, in
a folder whose name has a space, a quote, a $ and a % in it, and check that
the Python they start is the folder's own, told the folder's paths whole, and
handed the arguments as they were given — and that the Mac app keeps to
Application Support when it is not in a portable folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "installers" / "linux" / "portable" / "ninaivu"
MAC = ROOT / "installers" / "macos" / "launcher.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                                reason="runs the launchers with sh")

#: Stands in for the bundled python3: writes what it was started with.
FAKE_PYTHON = """#!/bin/sh
{
for name in NINAIVU_HOME NINAIVU_STATE_DIR NINAIVU_AI_MODELS_DIR NINAIVU_LOG_DIR \\
            NINAIVU_PORTABLE NINAIVU_PYTHON PYTHONHOME PYTHONPATH PYTHONDONTWRITEBYTECODE \\
            PYTHONNOUSERSITE; do
    eval "printf '%s=%s\\\\n' $name \\"\\${$name-<unset>}\\""
done
for a in "$@"; do printf 'ARG=%s\\n' "$a"; done
} > "$RECORD"
"""

AWKWARD = "My Apps/O'Neil $HOME `x` 50%"


def _fake_python(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_PYTHON, newline="\n")
    path.chmod(0o755)


def _run(program: Path, *args: str, record: Path, home: Path) -> dict[str, list[str] | str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "RECORD": str(record),
           "PYTHONHOME": "/elsewhere", "PYTHONPATH": "/elsewhere", "NINAIVU_NO_DIALOG": "1"}
    done = subprocess.run(["sh", str(program), *args], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    seen: dict[str, list[str] | str] = {"ARG": []}
    for line in record.read_text().splitlines():
        key, _, value = line.partition("=")
        if key == "ARG":
            seen["ARG"].append(value)            # type: ignore[union-attr]
        else:
            seen[key] = value
    return seen


@pytest.fixture()
def linux_folder(tmp_path):
    folder = tmp_path / AWKWARD / "Ninaivu-1.0.0-linux-x64-portable"
    _fake_python(folder / "app" / "python" / "bin" / "python3")
    shutil.copy(LINUX, folder / "ninaivu")
    (folder / "ninaivu").chmod(0o755)
    return folder


def test_the_linux_folder_keeps_everything_in_itself(linux_folder, tmp_path):
    record = tmp_path / "record"
    seen = _run(linux_folder / "ninaivu", record=record, home=tmp_path / "home")
    app = linux_folder.resolve() / "app"
    data = linux_folder.resolve() / "data"
    # What it keeps is apart from the program (desktop/portable.py).
    assert seen["NINAIVU_HOME"] == str(data / "control")
    assert seen["NINAIVU_STATE_DIR"] == str(data)
    assert seen["NINAIVU_AI_MODELS_DIR"] == str(data / "ai-models")
    assert seen["NINAIVU_LOG_DIR"] == str(linux_folder.resolve() / "logs")
    assert seen["NINAIVU_PORTABLE"] == "1"
    assert seen["NINAIVU_PYTHON"] == str(app / "python" / "bin" / "python3")
    assert seen["PYTHONHOME"] == "<unset>" and seen["PYTHONPATH"] == "<unset>"
    assert seen["PYTHONNOUSERSITE"] == "1", "no pip --user packages from the home folder"
    assert seen["ARG"] == ["-m", "ninaivu.desktop.app"], "the Control Panel opens"
    for made in ("logs", "data/control", "data/ai-models"):
        assert (linux_folder / made).is_dir(), made
    assert not (tmp_path / "home").exists(), "nothing written outside the folder"


def test_the_linux_server_takes_its_arguments_whole(linux_folder, tmp_path):
    record = tmp_path / "record"
    photos = str(tmp_path / AWKWARD / "Family photos")
    seen = _run(linux_folder / "ninaivu", "--server", photos, "--port", "8080",
                record=record, home=tmp_path / "home")
    assert seen["ARG"] == ["-m", "ninaivu", photos, "--port", "8080"]
    seen = _run(linux_folder / "ninaivu", "--tray", record=record, home=tmp_path / "home")
    assert seen["ARG"] == ["-m", "ninaivu.desktop.tray"]


def test_the_linux_folder_brings_a_library_across_without_a_desktop(linux_folder, tmp_path):
    """What the Control Panel offers after an update, for a machine over ssh."""
    older = str(tmp_path / AWKWARD / "Ninaivu-0.9.0-linux-x64-portable")
    seen = _run(linux_folder / "ninaivu", "--bring", older, record=tmp_path / "record",
                home=tmp_path / "home")
    assert seen["ARG"] == ["-m", "ninaivu.desktop.portable", "--bring", older]
    assert seen["NINAIVU_PORTABLE"] == "1"
    seen = _run(linux_folder / "ninaivu", "--go-back", record=tmp_path / "record",
                home=tmp_path / "home")
    assert seen["ARG"] == ["-m", "ninaivu.desktop.portable", "--go-back"]


def test_the_linux_folder_works_through_a_link_to_its_script(linux_folder, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ninaivu").symlink_to(linux_folder / "ninaivu")
    seen = _run(bin_dir / "ninaivu", record=tmp_path / "record", home=tmp_path / "home")
    assert seen["NINAIVU_STATE_DIR"] == str(linux_folder.resolve() / "data")


def test_the_linux_folder_says_so_when_its_program_is_missing(tmp_path):
    folder = tmp_path / "lonely"
    folder.mkdir()
    shutil.copy(LINUX, folder / "ninaivu")
    done = subprocess.run(["sh", str(folder / "ninaivu")], capture_output=True, text=True, timeout=60,
                          env={"NINAIVU_NO_DIALOG": "1", "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)})
    assert done.returncode == 1
    assert "Extract the whole archive" in done.stderr


def _mac_app(folder: Path) -> Path:
    contents = folder / "Ninaivu.app" / "Contents"
    _fake_python(contents / "Resources" / "python" / "bin" / "python3")
    (contents / "MacOS").mkdir(parents=True)
    shutil.copy(MAC, contents / "MacOS" / "ninaivu")
    shutil.copy(ROOT / "installers" / "macos" / "original_path.py", contents / "Resources")
    return contents / "MacOS" / "ninaivu"


def test_the_mac_app_in_its_portable_folder_keeps_everything_there(tmp_path):
    folder = tmp_path / AWKWARD / "Ninaivu-1.0.0-macos-arm64-portable"
    launcher = _mac_app(folder)
    (folder / ".ninaivu-portable").write_text("portable\n")
    seen = _run(launcher, record=tmp_path / "record", home=tmp_path / "home")
    real = folder.resolve()
    assert seen["NINAIVU_HOME"] == str(real / "data" / "control")
    assert seen["NINAIVU_STATE_DIR"] == str(real / "data")
    assert seen["NINAIVU_AI_MODELS_DIR"] == str(real / "data" / "ai-models")
    assert seen["NINAIVU_LOG_DIR"] == str(real / "logs")
    assert seen["NINAIVU_PORTABLE"] == "1"
    assert seen["PYTHONHOME"] == "<unset>"
    assert seen["ARG"] == ["-m", "ninaivu.desktop.app"]
    assert not (tmp_path / "home").exists(), "nothing written outside the folder"
    seen = _run(launcher, "--tray", record=tmp_path / "record", home=tmp_path / "home")
    assert seen["ARG"] == ["-m", "ninaivu.desktop.tray"]
    older = str(tmp_path / AWKWARD / "Ninaivu-0.9.0-macos-arm64-portable")
    seen = _run(launcher, "--bring", older, record=tmp_path / "record", home=tmp_path / "home")
    assert seen["ARG"] == ["-m", "ninaivu.desktop.portable", "--bring", older]
    seen = _run(launcher, "--go-back", record=tmp_path / "record", home=tmp_path / "home")
    assert seen["ARG"] == ["-m", "ninaivu.desktop.portable", "--go-back"]


def test_the_mac_app_never_writes_into_itself(tmp_path):
    """A signed app that changes is no longer the app Apple notarised: the
    launcher keeps Python from caching modules inside Ninaivu.app, and the
    build compiles them before the app is signed."""
    seen = _run(_mac_app(tmp_path / "Applications"), record=tmp_path / "record", home=tmp_path / "home")
    assert seen["PYTHONDONTWRITEBYTECODE"] == "1"
    build = (ROOT / "installers" / "macos" / "build.sh").read_text(encoding="utf-8")
    compiled = build.index("-m compileall")
    assert compiled < build.index("codesign --force --options runtime"), "compiled before signing"
    assert "--invalidation-mode unchecked-hash" in build


def test_the_mac_app_on_its_own_keeps_to_application_support(tmp_path):
    launcher = _mac_app(tmp_path / "Applications")
    home = tmp_path / "home"
    seen = _run(launcher, record=tmp_path / "record", home=home)
    assert seen["NINAIVU_HOME"] == str(home / "Library" / "Application Support" / "Ninaivu")
    assert seen["NINAIVU_STATE_DIR"] == "<unset>" and seen["NINAIVU_PORTABLE"] == "<unset>"
    assert not (tmp_path / "Applications" / "data").exists()


def test_the_mac_app_refuses_to_start_from_a_translocated_copy(tmp_path):
    """Run from macOS's hidden copy it cannot see its folder, and would have
    opened an empty Ninaivu in Application Support instead. (Without
    NINAIVU_NO_DIALOG it also shows an alert, which on a Mac waits for a click:
    the first run of this test on a Mac runner sat there until it timed out.)"""
    folder = tmp_path / "AppTranslocation" / "1234" / "d"
    launcher = _mac_app(folder)
    done = subprocess.run(["sh", str(launcher)], capture_output=True, text=True, timeout=60,
                          env={"NINAIVU_NO_DIALOG": "1", "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "home"),
                               "RECORD": str(tmp_path / "record")})
    assert done.returncode == 1
    assert "xattr -dr com.apple.quarantine" in done.stderr
    # The app's Python was asked only where the copy came from (it could not
    # say), never started as Ninaivu.
    record = tmp_path / "record"
    if record.exists():
        args = [line[4:] for line in record.read_text().splitlines() if line.startswith("ARG=")]
        assert args[0].endswith("original_path.py") and "ninaivu.desktop.app" not in args, args


@pytest.mark.skipif(sys.platform != "darwin", reason="asks macOS's Security framework")
def test_the_mac_helper_answers_for_a_folder_that_was_not_translocated(tmp_path):
    """SecTranslocateCreateOriginalPathForURL, through ctypes, on a real Mac:
    the symbols load and an ordinary folder comes back as itself (or nothing),
    never a crash."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "original_path", ROOT / "installers" / "macos" / "original_path.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = tmp_path / "Ninaivu.app"
    folder.mkdir()
    answer = module.original_path(str(folder))
    assert answer is None or Path(answer).resolve() == folder.resolve()
