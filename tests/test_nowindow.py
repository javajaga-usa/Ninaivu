"""ninaivu.nowindow: no console windows flashing up on Windows.

Run anywhere: Popen's own __init__ is swapped for a recorder, so what is
checked is the creationflags each call would hand to Windows.
"""

import subprocess

import pytest

from ninaivu import nowindow

NO_WINDOW = 0x08000000


@pytest.fixture()
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess.Popen, "__init__",
                        lambda self, *a, creationflags=0, **k: calls.append(creationflags))
    monkeypatch.delattr(subprocess.Popen, "_ninaivu_no_window", raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", NO_WINDOW, raising=False)
    yield calls
    if hasattr(subprocess.Popen, "_ninaivu_no_window"):
        delattr(subprocess.Popen, "_ninaivu_no_window")


def test_a_process_without_a_console_hides_its_childrens(recorded):
    assert nowindow.install(platform="win32", console=False)
    subprocess.Popen(["ffprobe", "x.mp4"])
    subprocess.Popen(["powershell.exe"], creationflags=0x200)   # its own process group
    assert recorded == [NO_WINDOW, NO_WINDOW | 0x200]


def test_a_child_asked_for_its_own_console_or_none_is_left_as_written(recorded):
    nowindow.install(platform="win32", console=False)
    subprocess.Popen(["relaunch"], creationflags=0x08)          # DETACHED_PROCESS
    subprocess.Popen(["cmd"], creationflags=0x10)               # CREATE_NEW_CONSOLE
    assert recorded == [0x08, 0x10]


def test_installing_twice_does_not_wrap_twice(recorded):
    nowindow.install(platform="win32", console=False)
    nowindow.install(platform="win32", console=False)
    subprocess.Popen(["x"])
    assert recorded == [NO_WINDOW]


def test_a_visible_console_and_other_systems_are_left_alone(recorded):
    assert not nowindow.install(platform="win32", console=True)   # start.cmd: output shows there
    assert not nowindow.install(platform="darwin", console=False)
    subprocess.Popen(["x"])
    assert recorded == [0]
