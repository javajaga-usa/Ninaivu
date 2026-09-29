"""The Control Panel window (ninaivu/desktop/app.py) and its launchers.

Most of it is Tk, which needs a display; what is tested here is everything
that is not: the certificate steps it shares with the tray, the numbers it
shows under each resource mode, the running server's mode it follows, and
what it says when it cannot open at all. The window itself is built once, when
there is a display to build it on.
"""

import importlib.util
import sys
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
    dashboard.controller = SimpleNamespace(runtime=tmp_path)
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
