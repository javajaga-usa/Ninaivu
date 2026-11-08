"""One desktop program: the Control Panel holds the tray icon, closing the
window puts it there, opening Ninaivu starts it, and opening it again shows
the one already open. Nothing here needs a display or Tk."""

import json
import queue
import sys
import threading
import time
import types

import pytest

from ninaivu.desktop import app, instance, tray


# -- opening it again shows the one already open ----------------------------------

def test_the_first_holds_the_desktop_and_a_second_shows_it(tmp_path):
    first = instance.claim(tmp_path)
    assert first is not None
    shown = threading.Event()
    first.serve(shown.set)
    try:
        assert instance.claim(tmp_path, patience=2) is None, "the second one ends"
        assert shown.wait(5), "and the first was told to show its window"
    finally:
        first.close()


def test_once_the_first_has_quit_the_next_one_is_the_one(tmp_path):
    first = instance.claim(tmp_path)
    first.serve(lambda: None)
    first.close()
    assert not (tmp_path / instance.ADDRESS).exists()
    second = instance.claim(tmp_path, patience=0)
    try:
        assert second is not None and (tmp_path / instance.ADDRESS).is_file()
    finally:
        second.close()


def test_a_first_that_does_not_answer_does_not_leave_the_person_with_nothing(tmp_path):
    first = instance.claim(tmp_path)                     # never serves: hung, or still drawing
    try:
        second = instance.claim(tmp_path, patience=0.3, sleep=lambda s: time.sleep(0.05))
        assert second is not None, "it opens anyway"
        second.close()
    finally:
        first.close()


def test_only_the_right_token_brings_the_window_up(tmp_path):
    first = instance.claim(tmp_path)
    shown = []
    first.serve(lambda: shown.append(True))
    try:
        told = json.loads((tmp_path / instance.ADDRESS).read_text())
        told["token"] = "0" * 32
        (tmp_path / instance.ADDRESS).write_text(json.dumps(told))
        assert instance.tell_to_show(tmp_path) is False
        time.sleep(0.2)
        assert shown == []
    finally:
        first.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_token_is_this_accounts_alone(tmp_path):
    first = instance.claim(tmp_path)
    try:
        assert (tmp_path / instance.ADDRESS).stat().st_mode & 0o077 == 0
    finally:
        first.close()


def test_a_folder_that_cannot_be_told_opens_as_before(monkeypatch):
    def no_folder(folder):
        raise PermissionError("read-only")
    assert app.claim_the_desktop(no_folder) is None
    assert app.claim_the_desktop(lambda folder: None) is app.ALREADY_OPEN
    held = object()
    assert app.claim_the_desktop(lambda folder: held) is held


# -- the tray's menu: the panel minimised ------------------------------------------

class Host:
    def __init__(self):
        self.posted = []

    def post(self, work):
        self.posted.append(work)

    def __getattr__(self, name):
        if name in ("show_window", "start", "stop", "open_family", "quit"):
            return name
        raise AttributeError(name)


def _menu(menu):
    return [(i["label"], i.get("enabled")) for i in menu.menu() if i["label"] != "-"]


def test_the_menu_offers_only_the_one_of_start_and_stop_that_can_be_pressed():
    menu = tray.PanelTray(Host())
    labels = [label for label, _ in _menu(menu)]
    assert "Start Ninaivu" in labels and "Stop Ninaivu" not in labels
    assert labels[0] == "Ninaivu is stopped" and labels[-1] == "Quit"
    menu.update(True, None)
    labels = [label for label, _ in _menu(menu)]
    assert "Stop Ninaivu" in labels and "Start Ninaivu" not in labels
    assert labels[-1] == "Quit (Ninaivu keeps running)"


def test_clicking_the_icon_shows_the_window():
    menu = tray.PanelTray(Host())
    default = [i for i in menu.menu() if i.get("default")]
    assert [i["label"] for i in default] == ["Show Ninaivu"]


