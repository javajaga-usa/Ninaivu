"""Keeping the machine awake for as long as Ninaivu is serving.

A media library that goes to sleep is worse than one that is switched off: the
phone does not get an error, it waits and then reports a timeout, which looks
exactly like a firewall problem and sends people hunting in the wrong place.
So while the server is running, the machine is asked to stay up.

Three deliberate choices:

**The screen is allowed to sleep.** Only the *system* is held awake. A monitor
left burning all night to serve photographs is a waste, and on a laptop lid it
is worse than a waste.

**It is a request, not a command.** Every platform here has the right to ignore
it — Windows on battery under some power plans, a laptop whose lid is closed, a
machine the user suspends by hand. Nothing pretends otherwise, and nothing
fails because the request was refused.

**It is released on the way out.** The whole point of an inhibitor is that it
ends. A crash still releases it, because on Windows the state dies with the
process and on the others it is a child process that exits with us.

Platforms:

* **Windows** — ``SetThreadExecutionState``. Per-thread and persistent, so it
  is set from the thread that then goes on to serve, and cleared afterwards.
* **macOS** — ``caffeinate -s -w <our pid>``, which exits by itself when Ninaivu
  does, even if Ninaivu is killed.
* **Linux** — ``systemd-inhibit``, where it exists. A machine already acting as
  a server usually has sleep disabled anyway.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

__all__ = ["KeepAwake", "keep_awake", "available"]

#: Windows execution-state flags.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
#: Lets the machine look asleep while still serving — what media servers use.
ES_AWAYMODE_REQUIRED = 0x00000040


def available() -> bool:
    """Whether anything on this platform can hold sleep off."""
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return _which("caffeinate")
    return _which("systemd-inhibit")


def _which(program: str) -> bool:
    from shutil import which

    return which(program) is not None


class KeepAwake:
    """Hold off system sleep. Use as a context manager, or call start/stop.

    ``active`` says whether the request was actually accepted, and ``how``
    names the mechanism, so the banner can tell the truth rather than claim
    something that did not happen.
    """

    def __init__(self, reason: str = "Serving the family library") -> None:
        self.reason = reason
        self.active = False
        self.how = ""
        self._process: subprocess.Popen[Any] | None = None

    # -- platforms ---------------------------------------------------------

    def _start_windows(self) -> bool:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint]
            kernel32.SetThreadExecutionState.restype = ctypes.c_uint

            # Away mode first: the machine can look off while still serving,
            # which is what a family expects from something in a cupboard. Not
            # every machine supports it, and asking for it where it is not
            # supported fails the whole call — so fall back to plain wakefulness.
            for flags, label in (
                (ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED,
                 "Windows away mode"),
                (ES_CONTINUOUS | ES_SYSTEM_REQUIRED, "Windows"),
            ):
                if kernel32.SetThreadExecutionState(flags) != 0:
                    self.how = label
                    return True
        except Exception:                        # noqa: BLE001 — advisory only
            pass
        return False

    def _start_darwin(self) -> bool:
        try:
            # -w makes it wait on our pid, so it goes away with us even if we
            # are killed rather than closed.
            self._process = subprocess.Popen(
                ["caffeinate", "-s", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return False
        self.how = "caffeinate"
        return True

    def _start_linux(self) -> bool:
        try:
            self._process = subprocess.Popen(
                ["systemd-inhibit", "--what=sleep:idle", "--who=Ninaivu",
                 f"--why={self.reason}", "--mode=block",
                 "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return False
        self.how = "systemd-inhibit"
        return True

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Ask this machine to stay awake. True if the request was accepted."""
        if self.active:
            return True
        if sys.platform == "win32":
            self.active = self._start_windows()
        elif sys.platform == "darwin":
            self.active = self._start_darwin()
        elif _which("systemd-inhibit"):
            self.active = self._start_linux()
        return self.active

    def stop(self) -> None:
        """Let the machine sleep normally again. Safe to call twice."""
        if sys.platform == "win32" and self.active:
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint]
                kernel32.SetThreadExecutionState.restype = ctypes.c_uint
                kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:                    # noqa: BLE001
                pass
        if self._process is not None:
            for finish in (self._process.terminate, self._process.kill):
                try:
                    finish()
                    self._process.wait(timeout=3)
                    break
                except (OSError, subprocess.SubprocessError):
                    continue
            self._process = None
        self.active = False

    def describe(self) -> str:
        """One line for the banner."""
        if self.active:
            return f"sleep:    held off while Ninaivu runs ({self.how})"
        if sys.platform == "win32":
            return "sleep:    could not be held off — this machine may sleep"
        if sys.platform == "darwin":
            return "sleep:    not held off (caffeinate not found)"
        return "sleep:    not held off (systemd-inhibit not found)"

    def __enter__(self) -> "KeepAwake":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def keep_awake(reason: str = "Serving the family library") -> KeepAwake:
    """Convenience: ``with keep_awake():``."""
    return KeepAwake(reason)
