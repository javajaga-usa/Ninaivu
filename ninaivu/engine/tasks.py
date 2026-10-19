"""The job engine's list of work: one row per small task, kept on disk.

A task is one photograph's worth of one derivation: "read the words in
photograph 81,204 at reader version 1", "find the faces in it at detector
version 1". Rows live in ``engine.db`` beside the index rather than in it,
so taking, finishing and failing tasks never waits on the index's write lock
and never makes the index's write-ahead log grow.

States:

* ``queued`` — waiting for a worker.
* ``running`` — handed to a worker. A row left ``running`` when Ninaivu
  stopped (or a worker died) goes back to ``queued`` the next time the
  engine opens the list (:meth:`TaskStore.recover`): nothing is lost and
  nothing is done twice beyond the one task that was in hand.
* ``done`` rows are deleted, not kept: what was done is in the index itself,
  stamped with the derivation's version, which is what decides what is owed.
* ``failed`` — tried :data:`MAX_ATTEMPTS` times and failed each time (a file
  that crashes the decoder, say). Kept so it is not handed out again at this
  version, and so the console can say how many there were.

Each task carries the version of the derivation it was made for. A
derivation whose version goes up (a better face model) makes its old rows
stale; :meth:`TaskStore.prune` drops them, and the derivation's own owed
query queues the photographs again at the new version.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

#: Times a task is handed out before it is set aside as failed. Twice: a
#: worker can die for reasons that have nothing to do with the photograph it
#: had (the computer short of memory for a moment), so one death is not
#: proof; two deaths on the same file are.
MAX_ATTEMPTS = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL,
    root       TEXT    NOT NULL,
    item       INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 0,
    rank       REAL    NOT NULL DEFAULT 0,
    state      TEXT    NOT NULL DEFAULT 'queued',
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    created_at REAL    NOT NULL,
    updated_at REAL    NOT NULL,
    UNIQUE (kind, root, item, version)
);
CREATE INDEX IF NOT EXISTS tasks_next
    ON tasks (state, priority DESC, rank, id);
CREATE INDEX IF NOT EXISTS tasks_kind
    ON tasks (kind, root, state);
"""


class TaskStore:
    """``engine.db``: the durable list of tasks. Safe from several threads."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False,
                                     isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # A lost task is queued again from the index's own stamps, so the
        # last moment's rows before a power cut are not worth a sync each.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- filling ----------------------------------------------------------
    def add(self, kind: str, root: str, version: int,
            items: Iterable[tuple[int, float]], priority: int = 0) -> int:
        """Queue *items* (``(item id, rank)``, lower rank first) for *kind*.

        Already queued, running or failed at this version: left as it is.
        Returns how many were new.
        """
        now = time.time()
        rows = [(kind, str(root), int(item), int(version), int(priority), float(rank),
                 now, now) for item, rank in items]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute("BEGIN")
            self._conn.executemany(
                "INSERT OR IGNORE INTO tasks (kind, root, item, version, priority, "
                "rank, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)", rows)
            self._conn.execute("COMMIT")
            return self._conn.total_changes - before

    def prune(self, kind: str, version: int, keep: Iterable[int] | None = None,
              root: str | None = None) -> int:
        """Drop *kind*'s rows made for any other version; with *keep*, also
        its queued rows in *root* whose item is not in *keep* (no longer owed:
        deleted, hidden, or done by another way). Returns how many went."""
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute("BEGIN")
            self._conn.execute("DELETE FROM tasks WHERE kind=? AND version<>?",
                               (kind, int(version)))
            if keep is not None and root is not None:
                self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS keep (item INTEGER PRIMARY KEY)")
                self._conn.execute("DELETE FROM keep")
                self._conn.executemany("INSERT OR IGNORE INTO keep VALUES (?)",
                                       ((int(i),) for i in keep))
                self._conn.execute(
                    "DELETE FROM tasks WHERE kind=? AND root=? AND state='queued' "
                    "AND item NOT IN (SELECT item FROM keep)", (kind, str(root)))
                self._conn.execute("DELETE FROM keep")
            self._conn.execute("COMMIT")
            return self._conn.total_changes - before

    def recover(self) -> int:
        """Put back every task left ``running`` (by a stop, a crash or a
        worker that died). Returns how many."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET state='queued', updated_at=? WHERE state='running'",
                (time.time(),))
            return cur.rowcount

    # -- taking and settling ---------------------------------------------
    def claim(self, kinds: Sequence[str], root: str, limit: int
              ) -> list[tuple[int, str, int, int]]:
        """Take up to *limit* queued tasks of *kinds* in *root*, highest
        priority first, then by rank: ``(task id, kind, item, attempts)``."""
        if not kinds or limit <= 0:
            return []
        marks = ",".join("?" * len(kinds))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                f"SELECT id, kind, item, attempts FROM tasks WHERE state='queued' "
                f"AND root=? AND kind IN ({marks}) "
                f"ORDER BY priority DESC, rank, id LIMIT ?",
                (str(root), *kinds, int(limit))).fetchall()
            if rows:
                now = time.time()
                self._conn.executemany(
                    "UPDATE tasks SET state='running', attempts=attempts+1, updated_at=? "
                    "WHERE id=?", [(now, row[0]) for row in rows])
            self._conn.execute("COMMIT")
        return [(int(r[0]), str(r[1]), int(r[2]), int(r[3]) + 1) for r in rows]

    def done(self, task_ids: Iterable[int]) -> None:
        ids = [(int(t),) for t in task_ids]
        if not ids:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.executemany("DELETE FROM tasks WHERE id=?", ids)
            self._conn.execute("COMMIT")

    def failed(self, task_id: int, error: str, attempts: int) -> bool:
        """A task that did not finish. Queued again while it has attempts
        left; set aside for good after :data:`MAX_ATTEMPTS`. Returns whether
        it was set aside."""
        final = attempts >= MAX_ATTEMPTS
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET state=?, error=?, updated_at=? WHERE id=?",
                ("failed" if final else "queued", str(error)[:500], time.time(), int(task_id)))
        return final

    def release(self, task_ids: Iterable[int]) -> None:
        """Hand back tasks that were taken but not started (a stop), without
        counting the attempt."""
        ids = [(time.time(), int(t)) for t in task_ids]
        if not ids:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.executemany(
                "UPDATE tasks SET state='queued', attempts=MAX(0, attempts-1), "
                "updated_at=? WHERE id=? AND state='running'", ids)
            self._conn.execute("COMMIT")

    # -- reading ----------------------------------------------------------
    def counts(self, root: str | None = None) -> dict[str, dict[str, int]]:
        """``{kind: {state: count}}``, for one library folder or all."""
        sql = "SELECT kind, state, COUNT(*) FROM tasks"
        params: tuple = ()
        if root is not None:
            sql += " WHERE root=?"
            params = (str(root),)
        sql += " GROUP BY kind, state"
        out: dict[str, dict[str, int]] = {}
        with self._lock:
            for kind, state, count in self._conn.execute(sql, params):
                out.setdefault(kind, {})[state] = int(count)
        return out

    def failed_items(self, kind: str, root: str) -> set[int]:
        with self._lock:
            return {int(r[0]) for r in self._conn.execute(
                "SELECT item FROM tasks WHERE kind=? AND root=? AND state='failed'",
                (kind, str(root)))}
