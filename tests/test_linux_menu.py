"""One click on Linux: "Open Ninaivu" in the portable folder, and Ninaivu in
the application menu and on the Desktop (ninaivu/desktop/linux_menu.py)."""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ninaivu.desktop import linux_menu

ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "installers" / "linux"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux desktops")


def _portable(folder: Path, marker: Path) -> Path:
    (folder / "app").mkdir(parents=True)
    script = folder / "ninaivu"
    script.write_text(f'#!/bin/sh\nprintf "%s|%s|%s" "$0" "$PWD" "$*" > "{marker}"\n')
    script.chmod(0o755)
    return folder


@pytest.mark.parametrize("name", ["Ninaivu", "With space", "Dollar $HOME", "Quote 'q' \"d\"",
                                  "Photos 100%", "back\\slash", "பாட்டி"])
def test_the_menu_entry_opens_the_folder_it_was_made_for(tmp_path, name, monkeypatch):
    if not sys.platform.startswith("linux") or not shutil.which("gio"):
        # Homebrew's gio on a Mac has no "launch": desktop entries are Linux's.
        pytest.skip("gio launches desktop entries the way the desktop does")
    marker = tmp_path / "marker"
    folder = _portable(tmp_path / name, marker)
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert linux_menu.add(folder, out=lambda _: None) == 0
    entry = home / ".local" / "share" / "applications" / f"{linux_menu.PORTABLE_ID}.desktop"
    assert (home / "Desktop" / entry.name).read_text() == entry.read_text()
    assert (home / ".local/share/icons/hicolor/512x512/apps/ninaivu.png").is_file()
    subprocess.run(["gio", "launch", str(entry)], check=True, timeout=30, cwd="/")
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    script, workdir, _ = marker.read_text().split("|")
    assert script == str(folder / "ninaivu") and workdir == str(folder)


def test_a_checkout_gets_ninaivu_and_its_control_panel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    made = dict(linux_menu.launchers(ROOT))
    assert set(made) == {linux_menu.CHECKOUT_ID, linux_menu.PANEL_ID}
    assert "Terminal=true" in made[linux_menu.CHECKOUT_ID]
    assert "launcher/start.sh" in made[linux_menu.CHECKOUT_ID]
    assert "Start - Ninaivu Control Panel.sh" in made[linux_menu.PANEL_ID]
    start = (ROOT / "launcher" / "start.sh").read_text()
    assert "--add-to-menu) exec python3 ninaivu/desktop/linux_menu.py --add" in start


PANEL = ROOT / linux_menu.PANEL_SCRIPT


def _checkout(folder: Path) -> Path:
    """A checkout's top: the Control Panel script, and nothing set up yet."""
    folder.mkdir(parents=True)
    shutil.copyfile(PANEL, folder / PANEL.name)
    return folder


def test_the_control_panel_script_runs_the_venv_from_anywhere(tmp_path):
    """Double-clicked, the file manager starts it from somewhere else."""
    marker = tmp_path / "marker"
    folder = _checkout(tmp_path / "My Photos 'x' $HOME")
    venv = folder / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(f'#!/bin/sh\nprintf "%s|%s" "$1" "$PWD" > "{marker}"\n')
    (venv / "python").chmod(0o755)
    subprocess.run(["sh", str(folder / PANEL.name)], check=True, cwd="/", timeout=30)
    assert marker.read_text() == f"{folder / 'ninaivu_control.pyw'}|{folder}"


def test_the_control_panel_script_says_to_set_up_first(tmp_path):
    folder = _checkout(tmp_path / "Ninaivu")
    done = subprocess.run(["sh", str(folder / PANEL.name)], cwd="/", capture_output=True, text=True,
                          timeout=30, env={"PATH": "/usr/bin:/bin", "NINAIVU_NO_DIALOG": "1"})
    assert done.returncode == 1
    assert "not set up in this folder yet" in done.stderr and "sh launcher/start.sh" in done.stderr


def test_the_control_panel_script_sits_beside_the_windows_one():
    assert (ROOT / "Start - Ninaivu Control Panel.vbs").is_file()
    assert PANEL.read_bytes().startswith(b"#!/bin/sh\n"), "LF line endings, or sh cannot run it"
    assert "ninaivu_control.pyw" in PANEL.read_text()


def test_removing_takes_out_only_its_own(tmp_path, monkeypatch):
    home = tmp_path / "home"
    menu = home / ".local" / "share" / "applications"
    menu.mkdir(parents=True)
    (menu / "firefox.desktop").write_text("[Desktop Entry]\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    linux_menu.add(_portable(tmp_path / "Ninaivu", tmp_path / "m"), out=lambda _: None)
    linux_menu.remove(out=lambda _: None)
    assert sorted(p.name for p in menu.iterdir()) == ["firefox.desktop"]


def test_open_ninaivu_runs_the_script_beside_it_from_anywhere(tmp_path):
    cc = shutil.which("cc")
    if not cc or not sys.platform.startswith("linux"):
        pytest.skip("needs a C compiler on Linux")
    marker = tmp_path / "marker"
    folder = _portable(tmp_path / "My Photos 'x'", marker)
    program = folder / "Open Ninaivu"
    subprocess.run([cc, "-O2", "-o", str(program), str(LINUX / "portable" / "open-ninaivu.c")], check=True)
    subprocess.run([str(program), "a b", "c"], check=True, cwd="/", timeout=30)
    assert marker.read_text() == f"{folder / 'ninaivu'}|{folder}|a b c"


def test_the_linux_build_makes_and_checks_open_ninaivu():
    build = (LINUX / "build.sh").read_text()
    script = (LINUX / "portable" / "ninaivu").read_text()
    assert 'cc -O2 -static -s -o "$stage/Open Ninaivu"' in build
    assert '"$stage/Open Ninaivu" --where' in build
    assert "--add-to-menu) exec \"$python\" -m ninaivu.desktop.linux_menu --add" in script
