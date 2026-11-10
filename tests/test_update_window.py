"""The Update window (desktop/updating.py): how to update, bringing the
library across with each step ticked off, and going back."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.desktop import updating


def test_the_three_steps_name_this_folder_and_say_nothing_is_looked_up():
    steps = updating.update_steps("3.7.0", Path("/Users/jaga/Downloads"))
    assert [s["title"] for s in steps] == [
        "Download the newer version", "Unpack it beside this folder", "Open the newer Ninaivu"]
    assert "never looks for updates by itself" in steps[0]["detail"]
    assert "/Users/jaga/Downloads" in steps[1]["detail"] and "same drive" in steps[1]["detail"]


def test_what_comes_says_where_and_whether_it_moves(tmp_path):
    older = tmp_path / "Ninaivu-3.7.0"
    inside = SimpleNamespace(version="3.7.0", path=older, state=older / "data",
                             models=older / "data" / "ai-models", control=older / "data" / "control")
    said = updating.what_comes(inside, "4.0.0")
    assert (said["from"], said["to"], said["stays"]) == ("3.7.0", "4.0.0", False)
    assert said["things"] == [("The index and settings", str(older / "data"))]
    assert "moved, not copied" in said["how"]
    home = tmp_path / "home" / ".ninaivu"
    (tmp_path / "models").mkdir()
    shared = SimpleNamespace(version=None, path=older, state=home, models=tmp_path / "models",
                             control=home / "control")
    said = updating.what_comes(shared, "4.0.0")
    assert said["stays"] and "takes them over" in said["how"] and said["from"] == "an older version"
    assert ("The AI models", str(tmp_path / "models")) in said["things"]


@pytest.fixture()
def root():
    tk = pytest.importorskip("tkinter")
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    window.withdraw()
    yield window
    window.destroy()


def _buttons(widget):
    from tkinter import ttk
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            yield child
        yield from _buttons(child)


def _pressing(root, text, then=None, *, after=200):
    """Press the button labelled *text* in the newest window once it is up."""
    import tkinter as tk

    def press():
        windows = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        if not windows:
            root.after(50, press)
            return
        found = [b for b in _buttons(windows[-1]) if b.cget("text") == text]
        if not found or str(found[0].cget("state")) == "disabled":
            root.after(50, press)
            return
        found[0].invoke()
        if then is not None:
            root.after(after, then)
    root.after(after, press)


def test_bringing_the_library_ticks_each_step_and_ends_with_done(root, tmp_path):
    older = SimpleNamespace(version="3.7.0", path=tmp_path / "old", state=tmp_path / "old" / "data",
                            models=tmp_path / "m", control=tmp_path / "c")
    stopped, brought = [], []

    def bring(folder, stop_older):
        stop_older(folder.state)
        brought.append(folder)
        return {"sign_in": False}

    _pressing(root, "Bring my library across", then=lambda: _pressing(root, "Done", after=50))
    started = time.monotonic()
    assert updating.offer_window(root, updating.Look(root), older, "4.0.0", bring=bring,
                                 stop=stopped.append) is True
    assert brought == [older] and stopped == [older.state]
    assert time.monotonic() - started < 10


def test_not_now_moves_nothing(root, tmp_path):
    older = SimpleNamespace(version="3.7.0", path=tmp_path / "old", state=tmp_path / "old" / "data",
                            models=tmp_path / "m", control=tmp_path / "c")
    _pressing(root, "Not now")
    assert updating.offer_window(root, updating.Look(root), older, "4.0.0",
                                 bring=lambda *a, **k: pytest.fail("moved"),
                                 stop=lambda _s: pytest.fail("stopped")) is False


def test_a_move_that_fails_is_said_and_can_be_tried_again(root, tmp_path):
    older = SimpleNamespace(version="3.7.0", path=tmp_path / "old", state=tmp_path / "old" / "data",
                            models=tmp_path / "m", control=tmp_path / "c")
    tries = []

    def bring(folder, stop_older):
        tries.append(1)
        if len(tries) == 1:
            raise OSError("The older Ninaivu is still running")
        stop_older(folder.state)
        return {}

    _pressing(root, "Bring my library across",
              then=lambda: _pressing(root, "Try again", then=lambda: _pressing(root, "Done", after=50)))
    assert updating.offer_window(root, updating.Look(root), older, "4.0.0", bring=bring,
                                 stop=lambda _s: None) is True
    assert len(tries) == 2


def test_the_update_window_opens_the_page_and_the_folder_only_when_asked(root, tmp_path):
    opened, shown, asked = [], [], []
    window = updating.how_to_update(
        root, updating.Look(root), version="4.0.0", folder=tmp_path / "Ninaivu-4.0.0",
        go_back=("3.7.0", str(tmp_path / "Ninaivu-3.7.0"), lambda: asked.append(True)),
        open_url=opened.append, open_folder=shown.append)
    assert opened == [] and shown == []
    buttons = {b.cget("text"): b for b in _buttons(window)}
    assert set(buttons) == {"Open the downloads page", "Show the folder", "Go back to 3.7.0…", "Close"}
    buttons["Open the downloads page"].invoke()
    buttons["Show the folder"].invoke()
    assert opened == [updating.DOWNLOADS] and shown == [tmp_path]
    assert updating.DOWNLOADS.endswith('/releases'), 'the page, not the address a lookup asks'
    buttons["Go back to 3.7.0…"].invoke()
    assert asked == [True]


def test_without_an_older_copy_there_is_no_going_back(root, tmp_path):
    window = updating.how_to_update(root, updating.Look(root), version="4.0.0",
                                    folder=tmp_path / "Ninaivu-4.0.0", open_url=lambda _u: None,
                                    open_folder=lambda _p: None)
    assert not any("Go back" in b.cget("text") for b in _buttons(window))
    window.destroy()
