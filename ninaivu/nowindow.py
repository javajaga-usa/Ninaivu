"""No console windows flashing up on Windows.

The Control Panel, the tray and the server started at sign-in run under
``pythonw``, and a restart from the console starts Ninaivu with
``DETACHED_PROCESS``: either way the process has no console. Windows then
gives every console program it runs - ffprobe, powershell, netsh, openssl,
tailscale, taskkill and the rest - a console window of its own, which opens
and closes with it: the black windows that flash while Ninaivu works.

:func:`install` makes ``CREATE_NO_WINDOW`` the default for every child of
such a process, so a program run for its output never shows a window. It
covers every ``subprocess`` call in the package, and in the libraries it uses,
rather than one call site at a time, which is how the flashes kept coming
back. GUI programs (Explorer, a browser) ignore the flag and open as before.

A process with a visible console - start.cmd, a terminal - is left alone: its
children share that console and show their output in it, as they should.
"""

from __future__ import annotations

import subprocess
import sys

#: Flags a caller passes when it wants the child's console to be something
#: other than hidden; such a call is left exactly as written.
_CONSOLE_CHOICES = (getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
                    | getattr(subprocess, "DETACHED_PROCESS", 0x08))
_MARK = "_ninaivu_no_window"


def has_console() -> bool:
    """Whether this process has a console window its children can share."""
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except (AttributeError, OSError):
        return True                     # cannot tell: change nothing


def install(platform: str | None = None, console: bool | None = None) -> bool:
    """Hide the console of every child process from now on, on Windows, when
    this process has no console of its own. Returns whether it did; calling
    it again is harmless."""
    if (platform or sys.platform) != "win32":
        return False
    if getattr(subprocess.Popen, _MARK, False):
        return True
    if has_console() if console is None else console:
        return False
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    original = subprocess.Popen.__init__

    def __init__(self, *args, creationflags=0, **kwargs):
        if not creationflags & _CONSOLE_CHOICES:
            creationflags |= no_window
        original(self, *args, creationflags=creationflags, **kwargs)

    __init__.__wrapped__ = original      # type: ignore[attr-defined]
    subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]
    setattr(subprocess.Popen, _MARK, True)
    return True
