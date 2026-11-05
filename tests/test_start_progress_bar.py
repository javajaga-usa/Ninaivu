"""The Control Panel's progress card for whatever holds up a start.

An index moved, switched, checked or repaired, one brought in from another
computer, the copy before an update, the old server finishing what it has in
hand during a restart: each says how far it has got in its note
(server/starting.py), and the panel draws it, whoever began the start.
"""

import json
import os
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ninaivu.desktop import control, progress
from ninaivu.server import starting
from ninaivu.storage import index_check, upgrade


@pytest.fixture()
def state(tmp_path):
    folder = tmp_path / "state"
    folder.mkdir()
    yield folder
    starting.end()
    starting.RESTARTING.end()


# -- the note ---------------------------------------------------------------------

def test_the_note_says_how_far_a_step_has_got(state):
    starting.begin(state)
    starting.step("checking the index: index.db", 30, 120)
    assert starting.read(state)["progress"] == 0.25
    starting.step("opening the index")                  # a step with no figure has none
    assert "progress" not in starting.read(state)


@pytest.mark.parametrize("done, total, share", [
    (0, 10, 0.0), (5, 10, 0.5), (12, 10, 1.0), (-1, 10, 0.0),
    (1, 0, None), (None, 10, None), ("x", 10, None), (float("nan"), 1, None),
])
def test_how_far_is_between_none_and_one(done, total, share):
    assert starting.fraction(done, total) == share


def test_a_percentage_for_every_file_does_not_rewrite_the_note_each_time(state):
    starting.begin(state)
    with patch.object(starting.STARTING, "_write", wraps=starting.STARTING._write) as wrote:
        for done in range(1000):
            starting.step("bringing in the index from the other computer: copying", done, 1000)
    assert wrote.call_count < 20
    starting.STARTING._write(starting.STARTING._current)  # the heartbeat's next write
    assert starting.read(state)["progress"] == pytest.approx(0.999)


def test_the_restart_note_is_its_own_and_never_copied_with_the_index(state):
    from ninaivu.storage import relocate
    starting.RESTARTING.begin(state, "waiting for Ninaivu to finish what is in hand")
    assert starting.read(state) is None
    assert starting.RESTARTING.read(state)["step"].startswith("waiting")
    assert starting.RESTARTING_FILE.startswith("ninaivu.starting")      # left out of a move
    assert relocate._RUNNING_NOTES[1] == "ninaivu.starting"
    starting.RESTARTING.end()
    assert not (state / starting.RESTARTING_FILE).exists()


# -- what the panel follows ---------------------------------------------------------

def test_the_panel_follows_a_start_and_a_restart_it_did_not_begin(state):
    assert control.under_way(state) is None
    starting.RESTARTING.begin(state, "waiting for Ninaivu to finish what is in hand")
    assert control.under_way(state)["kind"] == "restarting"
    starting.begin(state)
    starting.step("moving the index to its new folder: copying", 3, 4)
    seen = control.under_way(state)
    assert (seen["kind"], seen["progress"]) == ("starting", 0.75)     # the start says more


def test_a_note_whose_process_is_gone_is_not_followed(state):
    (state / starting.STARTING_FILE).write_text(json.dumps(
        {"pid": 2 ** 22 + 7, "step": "starting", "since": time.time(), "at": time.time()}))
    with patch.object(control, "_pid_alive", return_value=False):
        assert control.under_way(state) is None


def test_the_panel_is_told_how_far_while_it_waits(tmp_path, monkeypatch):
    c = control.Controller.__new__(control.Controller)
    c.cfg = SimpleNamespace(state_dir=tmp_path)
    c.started, c.went_back, c.mode = None, False, "standard"
    launched = time.time()
    notes = iter([{"step": "checking the index: index.db", "progress": 0.4, "since": launched},
                  {"step": "checking the index: index.db", "progress": 0.9, "since": launched},
                  {"step": "opening the index", "since": launched}])
    monkeypatch.setattr(starting, "read", lambda *_a, **_k: next(notes, None))
    ticks = iter(range(100))
    monkeypatch.setattr(c, "record", lambda: {"pid": 1} if next(ticks) >= 3 else None)
    monkeypatch.setattr(c, "capture_running_settings", lambda: None)
    monkeypatch.setattr(c, "save_mode", lambda *_: None)
    monkeypatch.setattr(c, "runtime", tmp_path, raising=False)
    monkeypatch.setattr(control, "PROGRESS_EVERY", 0)
    heard = []
    c.on_progress = lambda what, seconds, share=None: heard.append((what, share))
    clock = iter(range(0, 1000))
    assert c._wait_for_start(launched, clock=lambda: next(clock), sleep=lambda _s: None) == "Ninaivu is running."
    assert heard == [("checking the index: index.db", 0.4), ("checking the index: index.db", 0.9),
                     ("opening the index", None)]


# -- the steps that hold a start up say how far -------------------------------------

def _database(path, rows):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
        conn.execute("CREATE INDEX ta ON t (a)")
        conn.executemany("INSERT INTO t (a, b) VALUES (?, ?)",
                         ((os.urandom(16).hex(), os.urandom(12).hex()) for _ in range(rows)))
        conn.commit()


