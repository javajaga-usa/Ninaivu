"""The tray: the menu says what Ninaivu is doing, every action goes through
the controller's own start and stop, and nothing here needs a display."""

from types import SimpleNamespace

import pytest

from ninaivu.desktop import tray as tray_mod
from ninaivu.desktop.tray import Tray


class FakeController:
    def __init__(self, running=False, tmp_path=None):
        self._running = running
        self.calls = []
        self.settings = {"arguments": []}
        self.cfg = SimpleNamespace(state_dir=tmp_path)
        self.runtime = tmp_path
        self.root = tmp_path

    def record(self):
        return {"pid": 1} if self._running else None

    def start(self):
        self.calls.append("start"); self._running = True
        return "Ninaivu is running."

    def stop(self):
        self.calls.append("stop"); self._running = False
        return "Ninaivu stopped cleanly."

    def server_url(self, admin=False):
        return "https://ninaivu-admin.local" if admin else "https://ninaivu.local"


@pytest.fixture()
def tray(tmp_path, monkeypatch):
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: False)
    notes, opened = [], []
    t = Tray(FakeController(tmp_path=tmp_path), notify=lambda title, m: notes.append(m),
             ask=lambda title, q: True, open_url=opened.append, run_async=False)
    return t, notes, opened


def _labels(t):
    return [(i["label"], i.get("enabled")) for i in t.menu() if i["label"] != "-"]


def test_the_menu_follows_whether_ninaivu_is_running(tray):
    t, _, _ = tray
    assert _labels(t)[0] == ("Ninaivu is stopped", False)
    enabled = {label: on for label, on in _labels(t)}
    assert enabled["Start"] and not enabled["Stop"] and not enabled["Restart"]
    assert not enabled["Open the family app"]

    t.start()
    assert _labels(t)[0] == ("Ninaivu is running", False)
    enabled = {label: on for label, on in _labels(t)}
    assert enabled["Stop"] and enabled["Restart"] and enabled["Open the console"] and not enabled["Start"]


def test_actions_go_through_the_controller_and_say_what_happened(tray):
    t, notes, opened = tray
    t.start(); t.restart(); t.stop()
    assert t.controller.calls == ["start", "stop", "start", "stop"]
    assert notes == ["Ninaivu is running.", "Ninaivu is running.", "Ninaivu stopped cleanly."]
    t.controller._running = True
    t.open_family(); t.open_console()
    assert opened == ["https://ninaivu.local", "https://ninaivu-admin.local"]


def test_an_error_is_a_notification_not_a_crash(tray):
    t, notes, _ = tray
    t.controller.start = lambda: (_ for _ in ()).throw(RuntimeError("Ninaivu could not start."))
    t.start()
    assert notes == ["Ninaivu could not start."]
    assert t.busy is None, "the menu is not left greyed out"


def test_one_thing_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: False)
    notes = []
    t = Tray(FakeController(tmp_path=tmp_path), notify=lambda title, m: notes.append(m), run_async=False)
    t.busy = "Starting"
    assert t.status_line() == "Starting…"
    assert not {label: on for label, on in _labels(t)}["Stop"]
    t.stop()
    assert notes == ["Starting — wait for it to finish."] and t.controller.calls == []


def test_how_to_update_says_stop_first_and_asks_nobody(tray, monkeypatch):
    t, notes, opened = tray
    from ninaivu import __version__

    def no_network(*_a, **_k):
        raise AssertionError("the tray asked the internet")
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    t.how_to_update()
    assert opened == []
    assert notes[-1].startswith(f"Ninaivu {__version__}.") and "press Stop" in notes[-1]


def test_the_log_is_opened_only_once_there_is_one(tray, monkeypatch):
    t, notes, _ = tray
    shown = []
    monkeypatch.setattr(tray_mod, "open_file", shown.append)
    t.view_log()
    assert notes == ["No log yet — Ninaivu has not been started from here."] and shown == []
    (t.controller.runtime / "server.log").write_text("hello")
    t.view_log()
    assert shown == [t.controller.runtime / "server.log"]


def test_a_custom_certificate_is_not_replaced(tray):
    t, notes, _ = tray
    t.controller.settings["arguments"] = ["--cert", "mine.pem"]
    t.trust_certificate()
    assert "custom certificate" in notes[0]


def test_saying_no_to_trusting_leaves_things_as_they_were(tray, monkeypatch):
    from ninaivu.utils import tls
    t, notes, _ = tray
    tls.ensure_certificate(t.controller.cfg.state_dir)
    t.ask = lambda title, q: False
    monkeypatch.setattr(tray_mod.sys, "platform", "win32")
    touched = []
    monkeypatch.setattr("ninaivu.utils.tls.trust_ca_on_windows", lambda path: touched.append(path) or (True, "x"))
    t.trust_certificate()
    assert notes == ["Left as it was."] and touched == []


def test_start_at_sign_in_is_a_checked_item_where_it_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: True)
    monkeypatch.setattr("ninaivu.desktop.autostart.enabled", lambda home=None, platform=None: True)
    t = Tray(FakeController(tmp_path=tmp_path), run_async=False)
    item = next(i for i in t.menu() if i["label"] == "Start at sign-in")
    assert item["checked"] is True


def test_the_tray_says_what_it_needs_when_pystray_is_missing(monkeypatch, capsys):
    import builtins
    real = builtins.__import__

    def no_pystray(name, *a, **k):
        if name == "pystray":
            raise ImportError("no pystray")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_pystray)
    assert tray_mod.main([]) == 2
    assert "requirements-desktop.txt" in capsys.readouterr().err
