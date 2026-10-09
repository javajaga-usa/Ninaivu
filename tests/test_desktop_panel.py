"""The Control Panel window (ninaivu/desktop/app.py) and its launchers.

Most of it is Tk, which needs a display; what is tested here is everything
that is not: the certificate steps it shares with the tray, the numbers it
shows under each resource mode, the running server's mode it follows, and
what it says when it cannot open at all. The window itself is built once, when
there is a display to build it on.
"""

import importlib.util
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The module imports tkinter at the top; a Python without Tk cannot test it.
pytest.importorskip("tkinter")
from ninaivu.desktop import app  # noqa: E402


# ---------------------------------------------------------------------------
# Trusting the certificate: the tray's steps, the panel's dialogs
# ---------------------------------------------------------------------------

@pytest.fixture()
def ninaivu_ca(tmp_path):
    from ninaivu.utils import tls
    tls.ensure_certificate(tmp_path)
    return tls.ca_certificate_path(tmp_path)


def _dashboard_for(state_dir, monkeypatch, platform="win32", arguments=()):
    dashboard = app.Dashboard.__new__(app.Dashboard)
    dashboard.root = None
    dashboard.controller = SimpleNamespace(settings={"arguments": list(arguments)},
                                           cfg=SimpleNamespace(state_dir=state_dir))
    notices = []
    dashboard.notice = SimpleNamespace(set=notices.append)
    # sys.platform rather than os.name: pathlib reads os.name, and set to "nt"
    # on a Mac or Linux it builds Windows paths that cannot exist there.
    monkeypatch.setattr(sys, "platform", platform)
    return dashboard, notices


def test_a_custom_certificate_does_not_open_the_old_local_ca(tmp_path, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch, arguments=["--cert=custom.pem"])
    monkeypatch.setattr(tls, "ca_certificate_path", lambda *_: pytest.fail("Must not open an unrelated CA"))
    dashboard.open_certificate()
    assert "custom certificate" in notices[0]


