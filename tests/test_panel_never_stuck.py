"""The Control Panel is never stuck with a start.

A start that kept its heartbeat but stopped moving held the panel waiting
for up to an hour, and the window would not close meanwhile ("Wait for the
current start or stop to finish"). Its window also read the index folder on
its own thread every second, so a disk slow to answer (a sleeping USB drive)
froze it: "not responding". Now the window only draws; the readings, the log
and the panel's actions run on threads of their own. A start that does not
move on is called stuck and can be stopped, safely; the window closes at
any time, and a restart half-way is handed to the restart helper.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ninaivu.desktop import control, progress
from ninaivu.server import starting


def _controller(tmp_path):
    c = control.Controller.__new__(control.Controller)
    c.cfg = SimpleNamespace(state_dir=tmp_path)
    c.started, c.went_back, c.mode, c.on_progress = None, False, "standard", None
    c.runtime = tmp_path
    return c


# -- a start that does not move on ----------------------------------------------------

def test_a_start_that_stops_moving_is_not_waited_on_for_an_hour(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    launched = time.time()
    monkeypatch.setattr(starting, "read", lambda *_a, **_k: {
        "step": "opening the index", "since": launched, "at": time.time()})
    monkeypatch.setattr(c, "record", lambda: None)
    clock = iter(range(0, 10 ** 6, 30))
    with pytest.raises(RuntimeError, match="opening the index for 1[56] minutes without moving on"):
        c._wait_for_start(launched, clock=lambda: next(clock), sleep=lambda _s: None)


def test_a_start_that_keeps_moving_is_waited_on(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    launched = time.time()
    share = iter(i / 10000 for i in range(10 ** 6))
    monkeypatch.setattr(starting, "read", lambda *_a, **_k: {
        "step": "moving the index to its new folder: copying", "progress": next(share),
        "since": launched, "at": time.time()})
    ticks = iter(range(10 ** 6))
    monkeypatch.setattr(c, "record", lambda: {"pid": 1} if next(ticks) > 100 else None)
    monkeypatch.setattr(c, "capture_running_settings", lambda: None)
    monkeypatch.setattr(c, "save_mode", lambda *_: None)
    clock = iter(range(0, 10 ** 6, 30))             # 50 minutes of a moving bar
    assert c._wait_for_start(launched, clock=lambda: next(clock), sleep=lambda _s: None) == "Ninaivu is running."


def test_a_start_stopped_by_the_person_is_not_a_failed_start(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    c.started = SimpleNamespace(poll=lambda: 1, pid=1)
    monkeypatch.setattr(c, "failed_start", lambda: pytest.fail("counted as a failed start"))
    monkeypatch.setattr(starting, "read", lambda *_a, **_k: None)

    def stopped_meanwhile():
        c.stopping_start = True                      # what stop_start sets first
        return None
    monkeypatch.setattr(c, "record", stopped_meanwhile)
    with pytest.raises(control.StartStopped):
        c._wait_for_start(time.time(), clock=lambda: 0.0, sleep=lambda _s: None)


# -- stopping a start ------------------------------------------------------------------

def _process(tmp_path, name):
    script = tmp_path / name
    script.write_text("import time\nwhile True:\n    time.sleep(0.1)\n")
    return subprocess.Popen([sys.executable, str(script)])


def test_stop_this_start_ends_the_starting_ninaivu(tmp_path):
    pytest.importorskip("psutil")
    server = _process(tmp_path, "ninaivu_server_standin.py")
    try:
        note = {"pid": server.pid, "step": "checking the index: index.db", "since": time.time(),
                "at": time.time()}
        (tmp_path / starting.STARTING_FILE).write_text(json.dumps(note))
        c = _controller(tmp_path)
        answer = c.stop_start(patience=5)
        assert answer.startswith("The start was stopped")
        assert server.wait(timeout=5) is not None
        assert c.stopping_start is True
    finally:
        server.kill()


def test_a_process_that_is_not_ninaivu_is_never_ended(tmp_path):
    pytest.importorskip("psutil")
    other = _process(tmp_path, "somebody_elses_program.py")
    try:
        (tmp_path / starting.STARTING_FILE).write_text(json.dumps(
            {"pid": other.pid, "step": "starting", "since": time.time(), "at": time.time()}))
        assert _controller(tmp_path).stop_start(patience=1) == "Ninaivu is not starting."
        assert other.poll() is None
    finally:
        other.kill()


def test_a_start_waiting_on_a_dead_disk_is_said_so(tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    (tmp_path / starting.STARTING_FILE).write_text(json.dumps(
        {"pid": 4242, "step": "opening the index", "since": time.time(), "at": time.time()}))
    monkeypatch.setattr(control, "_end_ninaivu", lambda pid, patience: "stuck")
    assert "waiting on a disk that does not answer" in _controller(tmp_path).stop_start()


@pytest.mark.parametrize("step, words", [
    ("moving the index to its new folder: copying", "tried again at the next start"),
    ("checking the index: index.db", "done again at the next start"),
    ("repairing the index: index.db, table 2 of 9", "keeps its original"),
    ("copying the settings and the index before the update: copying", "Nothing in the index has changed"),
    ("bringing in the index from the other computer: unpacking", "left unfinished"),
    ("waiting for Ninaivu to finish what is in hand", "restart is called off"),
    ("opening the index", "Nothing is lost"),
])
def test_what_stopping_does_is_said_before_it_is_done(step, words):
    assert words in progress.stop_warning(step)


def test_an_import_is_offered_a_stop_only_once_it_is_stuck():
    step = "bringing in the index from the other computer: unpacking"
    assert progress.may_stop(step, stuck=False) is False
    assert progress.may_stop(step, stuck=True) is True
    assert progress.may_stop("checking the index: index.db", stuck=False) is True
    assert "16 minutes" in progress.stuck_words(16 * 60)


# -- a restart left half-way --------------------------------------------------------------

def test_a_restart_the_panel_leaves_is_handed_to_the_helper(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    c.root, c.logs = tmp_path, tmp_path
    started = []
    monkeypatch.setattr(control, "python_for_server", lambda _root: Path(sys.executable))
    monkeypatch.setattr(control.subprocess, "Popen", lambda command, **kw: started.append((command, kw)))
    c.hand_over_restart("performance")
    command, kwargs = started[0]
    assert command[1:] == ["-m", "ninaivu.desktop.relaunch", "--stopping", "--mode", "performance"]
    assert kwargs["env"]["NINAIVU_STATE_DIR"] == str(tmp_path)
    if sys.platform != "win32":
        assert kwargs["start_new_session"] is True       # outlives the panel


def test_the_helper_finishes_a_stop_without_asking_again(tmp_path, monkeypatch):
    from ninaivu.desktop import relaunch
    said = []

    class Panel:
        def __init__(self):
            self.cfg = SimpleNamespace(state_dir=tmp_path)
            self.settings, self.mode, self.on_progress = {}, "standard", None

        def record(self):
            return {"pid": 1}

        def stop(self):
            pytest.fail("asked to stop twice")

        def wait_for_stop(self, server):
            said.append("waited")
            return "Ninaivu stopped cleanly."

        def save_mode(self, _mode):
            pass

        def start(self):
            said.append("started")
            return "Ninaivu is running."

    monkeypatch.setattr(control, "Controller", Panel)
    monkeypatch.setattr(control, "_server_process", lambda _record: None)
    monkeypatch.setattr(control, "python_for_server", lambda _root: tmp_path)
    monkeypatch.setattr(control, "ninaivu_root", lambda: tmp_path)
    monkeypatch.setattr(relaunch.time, "sleep", lambda _s: None)
    assert relaunch.main(["--stopping"]) == 0
    assert said == ["waited", "started"]


# -- saving from more than one thread --------------------------------------------------------

def test_settings_saved_from_many_threads_stay_whole(tmp_path):
    c = control.Controller.__new__(control.Controller)
    c.settings, c.runtime, c.settings_path = {}, tmp_path, tmp_path / "settings.json"
    threads = [threading.Thread(target=c.save_setting, args=(f"key{i}", i)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    saved = json.loads(c.settings_path.read_text())
    assert saved == {f"key{i}": i for i in range(40)}


# -- the window, where there is a display -------------------------------------------------------

@pytest.fixture()
def window(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    from conftest import stop_panel
    from ninaivu.desktop import app
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: False)
    made = []

    def make(record=lambda _self: None):
        monkeypatch.setattr(control.Controller, "record", record)
        controller = control.Controller(root=tmp_path, cfg=SimpleNamespace(
            state_dir=tmp_path / "state", host="127.0.0.1", port=443, admin_port=3000,
            ai_engine="off", network_access=False))
        root.withdraw()
        dashboard = app.Dashboard(root, controller)
        made.append(dashboard)
        return dashboard
    yield root, make, app
    for dashboard in made:
        stop_panel(dashboard)
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_a_disk_that_does_not_answer_never_freezes_the_window(window):
    root, make, _app = window
    disk = threading.Event()
    on_tk = []

    def record(_self):
        if threading.current_thread() is threading.main_thread():
            import traceback
            on_tk.append("".join(traceback.format_stack(limit=6)))
        disk.wait(20)                                # the drive is asleep
        return None
    dashboard = make(record)
    on_tk.clear()          # the panel reads its settings once as it opens, before the window
    try:
        slowest = 0.0
        for _ in range(40):
            began = time.monotonic()
            root.update()
            slowest = max(slowest, time.monotonic() - began)
            time.sleep(0.02)
        assert slowest < 0.5
        assert not on_tk, on_tk[0] if on_tk else ''  # never read on the window's thread
        dashboard.open_in_browser(False)             # a button, too, answers at once
        root.update()
    finally:
        disk.set()


def test_the_window_closes_while_a_start_is_under_way(window):
    root, make, _app = window
    dashboard = make()
    dashboard.busy, dashboard.operation_label = True, "Starting"
    dashboard.close()
    assert dashboard.finished.is_set()


def test_closing_half_way_through_a_restart_hands_it_over(window):
    root, make, _app = window
    dashboard = make()
    handed = []
    dashboard.controller.hand_over_restart = handed.append
    dashboard.busy, dashboard.operation_label, dashboard.restart_mode = True, "Restarting", "power-saving"
    dashboard.controller.started = None
    dashboard.close()
    assert handed == ["power-saving"] and dashboard.finished.is_set()


def test_the_window_closes_even_while_the_panel_moves_files(window):
    root, make, _app = window
    dashboard = make()
    dashboard.busy, dashboard.operation_label = True, "Going back"
    dashboard.close()
    # Gone from the screen at once; the files are finished out of sight.
    assert root.state() == "withdrawn" and not dashboard.finished.is_set()
    dashboard.events.put(("done", ("The library is back.", None, None)))
    dashboard.pump()
    assert dashboard.finished.is_set()


def test_a_stuck_start_says_so_and_can_be_stopped(window, monkeypatch):
    root, make, app = window
    dashboard = make()
    card = dashboard.progress_card
    long_ago = time.time() - control.STALL_SECONDS - 60
    sample = dashboard.monitor.sample()
    sample["under_way"] = {"kind": "starting", "step": "opening the index", "progress": None,
                           "since": long_ago, "step_since": long_ago, "pid": 1}
    dashboard.display(sample)
    assert "Nothing has moved for 16 minutes" in card.warning.get()
    assert card.stop_button.winfo_manager() == "pack"

    stopped = threading.Event()
    monkeypatch.setattr(dashboard.controller, "stop_start", lambda: (stopped.set(), "The start was stopped.")[1])
    with patch.object(app.messagebox, "askyesno", return_value=True) as asked:
        dashboard.stop_this_start()
    assert "Nothing is lost" in asked.call_args.args[1]
    assert stopped.wait(5)
    deadline = time.monotonic() + 5
    while dashboard.notice.get() != "The start was stopped." and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert dashboard.notice.get() == "The start was stopped."
    assert str(card.stop_button.cget("state")) == "disabled"      # asked once


def test_an_import_in_hand_is_not_offered_a_stop(window):
    root, make, _app = window
    dashboard = make()
    sample = dashboard.monitor.sample()
    sample["under_way"] = {"kind": "starting", "step": "bringing in the index from the other computer: unpacking",
                           "progress": 0.3, "since": time.time(), "step_since": time.time(), "pid": 1}
    dashboard.display(sample)
    assert dashboard.progress_card.stop_button.winfo_manager() == ""
