"""The family app, the console and the Control Panel on the Desktop: put there
the first time, kept pointing at this Ninaivu, never put back once deleted,
and never written over somebody else's file. Nothing here needs a display."""
from __future__ import annotations

import plistlib
import sys
import threading
import types

import pytest

from ninaivu.desktop import app, instance, shortcuts

PANEL = shortcuts.FACES[2]


def mac_program(tmp_path, folder="Ninaivu 4.0"):
    bundle = tmp_path / folder / "Ninaivu.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    return shortcuts.Program((), bundle.parent, bundle)


@pytest.fixture
def desktop(tmp_path):
    folder = tmp_path / "Desktop"
    folder.mkdir()
    return folder


# -- what each shortcut opens -----------------------------------------------------------

def test_each_face_opens_the_one_program_with_its_page(tmp_path):
    names = [face.name for face in shortcuts.FACES]
    assert names == ["Ninaivu", "Ninaivu Console", "Ninaivu Control Panel"]
    assert [face.args for face in shortcuts.FACES] == [("--open", "family"), ("--open", "console"), ()]
    for face in shortcuts.FACES:
        assert face.icon.is_file(), face.icon
    assert len({face.icon for face in shortcuts.FACES}) == 3, "an icon each"


def test_the_program_is_found_from_what_the_starter_said(tmp_path):
    exe = tmp_path / "Ninaivu" / "Ninaivu.exe"
    found = shortcuts.program({"NINAIVU_LAUNCHER": str(exe)}, "win32", root=tmp_path)
    assert found.argv == (str(exe),) and found.workdir == exe.parent

    bundle = tmp_path / "Ninaivu" / "Ninaivu.app"
    found = shortcuts.program({"NINAIVU_LAUNCHER": str(bundle / "Contents" / "MacOS" / "ninaivu")},
                              "darwin", root=tmp_path)
    assert found.bundle == bundle
    # An installed Mac app does not say NINAIVU_LAUNCHER; its Python is inside it.
    found = shortcuts.program({"NINAIVU_PYTHON": str(bundle / "Contents/Resources/python/bin/python3")},
                              "darwin", root=tmp_path)
    assert found.bundle == bundle

    portable = tmp_path / "ninaivu-linux"
    (portable / "app").mkdir(parents=True)
    (portable / "app" / "ninaivu.sh").write_text("")
    found = shortcuts.program({"NINAIVU_PORTABLE_ROOT": str(portable)}, "linux", root=tmp_path)
    assert found.argv == ("sh", str(portable / "app" / "ninaivu.sh"))

    # A Windows checkout: the panel's own double-click, without a console window.
    found = shortcuts.program({"NINAIVU_PYTHON": r"C:\n\.venv\Scripts\python.exe"}, "win32", root=tmp_path)
    assert found.argv[0].endswith("pythonw.exe") and found.argv[1].endswith("ninaivu_control.pyw")


def test_a_mac_shortcut_opens_a_second_ninaivu_that_hands_over(tmp_path):
    prog = mac_program(tmp_path)
    script = shortcuts.mac_script(shortcuts.FACES[0], prog)
    assert "exec /usr/bin/open -n -a" in script and "--args --open family" in script
    assert "'" + str(prog.bundle) + "'" in script, "a folder name with a space is quoted"
    assert "--args" not in shortcuts.mac_script(PANEL, prog)


def test_a_windows_shortcut_is_one_powershell_run(tmp_path):
    prog = shortcuts.Program((r"C:\Users\Jaga's\Ninaivu\Ninaivu.exe",), tmp_path)
    made = [(tmp_path / f"{face.name}.lnk", face, prog, tmp_path / f"{face.key}.ico")
            for face in shortcuts.FACES]
    script = shortcuts.windows_script(made)
    assert script.count("$link.Save()") == 3
    assert "Jaga''s" in script, "a quote in a path is doubled for PowerShell"
    assert "$link.Arguments = '--open console'" in script
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs["input"]))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    shortcuts.write_windows(made, run)
    assert seen[0][0][0] == "powershell" and seen[0][1] == script
    with pytest.raises(OSError, match="denied"):
        shortcuts.write_windows(made, lambda argv, **k: types.SimpleNamespace(returncode=1, stdout="",
                                                                               stderr="denied"))


# -- placed once, kept right, never put back ---------------------------------------------

