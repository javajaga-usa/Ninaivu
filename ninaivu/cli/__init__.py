"""The maintenance commands, as ``ninaivu <command>``: ``backup``, ``restore``,
``list-backups``, ``reroot`` and ``offsite-restore``. In the package, so an installed copy — which
has no ``tools/`` folder — can recover itself; ``tools/*.py`` are shims kept
for a checkout's old habits and the tests."""

import sys


def use_utf8_output() -> None:
    """Print in UTF-8 whatever the terminal's code page is (a Windows console
    would otherwise fail on a tick or a Tamil folder name)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