def test_while_busy_nothing_can_be_pressed_twice():
    menu = tray.PanelTray(Host())
    menu.update(True, "Stopping")
    items = dict(_menu(menu))
    assert "Stopping…" in items
    assert items["Stop Ninaivu"] is False and items["Open the family app"] is False
    assert items["Show Ninaivu"] is True and items["Quit (Ninaivu keeps running)"] is True


def test_every_click_is_handed_to_the_window_not_run_on_the_icons_thread():
    host = Host()
    menu = tray.PanelTray(host)
    for item in menu.menu():
        if item["action"] is not None:
            item["action"]()
    assert host.posted == ["show_window", "open_family", "start", "quit"]


def test_the_menu_is_rebuilt_only_when_what_it_says_changes():
    class Icon:
        updates = 0

        def update_menu(self):
            Icon.updates += 1
    icon = tray.TrayIcon(Icon(), tray.PanelTray(Host()), shown=True, platform="win32")
    icon.refresh(False, None)
    assert Icon.updates == 0
    icon.refresh(True, None)
    icon.refresh(True, None)
    assert Icon.updates == 1
    icon.refresh(True, "Stopping")
    assert Icon.updates == 2


# -- each system's arrangement ---------------------------------------------------

class FakeIcon:
    HAS_NOTIFICATION = True
    made = []

    def __init__(self, name, image, title, menu=None):
        self.menu, self.calls, self.visible = menu, [], False
        FakeIcon.made.append(self)

    def run_detached(self, setup=None):
        self.calls.append(("run_detached", threading.current_thread() is threading.main_thread()))
        setup(self)

    def run(self, setup=None):
        self.calls.append(("run", threading.current_thread() is threading.main_thread()))

    def update_menu(self):
        pass

    def stop(self):
        self.calls.append(("stop", None))


@pytest.fixture()
def fake_pystray(monkeypatch):
    module = types.ModuleType("pystray")
    module.Icon = FakeIcon
    module.Menu = type("Menu", (), {"SEPARATOR": "-", "__init__": lambda self, *items: setattr(self, "items", items)})
    module.MenuItem = lambda text, action, enabled=True, default=False: (text, enabled, default)
    monkeypatch.setitem(sys.modules, "pystray", module)
    pil = types.ModuleType("PIL")
    pil.Image = types.SimpleNamespace(open=lambda path: "image")
    monkeypatch.setitem(sys.modules, "PIL", pil)
    FakeIcon.made = []
    return module


def test_on_a_mac_the_icon_joins_tks_loop_on_the_main_thread(fake_pystray):
    icon = tray.attach(Host(), platform="darwin")
    made = FakeIcon.made[0]
    assert made.calls == [("run_detached", True)], "no loop of its own: Tk's is the main thread's"
    assert made.visible is True and icon.shown
    icon.close()
    assert made.visible is False and ("stop", None) in made.calls


def test_on_windows_the_icon_has_its_own_loop_on_its_own_thread(fake_pystray):
    icon = tray.attach(Host(), platform="win32")
    made = FakeIcon.made[0]
    for _ in range(50):
        if made.calls:
            break
        time.sleep(0.02)
    assert made.calls == [("run", False)] and icon.shown


def test_on_linux_it_is_the_x11_tray_and_shown_only_where_there_is_one(fake_pystray, monkeypatch):
    monkeypatch.delenv("PYSTRAY_BACKEND", raising=False)
    icon = tray.attach(Host(), platform="linux")
    import os
    assert os.environ["PYSTRAY_BACKEND"] == "xorg", "Tk runs no GLib loop for the GTK backends"
    assert icon is not None and icon.shown is False, "a fake display has no tray: closing closes"