def test_it_trusts_after_asking_and_says_so(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(app.messagebox, "askyesno", lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(tls, "trust_ca_on_windows", lambda path: (True, "Installed in Trusted Root."))
    opened = []
    monkeypatch.setattr("ninaivu.desktop.tray.open_file", opened.append)
    dashboard.open_certificate()
    assert asked, "it trusted without asking"
    assert opened == [], "no certificate window when trusting worked"
    assert notices == ["Installed in Trusted Root."]


def test_saying_no_changes_nothing(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch)
    monkeypatch.setattr(app.messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(tls, "trust_ca_on_windows", lambda path: pytest.fail("must not install"))
    dashboard.open_certificate()
    assert notices == ["Left as it was."]


def test_if_it_fails_the_certificate_window_opens_with_the_store_named(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch)
    monkeypatch.setattr(app.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(tls, "trust_ca_on_windows", lambda path: (False, "Windows did not add it."))
    opened = []
    monkeypatch.setattr("ninaivu.desktop.tray.open_file", opened.append)
    dashboard.open_certificate()
    assert opened == [ninaivu_ca]
    assert "Trusted Root Certification Authorities" in notices[0]


def test_on_a_mac_the_certificate_is_trusted_after_asking(tmp_path, ninaivu_ca, monkeypatch):
    """Not imported through Keychain Access, which offers keychains that take no
    certificates and refuses one already there — "unable to import", untrusted."""
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch, platform="darwin")
    monkeypatch.setattr(app.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(tls, "trust_ca_on_mac", lambda path: (True, "Trusted for websites."))
    ran = []
    monkeypatch.setattr("ninaivu.desktop.tray.subprocess.run", lambda command, **kw: ran.append(command))
    monkeypatch.setattr(app.webbrowser, "open", lambda *a, **k: pytest.fail("opened in the browser"))
    dashboard.open_certificate()
    assert ran == [], "nothing to open when trusting worked"
    assert notices == ["Trusted for websites."]


def test_on_a_mac_a_failed_trust_shows_the_file_and_the_last_step(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch, platform="darwin")
    monkeypatch.setattr(app.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(tls, "trust_ca_on_mac", lambda path: (False, "macOS did not trust it."))
    ran = []
    monkeypatch.setattr("ninaivu.desktop.tray.subprocess.run", lambda command, **kw: ran.append(command))
    dashboard.open_certificate()
    assert ran == [["open", "-R", str(ninaivu_ca)]]
    assert "Always Trust" in notices[0]


def test_on_a_mac_saying_no_changes_nothing(tmp_path, ninaivu_ca, monkeypatch):
    from ninaivu.utils import tls
    dashboard, notices = _dashboard_for(tmp_path, monkeypatch, platform="darwin")
    monkeypatch.setattr(app.messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(tls, "trust_ca_on_mac", lambda path: pytest.fail("must not trust"))
    dashboard.open_certificate()
    assert notices == ["Left as it was."]


# ---------------------------------------------------------------------------
# What the panel shows
# ---------------------------------------------------------------------------

def test_each_mode_says_what_it_gives_this_computer():
    """Worked out from the budget, not written down: a fixed "4 workers" was
    wrong on most machines."""
    from ninaivu.utils.resources import budget
    for cpus in (2, 8, 16):
        for mode, _title, _description in app.MODES:
            b = budget(mode, cpus)
            hint = app.budget_hint(mode, cpus)
            assert hint.startswith(f"{b['workers']} worker")
            assert f"{b['compute_threads']} AI threads" in hint
    assert app.budget_hint("power-saving", 16).startswith("1 worker ·")


def test_log_lines_are_coloured_by_what_they_say():
    assert app.line_tag("Traceback (most recent call last):") == "error"
    assert app.line_tag("WARNING: slow disk") == "warning"
    assert app.line_tag("  family app: https://ninaivu.local") == "info"
    assert app.line_tag("scanned 120 files") == ""


def test_without_psutil_the_panel_says_so_instead_of_calling_ninaivu_stopped(monkeypatch):
    from ninaivu.desktop import control
    monkeypatch.setattr(control, "psutil", None)
    assert "psutil" in app.missing_requirement()
    assert "requirements-desktop.txt" in app.NEEDS


def test_the_log_pane_follows_the_server_or_the_local_ai(tmp_path):
    dashboard = app.Dashboard.__new__(app.Dashboard)
    dashboard.controller = SimpleNamespace(runtime=tmp_path, logs=tmp_path)
    dashboard.log_source = SimpleNamespace(get=lambda: "Server")
    assert dashboard.log_path() == tmp_path / "server.log"
    dashboard.log_source = SimpleNamespace(get=lambda: "Local AI")
    assert dashboard.log_path() == tmp_path / "ollama.log"


# ---------------------------------------------------------------------------
# The mode a running server was started in
# ---------------------------------------------------------------------------

class FakeProcess:
    def __init__(self, environ=None, denied=False):
        self._environ, self._denied = environ or {}, denied

    def environ(self):
        if self._denied:
            from ninaivu.desktop import control
            raise control._PsutilError("access denied")
        return self._environ


def test_the_running_mode_is_read_from_the_servers_environment():
    from ninaivu.desktop.control import running_mode
    process = FakeProcess({"NINAIVU_RESOURCE_MODE": "power-saving"})
    assert running_mode(process, ["--workers", "8"], "standard") == "power-saving"


def test_standard_on_sixteen_cores_is_not_taken_for_performance(monkeypatch):
    """Standard gives a sixteen-core machine eight workers; the old fixed table
    read eight as performance, ticked it in the panel and saved it."""
    from ninaivu.desktop.control import running_mode
    monkeypatch.setattr("ninaivu.utils.resources.os.cpu_count", lambda: 16)
    denied = FakeProcess(denied=True)
    assert running_mode(denied, ["--host", "0.0.0.0", "--workers", "8"], "standard") == "standard"
    assert running_mode(denied, ["--workers=16"], "standard") == "performance"
    assert running_mode(denied, ["--workers", "1"], "standard") == "power-saving"
    assert running_mode(denied, ["--https"], "performance") == "performance"
    assert running_mode(None, ["--workers", "3"], "standard") == "standard"


# ---------------------------------------------------------------------------
# The window itself, where there is a display
# ---------------------------------------------------------------------------

def test_the_window_opens_with_the_logs_hidden_until_asked(tmp_path, monkeypatch):
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    from ninaivu.desktop.control import Controller
    monkeypatch.setattr(Controller, "record", lambda _: None)
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: False)
    controller = Controller(root=tmp_path, cfg=SimpleNamespace(
        state_dir=tmp_path / "state", host="127.0.0.1", port=443, admin_port=3000,
        ai_engine="off", network_access=False))
    dashboard = None
    try:
        root.withdraw()
        dashboard = app.Dashboard(root, controller)
        assert dashboard.mode_description.get() != ""
        assert dashboard.logs_visible is False
        assert len(dashboard.split.panes()) == 1
        assert dashboard.log_button_text.get() == "View logs"

        dashboard.open_logs()
        assert dashboard.logs_visible is True
        assert len(dashboard.split.panes()) == 2
        assert dashboard.log_button_text.get() == "Hide logs"

        dashboard.hide_logs()
        assert dashboard.logs_visible is False
        assert dashboard.log_button_text.get() == "View logs"

        dashboard.toggle_logs()
        assert dashboard.logs_visible is True
    finally:
        if dashboard is not None:
            dashboard.finished.set()
        root.destroy()


@pytest.mark.parametrize("look", ["light", "dark"])
def test_hiding_the_logs_gives_the_height_back(tmp_path, monkeypatch, look):
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    from ninaivu.desktop.control import Controller
    monkeypatch.setattr(Controller, "record", lambda _: None)
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: False)
    controller = Controller(root=tmp_path, cfg=SimpleNamespace(
        state_dir=tmp_path / "state", host="127.0.0.1", port=443, admin_port=3000,
        ai_engine="off", network_access=False))
    dashboard = None

    def settle():
        for _ in range(5):
            root.update()
        return root.winfo_width(), root.winfo_height()

    try:
        dashboard = app.Dashboard(root, controller)
        dashboard.theme_choice.set(look)
        dashboard.choose_theme()
        # The first reading fits the window to its filled-in contents once; a
        # slow machine (the macOS runner) gets it only after a second or so,
        # so wait for it rather than let it land in the middle.
        deadline = time.monotonic() + 10
        while not dashboard._grown_for_readings and time.monotonic() < deadline:
            settle()
            time.sleep(0.05)
        before = settle()
        if not root.winfo_viewable() or before[1] <= 1:
            pytest.skip("the window manager did not show the window")

        # Opened and hidden again: the same size as before, wider or not.
        dashboard.show_logs()
        opened = settle()
        dashboard.hide_logs()
        assert settle() == before
        assert opened[1] >= before[1]

        # Made taller while the logs were open: only the pane's height goes.
        dashboard.show_logs()
        width, height = settle()
        root.geometry(f"{width}x{height + 80}")
        width, height = settle()
        pane = dashboard.split.winfo_height() - dashboard.upper_pane.winfo_height()
        dashboard.hide_logs()
        root.update_idletasks()
        needed = min(root.winfo_reqheight(), dashboard._room()[1])
        expected = min(height, max(root.minsize()[1], needed, height - pane))
        assert abs(settle()[1] - expected) <= 2, (before, opened, height, pane, needed, root.minsize())
        assert root.winfo_width() == width
    finally:
        if dashboard is not None:
            dashboard.finished.set()
        root.destroy()


# ---------------------------------------------------------------------------
# The Windows launcher and the entry point
# ---------------------------------------------------------------------------

VBS = ROOT / "Start - Ninaivu Control Panel.vbs"
PYW = ROOT / "ninaivu_control.pyw"


def test_the_windows_launcher_runs_the_panel_with_the_venvs_pythonw():
    text = VBS.read_text(encoding="utf-8")
    assert r".venv\Scripts\pythonw.exe" in text
    assert "ninaivu_control.pyw" in text
    assert "MsgBox" in text, "a missing .venv must be said, not a silent failure"


def test_line_endings_are_pinned_for_the_launchers():
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8").split()
    for pattern, ending in (("*.vbs", "eol=crlf"), ("*.command", "eol=lf"), ("*.sh", "eol=lf")):
        at = rules.index(pattern)
        assert ending in rules[at + 1:at + 3], pattern


def _load_pyw():
    loader = SourceFileLoader("ninaivu_control_pyw", str(PYW))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_a_python_without_tk_is_told_what_to_install(monkeypatch):
    entry = _load_pyw()
    told = []
    monkeypatch.setattr(entry, "tell", told.append)
    monkeypatch.setitem(sys.modules, "tkinter", None)       # import tkinter now fails
    monkeypatch.setattr(sys, "platform", "darwin")
    assert entry.main() == 1
    assert "python-tk@3.12" in told[0] and "tray" in told[0]


def test_a_panel_that_cannot_start_says_why(monkeypatch):
    entry = _load_pyw()
    told = []
    monkeypatch.setattr(entry, "tell", told.append)

    def broken():
        raise RuntimeError("no configuration")
    monkeypatch.setattr(app, "main", broken)
    assert entry.main() == 1
    assert "no configuration" in told[0]


# ---------------------------------------------------------------------------
# The two looks
# ---------------------------------------------------------------------------

from ninaivu.desktop import theme  # noqa: E402

#: (words, what they sit on) pairs that must be readable: WCAG AA, 4.5 to 1.
READABLE = [
    ("text", "bg"), ("text", "surface"), ("text2", "bg"), ("text2", "surface"),
    ("accent_fg", "accent_fill"), ("accent_fg", "accent_fill_hover"),
    ("success_fg", "success_fill"), ("success_fg", "success_fill_hover"),
    ("danger_fg", "danger_fill"), ("danger_fg", "danger_fill_hover"),
    ("status_running_fg", "status_running_bg"), ("status_busy_fg", "status_busy_bg"),
    ("status_stopped_fg", "status_stopped_bg"),
    ("log_text", "log_bg"), ("log_error", "log_bg"), ("log_warning", "log_bg"),
    ("log_info", "log_bg"), ("log_success", "log_bg"), ("match_fg", "match_bg"),
]


@pytest.mark.parametrize("look", ["light", "dark"])
def test_each_look_is_readable(look):
    p = theme.palette(look)
    for words, ground in READABLE:
        assert theme.contrast(p[words], p[ground]) >= 4.5, (look, words, ground)


def test_both_looks_name_the_same_colours():
    assert set(theme.palette("light")) == set(theme.palette("dark"))
    # The family app's and the console's own ground colours, so the three match.
    assert theme.palette("dark")["bg"] == "#0c0f14"
    assert theme.palette("light")["bg"] == "#f6f7f9"


def test_system_follows_the_computer_and_a_choice_overrides_it():
    assert theme.resolve("system", True) == "dark"
    assert theme.resolve("system", False) == "light"
    assert theme.resolve("light", True) == "light"
    assert theme.resolve("dark", False) == "dark"


def _answer(stdout):
    return lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=0)


def test_the_computers_own_setting_is_read_on_each_system():
    assert theme.system_prefers_dark("darwin", run=_answer("Dark\n")) is True
    # In light a Mac has no such key, and says so on stderr.
    assert theme.system_prefers_dark("darwin", run=_answer("")) is False
    assert theme.system_prefers_dark("linux", run=_answer("'prefer-dark'\n")) is True
    assert theme.system_prefers_dark("linux", run=_answer("'default'\n")) is False

    def missing(*a, **k):
        raise FileNotFoundError("gsettings")
    assert theme.system_prefers_dark("linux", run=missing) is False


def test_the_chosen_look_is_kept_beside_the_mode(tmp_path):
    from ninaivu.desktop.control import Controller
    cfg = SimpleNamespace(state_dir=tmp_path / "state", host="127.0.0.1", port=443, admin_port=3000)
    controller = Controller(root=tmp_path, cfg=cfg)
    assert app.theme_preference(controller) == "system"
    app.save_theme_preference(controller, "dark")
    controller.save_mode("performance")
    again = Controller(root=tmp_path, cfg=cfg)
    assert app.theme_preference(again) == "dark"
    assert again.mode == "performance"
    with pytest.raises(ValueError):
        app.save_theme_preference(controller, "sepia")
    assert app.theme_preference(SimpleNamespace(settings={"theme": "sepia"})) == "system"


def test_switching_the_look_repaints_the_open_window(tmp_path, monkeypatch):
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    from ninaivu.desktop.control import Controller
    monkeypatch.setattr(Controller, "record", lambda _: None)
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: False)
    monkeypatch.setattr(theme, "system_prefers_dark", lambda platform=None, run=None: False)
    controller = Controller(root=tmp_path, cfg=SimpleNamespace(
        state_dir=tmp_path / "state", host="127.0.0.1", port=443, admin_port=3000,
        ai_engine="off", network_access=False))
    dashboard = None
    try:
        root.withdraw()
        dashboard = app.Dashboard(root, controller)
        light, dark = theme.palette("light"), theme.palette("dark")
        assert root.cget("bg") == light["bg"]

        dashboard.theme_choice.set("dark")
        dashboard.choose_theme()
        assert root.cget("bg") == dark["bg"]
        assert dashboard.log_text.cget("bg") == dark["log_bg"]
        assert dashboard.status_pill.cget("bg") == dark["status_stopped_bg"]
        assert dashboard.style.lookup("TButton", "background") == dark["surface"]
        assert app.theme_preference(Controller(root=tmp_path, cfg=controller.cfg)) == "dark"

        # Back to following the computer, which has meanwhile turned dark.
        dashboard.theme_choice.set("system")
        dashboard.choose_theme()
        assert root.cget("bg") == light["bg"]
        dashboard.events.put(("appearance", True))
        dashboard.pump()
        assert root.cget("bg") == dark["bg"]
    finally:
        if dashboard is not None:
            dashboard.finished.set()
        root.destroy()
