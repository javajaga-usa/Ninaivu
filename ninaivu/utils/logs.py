"""Where Ninaivu's own account of itself goes.

Sixty places in this codebase call a logger. Until this file existed, none of
them reached anywhere: Python's default configuration drops everything below a
warning and sends the rest to a stream nobody is watching, because the console
window was closed weeks ago. A scan that half-finished overnight left no record
at all.

So there are two destinations, and they are deliberately different:

*A file*, rotating, in the state directory beside the index — the full account,
capped at a few megabytes so it can never be the reason a disk fills up. This is
what you read when something went wrong and you want to know what.

*A ring in memory*, holding the last few warnings and errors only — the short
account, which the console shows on the Activity tab. Nobody in a household is
going to open a log file, and the problems worth acting on are almost always in
the last dozen lines.
"""

from __future__ import annotations

import errno
import logging
import logging.handlers
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["configure", "recent", "LOG_NAME", "RecentProblems"]

LOG_NAME = "ninaivu.log"

#: How much of a warning to keep for the console. Enough to recognise it,
#: not enough to turn the panel into a log viewer.
_KEEP = 60


class RecentProblems(logging.Handler):
    """The last few warnings, for a screen rather than a file."""

    def __init__(self, keep: int = _KEEP) -> None:
        super().__init__(level=logging.WARNING)
        self.keep = keep
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:                                # noqa: BLE001
            message = str(record.msg)
        item = {
            "at": record.created,
            "level": record.levelname.lower(),
            "where": record.name,
            "what": message[:400],
        }
        if record.exc_info:
            item["detail"] = logging.Formatter().formatException(record.exc_info)[-800:]
        with self._lock:
            self._items.append(item)
            del self._items[:-self.keep]

    def items(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._items[-int(limit):]))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_problems = RecentProblems()
_configured = False
_lock = threading.Lock()


def _unroutable_connection(record: logging.LogRecord) -> bool:
    """zeroconf announcing on a connection that cannot carry it: debug, not a warning.

    It announces the name on every network connection, and a VPN (Tailscale's
    100.x) or a link-local address (169.254.x) refuses with "Can't assign
    requested address" and a traceback, in the console's problems. The name
    is still announced on the home network, where it is looked up.
    """
    exc = record.exc_info[1] if record.exc_info else None
    refused = {errno.EADDRNOTAVAIL, getattr(errno, "WSAEADDRNOTAVAIL", errno.EADDRNOTAVAIL)}
    if isinstance(exc, OSError) and (exc.errno in refused
                                     or getattr(exc, "winerror", None) in refused):
        record.levelno, record.levelname = logging.DEBUG, "DEBUG"
    return True


def configure(state_dir: Path | str, debug: bool = False,
              keep_files: int = 3, max_bytes: int = 2_000_000) -> Path | None:
    """Point the root logger at a file in *state_dir*, once per process.

    Returns the path being written to, or None if it could not be opened —
    which must never stop the application starting. A server that will not run
    because it cannot write its log is worse than one with no log.
    """
    global _configured
    with _lock:
        if _configured:
            return Path(state_dir) / LOG_NAME

        root = logging.getLogger()
        root.setLevel(logging.DEBUG if debug else logging.INFO)

        # Someone else's library shouting at DEBUG is not our account of
        # ourselves, and would bury it. Werkzeug serves every HTTPS start and
        # writes a line per request: a gallery of thumbnails was 13,000 lines
        # in five hours, enough to rotate the night's scan out of the file.
        for noisy in ("PIL", "urllib3", "watchdog", "comtypes", "waitress",
                      "werkzeug"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        # ExifRead warns "File format not recognized" for every video and
        # every file without EXIF. The date chain expects that and moves on;
        # as warnings they filled the console's problem list, 7,000 deep.
        logging.getLogger("exifread").setLevel(logging.ERROR)

        logging.getLogger("zeroconf").addFilter(_unroutable_connection)

        root.addHandler(_problems)

        path = Path(state_dir) / LOG_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=keep_files, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            handler.setLevel(logging.DEBUG if debug else logging.INFO)
            root.addHandler(handler)
        except OSError:
            path = None

        _configured = True
        logging.getLogger(__name__).info(
            "Ninaivu started; logging at %s", "debug" if debug else "info")
        return path


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """The last warnings and errors, newest first."""
    return _problems.items(limit)


def forget() -> None:
    """Drop what is held — for tests, and for the console's clear button."""
    _problems.clear()


def since(seconds: float) -> int:
    """How many problems in the last *seconds*. For a badge."""
    cutoff = time.time() - seconds
    return sum(1 for item in _problems.items(_KEEP) if item["at"] >= cutoff)
