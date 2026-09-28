"""Photo Archive Manager — Ninaivu's consolidation engine.

This is the archive tool folded into Ninaivu. It answers a different question
from the rest of the app: the library *reads* a folder you already have, while
this *builds* that folder — sweeping photos, video and audio off however many
old drives, cards and backup folders you have accumulated into one archive laid
out as ``YYYY/MM/DD``, hash-verifying every copy, and deleting nothing.

The engine (:mod:`.scanner`, :mod:`.database`, :mod:`.safety`) is carried over
whole and deliberately unaltered in its guarantees:

* nothing is re-encoded — files are streamed byte for byte;
* nothing is deleted, ever — sources are opened read-only;
* nothing is trusted unverified — every copy is re-read and SHA-256 matched;
* nothing is left half-written — copies land on a temporary name first;
* Start is also Resume — progress lives in SQLite.

Two things change by being inside Ninaivu. Its state moves into Ninaivu's state
directory (see :func:`configure`) instead of sitting beside the source files,
and its web layer is gone: the standalone Flask app, its own port and its
Host/Origin guard are replaced by :mod:`ninaivu.archive_api`, which is mounted
on the admin console alone and protected by Ninaivu's own administrator login.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["configure", "state_paths", "VERSION", "BUILD"]

#: The archive engine's own version, kept distinct from Ninaivu's — it says
#: which consolidation behaviour you have, which is what a half-migrated
#: archive needs you to be able to answer.
VERSION = "2.9"
BUILD = "archive-as-source"

_configured: dict[str, str] = {}


def configure(state_dir: Path | str,
              min_media_bytes: int | None = None) -> dict[str, str]:
    """Point the engine's database and run logs into Ninaivu's state directory.

    Standalone, the tool kept ``archive.db`` beside its own source files, which
    made "upgrade by extracting a zip over the folder" a hazard and tied the
    archive's memory to where the program happened to be unpacked. Under Ninaivu
    both live with everything else Ninaivu owns, so the record of what has been
    archived survives reinstalling the application.

    ``min_media_bytes`` carries Ninaivu's own "what counts as a photograph"
    floor into the engine, so the library and the archive agree about which
    files are real media rather than each having its own idea.

    Safe to call more than once. Changing the path closes any connection this
    thread already holds, so the next call opens the new file.
    """
    from . import database, scanner

    state = Path(state_dir).expanduser()
    db_path = str(state / "archive.db")
    log_dir = str(state / "archive-logs")

    if min_media_bytes is not None:
        scanner.MIN_MEDIA_BYTES = int(min_media_bytes)

    if database.DB_PATH != db_path:
        try:
            database.close_db()
        except Exception:                      # nothing worth failing over
            pass
        database.DB_PATH = db_path
    scanner.LOG_DIR = log_dir

    state.mkdir(parents=True, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    _configured.clear()
    _configured.update(db=db_path, logs=log_dir)
    return dict(_configured)


def state_paths() -> dict[str, str]:
    """Where the engine is currently keeping its state."""
    if _configured:
        return dict(_configured)
    from . import database, scanner
    return {"db": database.DB_PATH, "logs": scanner.LOG_DIR}