def test_no_tray_where_pystray_cannot_run(monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", None)       # import pystray now fails
    assert tray.attach(Host(), platform="win32") is None


def test_the_items_pystray_is_given(fake_pystray):
    menu = tray.PanelTray(Host())
    items = tray.pystray_items(fake_pystray, menu)
    assert ("Show Ninaivu", True, True) in items and "-" in items


def test_the_tray_entry_point_is_the_same_program_minimised(monkeypatch):
    monkeypatch.setattr(app, "tk", object())
    seen = []
    monkeypatch.setattr(app, "main", lambda argv: seen.append(argv) or 0)
    assert tray.main([]) == 0 and seen == [["--tray"]]


# -- the window and the icon --------------------------------------------------------

def test_options():
    assert app.parse_arguments(["--tray"]).tray is True
    assert app.parse_arguments(["--hello", "two words"]).tray is False, "a launcher's extra words are left alone"


def test_closing_the_window_puts_it_in_the_tray_only_where_there_is_one():
    shown = types.SimpleNamespace(shown=True)
    assert app.close_action(shown) == "hide"
    assert app.close_action(types.SimpleNamespace(shown=False)) == "close"
    assert app.close_action(None) == "close"
    assert app.close_action(shown, closing=True) == "close"
    assert app.tray_place("darwin") == "menu bar" and app.tray_place("win32") == "tray"


class Root:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append(name)

    def title(self):
        return "Ninaivu Control Panel"


def _board(**extra):
    board = app.Dashboard.__new__(app.Dashboard)
    board.root = Root()
    board.events = queue.Queue()
    board.finished = threading.Event()
    board.busy = False
    board.closing = False
    board.opening = False
    board.check_report = None
    board.tray = None
    board.__dict__.update(extra)
    return board


def test_the_close_button_hides_into_the_tray_and_says_where_once():
    said = []
    icon = types.SimpleNamespace(shown=True, told_once=False, notify=said.append)
    board = _board(tray=icon)
    board.close = lambda: pytest.fail("the program goes on")
    board.close_window()
    board.close_window()
    assert board.root.calls == ["withdraw", "withdraw"]
    assert len(said) == 1 and "keeps running" in said[0]


def test_without_a_tray_the_close_button_closes_as_before():
    closed = []
    board = _board()
    board.close = lambda: closed.append(True)
    board.close_window()
    assert closed == [True] and board.root.calls == []


def test_quit_takes_the_icon_away_and_closes_leaving_ninaivu_running():
    gone, closed = [], []
    board = _board(tray=types.SimpleNamespace(close=lambda: gone.append(True)))
    board.close = lambda: closed.append(True)
    board.quit()
    assert gone == [True] and closed == [True] and board.tray is None


def _run_posted(board):
    kind, work = board.events.get(timeout=5)
    assert kind == "call"
    work()


def test_opening_ninaivu_starts_it():
    started = []
    board = _board(controller=types.SimpleNamespace(record=lambda: None))
    board.start = lambda: started.append(True)
    board.start_on_open()
    _run_posted(board)
    assert started == [True] and board.opening is True


def test_opening_a_running_ninaivu_starts_nothing():
    board = _board(controller=types.SimpleNamespace(record=lambda: {"pid": 1}))
    board.start = lambda: pytest.fail("already running")
    board.start_on_open()
    _run_posted(board)
    assert board.opening is False


def test_a_reading_that_fails_starts_nothing():
    def broken():
        raise OSError("the disk did not answer")
    board = _board(controller=types.SimpleNamespace(record=broken))
    board.start = lambda: pytest.fail("not on a guess")
    board.start_on_open()
    _run_posted(board)


def test_the_release_check_hears_how_opening_went_and_the_program_quits(tmp_path):
    report = tmp_path / "opened.json"
    board = _board(check_report=str(report), tray=types.SimpleNamespace(shown=False))
    board._report_open("done", "Ninaivu is running.", True)
    told = json.loads(report.read_text())
    assert told["running"] is True and told["title"] == "Ninaivu Control Panel"
    assert told["tray"] is True and told["tray_shown"] is False
    assert "after" in board.root.calls, "and it quits, as a person would"
