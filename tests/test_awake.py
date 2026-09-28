"""Holding sleep off while the server runs.

A library that goes to sleep does not refuse connections, it swallows them —
the phone waits, then reports a timeout, and it reads as a firewall fault. So
this is worth getting right on each platform, and worth being honest about
when the request is refused.
"""

import subprocess
import sys

import pytest

from ninaivu.utils.awake import (ES_AWAYMODE_REQUIRED, ES_CONTINUOUS,
                          ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED, KeepAwake)


class FakeKernel32:
    """Records what Windows was asked for."""

    def __init__(self, accept=lambda flags: True):
        self.calls = []
        self._accept = accept

    class _Fn:
        argtypes = None
        restype = None

        def __init__(self, outer):
            self.outer = outer

        def __call__(self, flags):
            self.outer.calls.append(flags)
            return 1 if self.outer._accept(flags) else 0

    @property
    def SetThreadExecutionState(self):            # noqa: N802 — Windows name
        if not hasattr(self, "_fn"):
            self._fn = self._Fn(self)
        return self._fn


@pytest.fixture()
def windows(monkeypatch):
    def install(kernel):
        fake_ctypes = type("ctypes", (), {
            "windll": type("windll", (), {"kernel32": kernel})(),
            "c_uint": int,
        })()
        monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
        monkeypatch.setattr(sys, "platform", "win32")
        return kernel
    return install


# --- Windows ---------------------------------------------------------------

def test_windows_asks_for_away_mode_first(windows):
    kernel = windows(FakeKernel32())
    keeper = KeepAwake()
    assert keeper.start() is True
    assert kernel.calls[0] == (ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                               | ES_AWAYMODE_REQUIRED)
    assert keeper.how == "Windows away mode"


def test_windows_falls_back_when_away_mode_is_unsupported(windows):
    # Asking for away mode where it is not supported fails the whole call, so
    # the fallback is what keeps most machines awake.
    kernel = windows(FakeKernel32(
        accept=lambda flags: not flags & ES_AWAYMODE_REQUIRED))
    keeper = KeepAwake()
    assert keeper.start() is True
    assert kernel.calls[-1] == ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    assert keeper.how == "Windows"


def test_the_screen_is_never_held_on(windows):
    kernel = windows(FakeKernel32())
    KeepAwake().start()
    assert all(not (flags & ES_DISPLAY_REQUIRED) for flags in kernel.calls), (
        "a monitor left burning all night to serve photographs is a waste")


def test_stopping_releases_the_hold(windows):
    kernel = windows(FakeKernel32())
    keeper = KeepAwake()
    keeper.start()
    keeper.stop()
    assert kernel.calls[-1] == ES_CONTINUOUS, "sleep was never allowed again"
    assert keeper.active is False


def test_a_refused_request_is_reported_honestly(windows):
    windows(FakeKernel32(accept=lambda flags: False))
    keeper = KeepAwake()
    assert keeper.start() is False
    assert keeper.active is False
    assert "may sleep" in keeper.describe()


def test_a_broken_windows_call_is_not_fatal(windows, monkeypatch):
    class Exploding(FakeKernel32):
        @property
        def SetThreadExecutionState(self):        # noqa: N802
            raise OSError("kernel32 is unavailable")

    windows(Exploding())
    assert KeepAwake().start() is False


def test_stop_is_safe_without_start(windows):
    windows(FakeKernel32())
    KeepAwake().stop()          # must not raise


# --- macOS and Linux -------------------------------------------------------

def test_macos_runs_caffeinate_bound_to_our_pid(monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        return type("P", (), {"terminate": lambda s: None,
                              "wait": lambda s, timeout=None: 0,
                              "kill": lambda s: None})()

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    keeper = KeepAwake()
    assert keeper.start() is True
    assert seen["command"][:2] == ["caffeinate", "-s"]
    assert "-w" in seen["command"], (
        "without -w the helper outlives Ninaivu and the machine never sleeps again")
    assert keeper.how == "caffeinate"


def test_linux_uses_systemd_inhibit_when_present(monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        return type("P", (), {"terminate": lambda s: None,
                              "wait": lambda s, timeout=None: 0,
                              "kill": lambda s: None})()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ninaivu.utils.awake._which", lambda program: True)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    keeper = KeepAwake("Serving")
    assert keeper.start() is True
    assert seen["command"][0] == "systemd-inhibit"
    assert "--what=sleep:idle" in seen["command"]


def test_linux_without_systemd_inhibit_says_so(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ninaivu.utils.awake._which", lambda program: False)
    keeper = KeepAwake()
    assert keeper.start() is False
    assert "systemd-inhibit" in keeper.describe()


def test_a_missing_caffeinate_is_not_fatal(monkeypatch):
    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert KeepAwake().start() is False


# --- the flag ---------------------------------------------------------------

def test_allow_sleep_is_off_by_default():
    from ninaivu.__main__ import build_parser

    assert build_parser().parse_args([]).allow_sleep is False


def test_allow_sleep_can_be_asked_for():
    from ninaivu.__main__ import build_parser

    assert build_parser().parse_args(["--allow-sleep"]).allow_sleep is True


def test_it_works_as_a_context_manager(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ninaivu.utils.awake._which", lambda program: False)
    with KeepAwake() as keeper:
        assert keeper.active is False       # refused here, but no exception
