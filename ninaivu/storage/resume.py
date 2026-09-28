"""Work somebody started, remembered until it is finished or they stop it.

A restart — the Server page's button, an update, the machine rebooting —
used to end every long job that was not the library scan or the archive, and
nothing started them again: a cloud upload sat paused, a straightening survey
or storage check was simply gone, a model download never arrived. Each looked
exactly like something nobody had asked for.

So a job that should carry on is written down here when somebody starts it,
and crossed off when it finishes or when somebody stops it. Shutting Ninaivu
down stops the job without crossing it off, which is the whole point: start-up
reads what is still written down and carries it on (see
``Services._resume_jobs``). Writing it at the start rather than at shutdown is
what makes a power cut count too.

Kept in the index's ``meta`` table, one row per job, so a backup of Ninaivu's
state carries it and nothing else needs a schema.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db

PREFIX = "resume:"


def want(conn: sqlite3.Connection, name: str, args: dict[str, Any] | None = None) -> None:
    """Remember that *name* should carry on after a restart, with *args*."""
    with db._write_lock:
        db.set_meta(conn, PREFIX + name, json.dumps(args or {}))
        conn.commit()


def done(conn: sqlite3.Connection, name: str) -> None:
    """Forget *name*: it finished, or somebody stopped it."""
    with db._write_lock:
        conn.execute("DELETE FROM meta WHERE key=?", (PREFIX + name,))
        conn.commit()


def wanted(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every job still written down, with the arguments it was started with."""
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT key, value FROM meta WHERE key LIKE ?",
                            (PREFIX + "%",)):
        try:
            args = json.loads(row["value"] or "{}")
        except ValueError:
            args = {}
        out[row["key"][len(PREFIX):]] = args if isinstance(args, dict) else {}
    return out
