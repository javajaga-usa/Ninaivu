"""Starting a new version of Ninaivu over a home an older one set up.

Every installer replaces the program and only the program: the private
Python, Ninaivu's own files and the packages beside them. The library (the
photographs), the state folder (``config.json``, ``index.db``,
``archive.db``, the thumbnails, the keys) and the AI models live elsewhere,
and no installer writes to them. What an update *can* change is the index:
the first start of a new version adds the columns, tables and indexes it
needs to ``index.db`` and ``archive.db``, in place.

That is the one step that cannot be taken back by installing the old version
again, so it is guarded here, before anything opens either database:

* a copy of the state folder is made first, the same verified bundle the
  scheduled backup makes, in ``<state>/backups/before-update``, and the start
  stops if the copy cannot be made;
* a database written by a newer Ninaivu than this one (its schema number is
  higher than this version knows) is not opened at all. Starting would have
  run this version's schema over it and written the older schema number back.

The photographs are never read or written by any of this.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .. import __version__

log = logging.getLogger(__name__)

__all__ = ["prepare", "before_opening_the_index", "recorded", "UpdateError", "NewerDataError",
           "UpdateBackupError", "VERSION_FILE", "BEFORE_UPDATE", "KEEP"]

#: In the state folder: which version of Ninaivu last started on it.
VERSION_FILE = "version.json"
#: Under the state folder: the bundles taken before an update.
BEFORE_UPDATE = Path("backups") / "before-update"
#: How many of those are kept. Each is a whole index, and only the last few
#: updates are ever worth going back on.
KEEP = 3
#: Set to 1 to start without the copy (a full disk, and a person who has a
#: backup of their own). Said in the log when it is used.
SKIP_ENV = "NINAIVU_SKIP_UPDATE_BACKUP"


class UpdateError(RuntimeError):
    """This version should not start on this state folder; the text says why."""


class NewerDataError(UpdateError):
    """The index was written by a newer Ninaivu than this one."""


class UpdateBackupError(UpdateError):
    """The copy before the update could not be made."""


def recorded(state_dir: Path | str) -> dict[str, Any] | None:
    """What :data:`VERSION_FILE` says, or None (none yet, or unreadable)."""
    try:
        data = json.loads((Path(state_dir) / VERSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") else None


def _write_record(state_dir: Path, record: dict[str, Any]) -> None:
    tmp = state_dir / f".{VERSION_FILE}.tmp"
    try:
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(tmp, state_dir / VERSION_FILE)
    except OSError as exc:
        # Only means the next start makes the copy again.
        log.warning("could not note the version in %s: %s", state_dir, exc)
        tmp.unlink(missing_ok=True)


def _stored_schema(path: Path, query: str) -> int:
    """The schema number a database records, 0 when it has none."""
    if not path.is_file():
        return 0
    try:
        with closing(sqlite3.connect(str(path), timeout=30.0)) as conn:
            row = conn.execute(query).fetchone()
    except sqlite3.Error:
        # No such table yet, or not a database: init_db says so itself.
        return 0
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        return 0


def schemas(state_dir: Path | str) -> dict[str, tuple[int, int]]:
    """Per database: (the schema it records, the schema this version knows)."""
    from ..archive import database as archive_db         # noqa: PLC0415
    from . import db                                     # noqa: PLC0415

    state = Path(state_dir)
    return {
        "index.db": (_stored_schema(state / "index.db",
                                    "SELECT value FROM meta WHERE key='schema_version'"),
                     db.SCHEMA_VERSION),
        "archive.db": (_stored_schema(state / "archive.db",
                                      "SELECT value FROM config WHERE key='schema_version'"),
                       archive_db.SCHEMA_VERSION),
    }


def prepare(state_dir: Path | str, *, version: str = __version__,
            environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Make starting *version* on *state_dir* safe, or raise :class:`UpdateError`.

    Run once at start, holding the server lock, before anything opens the
    index. Returns what it found and did: ``{"from", "to", "backup"}``, with
    ``from`` None on a first start and ``backup`` None when no copy was
    needed.
    """
    environ = os.environ if environ is None else environ
    state = Path(state_dir)
    result: dict[str, Any] = {"from": None, "to": version, "backup": None}
    if not state.is_dir():
        return result
    has_data = (state / "index.db").is_file() or (state / "archive.db").is_file()
    before = recorded(state)
    old = str(before["version"]) if before else None
    result["from"] = old

    if has_data:
        for name, (stored, known) in schemas(state).items():
            if stored > known:
                said = f"Ninaivu {old}" if old else "a newer Ninaivu"
                raise NewerDataError(
                    f"{state / name} was last used by {said}, which changed it in a way "
                    f"Ninaivu {version} does not know (schema {stored}, this version knows "
                    f"{known}). Nothing was changed. Install Ninaivu {old or 'the newer version'} "
                    f"or later again; or, to go back to this version, restore the copy taken "
                    f"before that update: `ninaivu list-backups \"{state / BEFORE_UPDATE}\"`, "
                    f"then `ninaivu restore <that file>`. The photographs are not affected "
                    f"either way.")

    if old == version:
        return result
    if not has_data:
        # A new home: nothing to keep a copy of.
        _write_record(state, {"version": version, "since": time.time()})
        return result

    # An update (or a step back to an older version with the same schema):
    # the state folder is copied before this version touches it.
    if (state / "index.db").is_file():
        if str(environ.get(SKIP_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}:
            log.warning("%s is set: starting Ninaivu %s without a copy of %s first",
                        SKIP_ENV, version, state)
        else:
            from . import backup                         # noqa: PLC0415

            folder = state / BEFORE_UPDATE
            made = backup.snapshot(state, folder, note={
                "before_update": {"from": old or "1.0.4 or earlier", "to": version}})
            if made is None:
                raise UpdateBackupError(
                    f"Ninaivu {version} did not start, so that nothing in {state} changes "
                    f"before a copy of it is made, and the copy could not be made in "
                    f"{folder} (is the disk full? the server log says why). Nothing was "
                    f"changed, and the photographs are not affected. Free some space and "
                    f"start it again; or, with a backup of your own, start it once with "
                    f"{SKIP_ENV}=1.")
            backup.prune(folder, KEEP)
            result["backup"] = str(made)
            log.info("copied the state folder before the update from %s to %s: %s",
                     old or "an earlier version", version, made.name)
    _write_record(state, {"version": version, "previous": old, "since": time.time(),
                          "backup": result["backup"]})
    return result


def before_opening_the_index(state_dir: Path | str) -> bool:
    """:func:`prepare`, for a command about to open the index: says what it
    did, and returns False, having said why, when the start must stop."""
    try:
        done = prepare(state_dir)
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    if done["backup"]:
        print(f"  Updated from Ninaivu {done['from'] or 'an earlier version'} to "
              f"{done['to']}. The settings and the index were copied first, to "
              f"{done['backup']}; the photographs are not touched by an update.")
    return True
