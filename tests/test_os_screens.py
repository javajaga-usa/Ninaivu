"""What each screen shows on macOS, Windows and Linux.

The Control Panel on the portable Linux download runs on a Tk built without
Xft (python-build-standalone's), which has X's core fonts only and no
fallback to another face: the shapes on its buttons and its curly quotes came
out as boxes. Linux desktops other than GNOME keep dark mode and scaling in
their own places. The console's hints showed Windows and Mac paths on every
computer, and only the Windows build carried a Tamil font.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("tkinter")
from ninaivu.desktop import app, theme  # noqa: E402


# ---------------------------------------------------------------------------
# The Control Panel on a Tk with core fonts only
# ---------------------------------------------------------------------------

def test_core_fonts_are_told_from_xft_by_the_tk_itself():
    def root(answer):
        return SimpleNamespace(tk=SimpleNamespace(call=lambda *a: answer))
    assert app.core_fonts_only(root("x11")) is True
    assert app.core_fonts_only(root("xft")) is False

    def no_such(*a):
        raise app.tk.TclError("no pkgconfig")       # Windows and macOS have none
    assert app.core_fonts_only(SimpleNamespace(tk=SimpleNamespace(call=no_such))) is False


def test_core_fonts_get_plain_letters_and_nothing_else_changes():
    assert app.glyphs('▶  Start', plain=True) == 'Start'
    assert app.glyphs('●  Running', plain=True) == 'Running'
    assert app.glyphs('○  Stopped', plain=True) == 'Stopped'
    assert app.glyphs('✕ Hide', plain=True) == 'Hide'
    assert app.glyphs('Checking server…', plain=True) == 'Checking server...'
    assert app.glyphs('The two passwords don’t match.', plain=True) == "The two passwords don't match."
    assert app.glyphs('•', plain=True) == '*'
    # Every letter left is one a core font has.
    for _fancy, simple in app.PLAIN:
        assert all(ord(c) < 128 for c in simple)
    assert app.glyphs('▶  Start', plain=False) == '▶  Start'
    assert app.glyphs('', plain=True) == ''


def test_the_panel_says_its_lines_in_letters_its_fonts_have(monkeypatch):
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    try:
        monkeypatch.setattr(app, "_plain", True)
        line = app.Words(root, value='Checking server…')
        assert line.get() == 'Checking server...'
        line.set('●  Running')
        assert line.get() == 'Running'
        monkeypatch.setattr(app, "_plain", False)
        line.set('●  Running')
        assert line.get() == '●  Running'
    finally:
        root.destroy()


def test_a_password_box_is_never_a_line_the_panel_rewrites():
    source = (ROOT / "ninaivu" / "desktop" / "app.py").read_text(encoding="utf-8")
    assert "first, again = tk.StringVar(), tk.StringVar()" in source
    assert "show=glyphs('•')" in source


# ---------------------------------------------------------------------------
# Scaling
# ---------------------------------------------------------------------------

def _xrdb(text):
    return lambda *a, **k: SimpleNamespace(stdout=text, returncode=0)


def test_a_linux_desktop_scaled_up_says_so_in_xft_dpi():
    assert app.desktop_dpi("linux", run=_xrdb("Xft.antialias:\t1\nXft.dpi:\t192\n")) == 192.0
    assert app.desktop_dpi("linux", run=_xrdb("Xft.dpi:\t96\n")) is None       # Tk's own
    assert app.desktop_dpi("linux", run=_xrdb("")) is None
    assert app.desktop_dpi("linux", run=_xrdb("Xft.dpi: lots\n")) is None
    assert app.desktop_dpi("darwin", run=_xrdb("Xft.dpi:\t192\n")) is None
    assert app.desktop_dpi("win32", run=_xrdb("Xft.dpi:\t192\n")) is None

    def missing(*a, **k):
        raise FileNotFoundError("xrdb")
    assert app.desktop_dpi("linux", run=missing) is None


def test_windows_before_1703_is_still_told_the_panel_is_dpi_aware():
    calls = []

    def absent():
        raise AttributeError("no such function")

    def took(name, answer):
        def call(*a):
            calls.append(name)
            return answer
        return call

    new = SimpleNamespace(user32=SimpleNamespace(SetProcessDpiAwarenessContext=took("context", 1)))
    assert app.windows_dpi_aware(new) is True and calls == ["context"]

    calls.clear()
    eight = SimpleNamespace(user32=SimpleNamespace(SetProcessDpiAwarenessContext=lambda *a: absent(),
                                                   SetProcessDPIAware=took("system", 1)),
                            shcore=SimpleNamespace(SetProcessDpiAwareness=took("monitor", 0)))
    assert app.windows_dpi_aware(eight) is True and calls == ["monitor"]

    calls.clear()
    vista = SimpleNamespace(user32=SimpleNamespace(SetProcessDpiAwarenessContext=lambda *a: absent(),
                                                   SetProcessDPIAware=took("system", 1)))
    assert app.windows_dpi_aware(vista) is True and calls == ["system"]


# ---------------------------------------------------------------------------
# Dark mode on Linux desktops that are not GNOME
# ---------------------------------------------------------------------------

def _answers(by_program):
    def run(command, **kwargs):
        answer = by_program.get(command[0])
        if answer is None:
            raise FileNotFoundError(command[0])
        return SimpleNamespace(stdout=answer, returncode=0)
    return run


def test_xfce_says_dark_in_its_own_settings(tmp_path):
    # MX Linux and Xubuntu: gsettings is there, but GNOME's keys stay at default.
    run = _answers({"gsettings": "'default'\n", "xfconf-query": "Greybird-dark\n"})
    assert theme.system_prefers_dark("linux", run=run, environ={"HOME": str(tmp_path)}) is True
    run = _answers({"gsettings": "'default'\n", "xfconf-query": "Greybird\n"})
    assert theme.system_prefers_dark("linux", run=run, environ={"HOME": str(tmp_path)}) is False


def test_kde_and_a_theme_named_in_the_environment_say_dark(tmp_path):
    nothing = _answers({})
    assert theme.system_prefers_dark("linux", run=nothing, environ={"GTK_THEME": "Adwaita:dark"}) is True
    (tmp_path / "kdeglobals").write_text("[General]\nColorScheme=BreezeDark\n", encoding="utf-8")
    assert theme.system_prefers_dark("linux", run=nothing, environ={"XDG_CONFIG_HOME": str(tmp_path)}) is True
    (tmp_path / "kdeglobals").write_text("[General]\nColorScheme=BreezeLight\n", encoding="utf-8")
    assert theme.system_prefers_dark("linux", run=nothing, environ={"XDG_CONFIG_HOME": str(tmp_path)}) is False


# ---------------------------------------------------------------------------
# The console's sample paths, and the system-folder refusal
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_sample_paths_are_written_the_way_the_server_writes_them():
    script = (
        "import { samplePaths } from %s;\n"
        "const out = {};\n"
        "for (const host of ['darwin', 'linux', 'win32', 'freebsd']) {\n"
        "  out[host] = ['e.g. D:\\\\OldPhotos or /Volumes/Backup', 'e.g. D:\\\\Photo Archive',\n"
        "    '…or type a path, e.g. C:\\\\Users\\\\you\\\\Pictures', '/Volumes/Backup/Ninaivu',\n"
        "    '/mnt/away-disk', '/Volumes/USB'].map((t) => samplePaths(t, host));\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    ) % json.dumps((ROOT / "ninaivu" / "static" / "js" / "i18n.js").as_uri())
    done = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                          capture_output=True, text=True, timeout=60, check=True)
    out = json.loads(done.stdout)
    assert out["linux"][:3] == ["e.g. /media/you/OldPhotos or /media/you/Backup",
                                "e.g. /media/you/Photo Archive",
                                "…or type a path, e.g. /home/you/Pictures"]
    assert out["darwin"][2] == "…or type a path, e.g. /Users/you/Pictures"
    assert out["darwin"][4] == "/Volumes/Away"
    assert out["win32"][3:] == ["E:\\Backup\\Ninaivu", "E:\\Away", "E:\\"]
    assert out["win32"][2].endswith("C:\\Users\\you\\Pictures")
    # A computer it does not know keeps the hint as written.
    assert out["freebsd"][1] == "e.g. D:\\Photo Archive"
    # Each Windows sample swapped on the others is in the markup or a script,
    # so none of the table is dead.
    pages = (ROOT / "ninaivu" / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "D:\\Photo Archive" in pages and "E:\\Restored" in pages


def test_the_pages_say_which_computer_the_folders_are_on(client):
    import sys
    page = client.get("/").get_data(as_text=True)
    host = "win32" if sys.platform.startswith("win") else sys.platform
    assert f'data-host="{host}"' in page


def test_the_system_folder_refusal_uses_this_computers_paths():
    import sys
    from ninaivu.api import api_library
    example = api_library._MEDIA_EXAMPLE
    if sys.platform.startswith("win"):
        assert example.startswith("C:\\Users\\you\\Pictures")
    else:
        assert "C:\\" not in example and example.endswith("rather than /")


# ---------------------------------------------------------------------------
# Tamil on Linux
# ---------------------------------------------------------------------------

def test_the_linux_build_carries_the_same_tamil_font_as_windows():
    linux = (ROOT / "installers" / "linux" / "build.sh").read_text(encoding="utf-8")
    windows = (ROOT / "installers" / "windows" / "build.ps1").read_text(encoding="utf-8")
    commit = re.search(r'\$fontCommit = "([0-9a-f]{40})"', windows).group(1)
    assert f"google/fonts/{commit}/ofl/notosanstamil" in linux
    for digest in re.findall(r'Sha256 = "([0-9a-f]{64})"', windows):
        assert digest in linux
    assert "NotoSansTamil.ttf" in linux and "NotoSansTamil-OFL.txt" in linux
    # Before the wheel is built, or the wheel would not carry it.
    assert linux.index("NotoSansTamil.ttf") < linux.index("pip wheel")


def test_a_line_that_comes_later_is_not_cut_off_the_bottom(tmp_path, monkeypatch):
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    from ninaivu.desktop.control import Controller
    monkeypatch.setattr(Controller, "record", lambda _: None)
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: False)
    monkeypatch.setattr(theme, "system_prefers_dark", lambda platform=None, run=None, environ=None: False)
    controller = Controller(root=tmp_path, cfg=SimpleNamespace(
        state_dir=tmp_path / "state", host="127.0.0.1", port=443, admin_port=3000,
        ai_engine="off", network_access=False))
    dashboard = None
    try:
        dashboard = app.Dashboard(root, controller)
        root.update()
        # The divided pane went on asking for the height it was laid out at.
        dashboard.notice.set("A notice long enough to wrap. " * 40)
        root.update_idletasks()
        upper = dashboard.upper_pane
        assert dashboard._wanted_height() >= root.winfo_reqheight()
        dashboard._grow_to_fit()
        root.update()
        if upper.winfo_reqheight() + 120 < root.winfo_screenheight():
            assert upper.winfo_height() >= upper.winfo_reqheight()
    finally:
        if dashboard is not None:
            dashboard.finished.set()
        root.destroy()