def test_checking_the_index_moves_the_bar_and_learns_its_pace(state):
    _database(state / "index.db", 30000)
    _database(state / "archive.db", 2000)
    told = []
    index_check.run(state, tell=lambda what, done=None, total=None: told.append(
        (what, None if total is None else done / total)))
    shares = [share for _, share in told if share is not None]
    assert shares == sorted(shares)                      # never goes back
    assert shares[0] == 0 and 0.85 < shares[-1] < 1      # never full before it is done
    assert {what for what, _ in told} == {"checking the index: index.db", "checking the index: archive.db"}
    pace = json.loads((state / index_check.RESULT).read_text())["pace"]
    assert pace["index.db"]["quick_check"] > 0

    # The next check goes by the pace this one measured: close to the truth.
    told.clear()
    index_check.run(state, tell=lambda what, done=None, total=None: told.append(
        (what, None if total is None else done / total)))
    last_of_index = max(share for what, share in told if what.endswith("index.db") and share is not None)
    assert last_of_index > 0.8


def test_the_copy_before_an_update_says_how_far_by_the_bytes():
    told = []
    progress_of = upgrade._update_progress(lambda *said: told.append(said))
    progress_of("copying", files_done=1, files_total=4, bytes_done=50, bytes_total=200)
    progress_of("copying", files_done=1, files_total=4, bytes_done=51, bytes_total=200)   # same percent
    progress_of("verifying", files_done=0, files_total=4, bytes_done=0, bytes_total=0)
    assert told == [(f"{upgrade.UPDATE_STEP}: copying", 50, 200),
                    (f"{upgrade.UPDATE_STEP}: checking the copied index", 0, 4)]


def test_the_restart_helper_keeps_its_note_until_the_new_server_runs(tmp_path, monkeypatch):
    from ninaivu.desktop import relaunch
    seen = []

    class Panel:
        def __init__(self):
            self.cfg = SimpleNamespace(state_dir=tmp_path)
            self.settings, self.mode, self.on_progress = {}, "standard", None

        def stop(self):
            self.on_progress("finishing what is in hand", 3, None)
            seen.append(starting.RESTARTING.read(tmp_path)["step"])
            return "Ninaivu stopped cleanly."

        def save_mode(self, _mode):
            pass

        def start(self):
            seen.append(starting.RESTARTING.read(tmp_path)["step"])
            return "Ninaivu is running."

    monkeypatch.setattr(control, "Controller", Panel)
    monkeypatch.setattr(control, "python_for_server", lambda _root: tmp_path)
    monkeypatch.setattr(control, "ninaivu_root", lambda: tmp_path)
    monkeypatch.setattr(relaunch.time, "sleep", lambda _s: None)
    assert relaunch.main([]) == 0
    assert seen == ["waiting for Ninaivu to finish what is in hand", "starting Ninaivu again"]
    assert starting.RESTARTING.read(tmp_path) is None


# -- the words on the card -----------------------------------------------------------

@pytest.mark.parametrize("step, title, detail", [
    ("checking the index: index.db", "Checking the index", "index.db"),
    ("moving the index to its new folder: copying", "Moving the index to its new folder", "Copying"),
    ("starting", "Starting Ninaivu", "Loading its programs and settings"),
    ("waiting for Ninaivu to finish what is in hand", "Finishing what is in hand",
     progress.DETAILS["Finishing what is in hand"]),
    (None, "Starting Ninaivu", "Loading its programs and settings"),
    ("repairing the index: index.db, table 3 of 12", "Repairing the index", "index.db, table 3 of 12"),
])
def test_each_step_has_a_title_and_a_line_under_it(step, title, detail):
    assert progress.describe_step(step) == (title, detail)


def test_the_figure_is_never_full_before_the_end():
    assert progress.percent(None) == ""
    assert progress.percent(0.426) == "42%"
    assert progress.percent(0.999) == "99%"
    assert progress.percent(1.0) == "100%"


def test_the_heading_and_the_time_left():
    assert progress.heading("restarting", 200) == "Restarting · 3 minutes"
    assert progress.heading("starting", None) == "Starting"
    began = (100.0, 0.10)
    assert progress.remaining(began, 0.11, 105) == ""                 # too soon to say
    assert progress.remaining(began, 0.40, 160) == "About 2 minutes left"
    assert progress.remaining(began, 0.95, 160) == "Less than a minute left"
    assert progress.remaining(None, 0.5, 160) == ""
    assert progress.remaining(began, None, 160) == ""


# -- the card itself, where there is a display ---------------------------------------

def test_the_card_shows_a_start_begun_elsewhere_and_goes_with_it(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    from conftest import stop_panel
    from ninaivu.desktop import app
    monkeypatch.setattr(control.Controller, "record", lambda _: None)
    monkeypatch.setattr("ninaivu.desktop.autostart.supported", lambda platform=None: False)
    controller = control.Controller(root=tmp_path, cfg=SimpleNamespace(
        state_dir=tmp_path / "state", host="127.0.0.1", port=443, admin_port=3000,
        ai_engine="off", network_access=False))
    dashboard = None
    try:
        root.withdraw()
        dashboard = app.Dashboard(root, controller)
        sample = dashboard.monitor.sample()
        sample["under_way"] = {"kind": "restarting", "step": "checking the index: index.db",
                               "progress": 0.42, "since": time.time() - 30}
        dashboard.display(sample)
        card = dashboard.progress_card
        assert card.shown == "follow" and card.outer.winfo_manager() == "pack"
        assert (card.title.get(), card.figure.get()) == ("Checking the index", "42%")
        assert dashboard.status.get().startswith("Restarting")
        assert str(dashboard.start_button.cget("state")) == "disabled"
        card.draw()
        sample["under_way"] = None
        dashboard.display(sample)
        assert card.shown is None and card.outer.winfo_manager() == ""
    finally:
        stop_panel(dashboard)
        root.destroy()