def test_the_first_time_puts_all_three_on_a_mac_desktop(tmp_path, desktop):
    control = tmp_path / "control"
    prog = mac_program(tmp_path)
    names = shortcuts.settle(control, platform="darwin", desktop=desktop, prog=prog)
    assert names == ["Ninaivu", "Ninaivu Console", "Ninaivu Control Panel"]
    family = desktop / "Ninaivu.app" / "Contents"
    info = plistlib.loads((family / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "open-ninaivu" and info["CFBundleIconFile"] == "icon"
    assert (family / "MacOS" / "open-ninaivu").stat().st_mode & 0o111
    assert (family / "Resources" / "icon.icns").read_bytes()[:4] == b"icns"
    assert shortcuts.settle(control, platform="darwin", desktop=desktop, prog=prog) == [], \
        "the second time there is nothing to do"


def test_a_deleted_shortcut_is_not_put_back(tmp_path, desktop):
    import shutil
    control = tmp_path / "control"
    prog = mac_program(tmp_path)
    shortcuts.settle(control, platform="darwin", desktop=desktop, prog=prog)
    shutil.rmtree(desktop / "Ninaivu Console.app")
    assert shortcuts.settle(control, platform="darwin", desktop=desktop, prog=prog) == []
    assert not (desktop / "Ninaivu Console.app").exists()
    # --add puts it back when asked.
    assert shortcuts.settle(control, platform="darwin", desktop=desktop, prog=prog, force=True) \
        == ["Ninaivu Console"]
    assert (desktop / "Ninaivu Console.app").is_dir()


def test_a_newer_copy_takes_over_the_shortcuts_still_there(tmp_path, desktop):
    control = tmp_path / "control"
    shortcuts.settle(control, platform="darwin", desktop=desktop, prog=mac_program(tmp_path, "Ninaivu 4.0"))
    newer = mac_program(tmp_path, "Ninaivu 4.1")
    assert shortcuts.settle(control, platform="darwin", desktop=desktop, prog=newer) == \
        ["Ninaivu", "Ninaivu Console", "Ninaivu Control Panel"]
    script = (desktop / "Ninaivu.app" / "Contents" / "MacOS" / "open-ninaivu").read_text()
    assert "Ninaivu 4.1" in script and "Ninaivu 4.0" not in script


def test_somebody_elses_file_under_the_name_is_left_alone(tmp_path, desktop):
    theirs = desktop / "Ninaivu.app"
    (theirs / "Contents").mkdir(parents=True)
    (theirs / "Contents" / "Info.plist").write_text("the real one, dragged here")
    names = shortcuts.settle(tmp_path / "control", platform="darwin", desktop=desktop,
                             prog=mac_program(tmp_path))
    assert "Ninaivu" not in names and len(names) == 2
    assert (theirs / "Contents" / "Info.plist").read_text() == "the real one, dragged here"
    shortcuts.remove(tmp_path / "control", platform="darwin")
    assert theirs.is_dir(), "and never removed"
    assert not (desktop / "Ninaivu Console.app").exists()


def test_no_desktop_is_remembered_as_done(tmp_path, monkeypatch):
    control = tmp_path / "control"
    monkeypatch.setattr(shortcuts, "desktop_folder", lambda environ=None, platform=None: None)
    assert shortcuts.settle(control, platform="darwin", prog=mac_program(tmp_path)) == []
    assert shortcuts.read_record(control)["done"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_on_linux_each_is_a_trusted_desktop_entry_with_its_icon(tmp_path, desktop, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-gio"))
    prog = shortcuts.Program(("sh", str(tmp_path / "Ninaivu Folder" / "app" / "ninaivu.sh")),
                             tmp_path / "Ninaivu Folder")
    shortcuts.settle(tmp_path / "control", platform="linux", desktop=desktop, prog=prog)
    family = (desktop / "local.ninaivu.family.desktop").read_text()
    assert "Name=Ninaivu\n" in family
    assert 'Exec=sh "' in family and family.rstrip().endswith("X-Ninaivu-Shortcut=family")
    assert "--open family" in family
    assert f"Icon={shortcuts.FACES[0].icon}" in family
    assert (desktop / "local.ninaivu.panel.desktop").stat().st_mode & 0o111
    assert shortcuts.remove(tmp_path / "control", platform="linux") == \
        ["Ninaivu", "Ninaivu Console", "Ninaivu Control Panel"]
    assert list(desktop.iterdir()) == []


def test_the_release_checks_can_turn_it_off(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_DESKTOP_SHORTCUTS", "0")
    monkeypatch.setattr(shortcuts, "settle", lambda *a, **k: pytest.fail("not placed"))
    assert shortcuts.settle_quietly(tmp_path) == []


def test_a_failure_never_stops_the_panel(tmp_path, monkeypatch):
    monkeypatch.delenv("NINAIVU_DESKTOP_SHORTCUTS", raising=False)
    monkeypatch.setattr(shortcuts, "settle", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    assert shortcuts.settle_quietly(tmp_path) == []


# -- a shortcut opening while Ninaivu is open -------------------------------------------

def test_a_second_ninaivu_opened_for_a_page_hands_it_to_the_first(tmp_path):
    first = instance.claim(tmp_path)
    asked, shown = [], threading.Event()
    opened = threading.Event()
    first.serve(shown.set, lambda which: (asked.append(which), opened.set()))
    try:
        assert instance.claim(tmp_path, patience=2, say=b"console") is None
        assert opened.wait(5) and asked == ["console"]
        assert not shown.is_set(), "the page, not the window"
        assert instance.claim(tmp_path, patience=2) is None
        assert shown.wait(5)
        assert instance.tell_to_show(tmp_path, say=b"rm -rf") is False, "only the two pages"
    finally:
        first.close()


def test_the_option_and_what_it_tells_the_one_already_open(monkeypatch):
    assert app.parse_arguments(["--open", "family"]).open == "family"
    assert app.parse_arguments([]).open is None
    told = []
    monkeypatch.setattr(instance, "claim", lambda folder, say: told.append(say))
    app.claim_the_desktop(say="console")
    app.claim_the_desktop()
    assert told == [b"console", instance.SHOW]
