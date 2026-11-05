"""Check the index's databases at start when there is reason to, and repair them.

There is reason to after the index has moved to another folder (relocate.py),
after "database disk image is malformed" or a like error was logged while
Ninaivu ran (:class:`Watch`), and when an administrator asks from the console.
It runs before anything opens the index, with the server lock held, so
nothing else is reading or writing it. A check on a sound index is one pass
of ``PRAGMA quick_check``: a minute or so on a large one, and only then.

A damaged database is first copied aside, unchanged, as
``<name>.damaged-<when>``, and then repaired, each step checked again:

1. ``REINDEX``. Damage confined to indexes, the commonest kind after a crash
   or a bad write, is rebuilt from the tables.
2. A rebuild into a fresh file: the schema, then every row that can still be
   read, table by table, skipping over unreadable stretches; then the
   indexes and triggers, and the photo search rebuilt from the photographs.

When neither gives a sound database the original is left as it was and the
console says so: a backup is the way back then (Backup & health).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

#: The databases of the index folder (storage/backup.py DATABASES).
DATABASES = ("index.db", "archive.db", "mugil-sent.db")
WANTED = "index-check-wanted.json"
RESULT = "index-check-result.json"
#: What SQLite says when a database file is damaged.
DAMAGE = ("database disk image is malformed", "file is not a database",
          "database corruption", "malformed database schema")
_SAMPLE = 20
_CHUNK = 500

log = logging.getLogger(__name__)
Tell = Callable[[str], None]


def _say(message: str) -> None:
    """Into the server's log: this runs before logging is set up at start."""
    print(f"Index check: {message}", flush=True)


def _record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def ask(state: Path | str, reason: str, detail: str = "") -> None:
    """Have the next start check the index. The first reason given is kept."""
    path = Path(state) / WANTED
    if path.exists():
        return
    try:
        _write(path, {"reason": reason, "detail": detail[:500], "at": time.time()})
    except OSError:
        pass


def wanted(state: Path | str) -> dict | None:
    record = _record(Path(state) / WANTED)
    return record or ((Path(state) / WANTED).exists() and {"reason": "asked"}) or None


def status(state: Path | str) -> dict:
    """For the console: a check waiting for the next start, and the last one."""
    return {"wanted": wanted(state), "last": _record(Path(state) / RESULT) or None}


class Watch(logging.Handler):
    """Asks for a check at the next start the first time the log says a
    database is damaged. The error is seen wherever it happens (a scan, the
    describer, a request), without each of them having to report it."""

    def __init__(self, state: Path | str):
        super().__init__(logging.WARNING)
        self.state = Path(state)
        self.asked = False

    def emit(self, record: logging.LogRecord) -> None:
        if self.asked:
            return
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text = f"{text} {record.exc_info[1]}"
            if any(words in text.lower() for words in DAMAGE):
                self.asked = True
                ask(self.state, "error", f"{record.name}: {text}")
        except Exception:                                   # noqa: BLE001 — a log handler never raises
            pass


def watch(state: Path | str) -> Watch:
    """Attach a :class:`Watch` for *state* to the root logger, once."""
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, Watch):
            handler.state = Path(state)
            return handler
    handler = Watch(state)
    root.addHandler(handler)
    return handler


# --------------------------------------------------------------------------
# Checking


def check(path: Path) -> list[str]:
    """What is wrong with the database at *path*: nothing, when it is sound."""
    try:
        with closing(sqlite3.connect(path, timeout=60)) as conn:
            rows = [str(row[0]) for row in conn.execute(f"PRAGMA quick_check({_SAMPLE})")]
    except sqlite3.DatabaseError as exc:
        return [str(exc)]
    return [] if rows == ["ok"] else rows


def _companions(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _set_aside(path: Path) -> Path:
    """Copy *path* (and its journal) aside unchanged; only the newest copy of
    a database is kept, as each is as large as the index."""
    for old in path.parent.glob(f"{path.name}.damaged-*"):
        try:
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()
        except OSError:
            pass
    aside = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
    aside.mkdir()
    for item in (path, *_companions(path)):
        if item.exists():
            shutil.copy2(item, aside / item.name)
    return aside


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------
# Repairing


def _reindex(path: Path) -> list[str]:
    try:
        with closing(sqlite3.connect(path, timeout=60)) as conn:
            conn.execute("REINDEX")
            conn.commit()
    except sqlite3.DatabaseError as exc:
        return [str(exc)]
    return check(path)


def _salvage(old: sqlite3.Connection, new: sqlite3.Connection, table: str,
             columns: list[str], alias: bool) -> tuple[int, int]:
    """Copy what can be read of *table* from *old* to *new*, a stretch at a
    time, stepping over stretches that cannot be read. The rows copied, and
    the stretches skipped.

    *alias*: the table's INTEGER PRIMARY KEY is its rowid, so the rowid comes
    with the columns; otherwise it is copied as well, as search and the
    photographs' own ids may rely on it."""
    names = ", ".join(_quote(c) for c in columns)
    target = names if alias else f"rowid, {names}"
    marks = ", ".join("?" for _ in range(len(columns) + (0 if alias else 1)))
    q = _quote(table)
    try:
        old.execute(f"SELECT rowid FROM {q} LIMIT 0").fetchall()
    except sqlite3.DatabaseError:
        return _bulk(old, new, table, columns), 1             # WITHOUT ROWID
    copied = skipped = 0
    last = -(2 ** 63)
    while True:
        step = 0
        while True:
            try:
                rows = old.execute(
                    f"SELECT rowid, {names} FROM {q} WHERE rowid > ? ORDER BY rowid LIMIT {_CHUNK}",
                    (last + step,)).fetchall()
                break
            except sqlite3.DatabaseError:
                # Unreadable from here on: look further along, ever further.
                step = 1 if step == 0 else step * 2
                if last + step >= 2 ** 63 - 1:
                    return copied, skipped + 1
        if step:
            skipped += 1
        if not rows:
            return copied, skipped
        new.executemany(f"INSERT OR IGNORE INTO {q} ({target}) VALUES ({marks})",
                        [row[1:] for row in rows] if alias else rows)
        copied += len(rows)
        last = rows[-1][0]


def _bulk(old: sqlite3.Connection, new: sqlite3.Connection, table: str,
          columns: list[str]) -> int:
    """Copy *table* whole; raises when a part of it cannot be read."""
    names = ", ".join(_quote(c) for c in columns)
    marks = ", ".join("?" for _ in columns)
    q = _quote(table)
    copied = 0
    cursor = old.execute(f"SELECT {names} FROM {q}")
    while rows := cursor.fetchmany(5000):
        new.executemany(f"INSERT INTO {q} ({names}) VALUES ({marks})", rows)
        copied += len(rows)
    return copied


def _rebuild(path: Path, tell: Tell) -> dict:
    """Everything that can be read of *path*, into a fresh file put in its place."""
    new = path.with_name(path.name + ".rebuilding")
    for item in (new, *_companions(new)):
        item.unlink(missing_ok=True)
    report: dict = {"tables": {}, "skipped": 0, "problems": []}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=60)) as old:
            schema = old.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                                 "WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
            version = old.execute("PRAGMA user_version").fetchone()[0]
            application = old.execute("PRAGMA application_id").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        report["problems"] = [f"its list of tables cannot be read: {exc}"]
        return report
    virtual = [name for kind, name, _, sql in schema
               if kind == "table" and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")]

    def shadow(name: str) -> bool:
        return any(name.startswith(f"{v}_") for v in virtual)

    with closing(sqlite3.connect(new, timeout=60)) as conn, \
            closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=60)) as old:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        tables = []
        for kind, name, _, sql in schema:
            if kind == "table" and not name.startswith("sqlite_") and not shadow(name):
                conn.execute(sql)
                if name not in virtual:
                    tables.append(name)
        conn.commit()
        for number, table in enumerate(tables, 1):
            tell(f"repairing the index: {path.name}, table {number} of {len(tables)}")
            info = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
            columns = [row[1] for row in info]
            keys = [row for row in info if row[5]]
            alias = len(keys) == 1 and str(keys[0][2]).upper() == "INTEGER"
            try:
                report["tables"][table] = _bulk(old, conn, table, columns)
            except sqlite3.DatabaseError:
                conn.rollback()
                conn.execute(f"DELETE FROM {_quote(table)}")
                copied, skipped = _salvage(old, conn, table, columns, alias)
                report["tables"][table] = copied
                report["skipped"] += skipped
            conn.commit()
        if "sqlite_sequence" in {name for _, name, _, _ in schema}:
            try:
                rows = old.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
                conn.executemany("INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES (?, ?)", rows)
            except sqlite3.DatabaseError:
                pass                       # rebuilt from the rows as they are added
        conn.commit()
        for kind, name, table, sql in schema:
            if kind in ("index", "trigger", "view") and not shadow(table):
                try:
                    conn.execute(sql)
                except sqlite3.DatabaseError as exc:
                    report["problems"].append(f"{kind} {name}: {exc}")
        for table in virtual:
            try:
                conn.execute(f"INSERT INTO {_quote(table)}({_quote(table)}) VALUES('rebuild')")
            except sqlite3.DatabaseError as exc:
                report["problems"].append(f"{table}: {exc}")
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.execute(f"PRAGMA application_id = {int(application)}")
        conn.commit()
    if report["problems"]:
        new.unlink(missing_ok=True)
        return report
    left = check(new)
    if left:
        report["problems"] = left
        new.unlink(missing_ok=True)
        return report
    for item in _companions(path):
        item.unlink(missing_ok=True)
    os.replace(new, path)
    return report


def repair(path: Path, problems: list[str], tell: Tell = lambda _what: None) -> dict:
    """Repair the damaged database at *path*, keeping the original aside."""
    result: dict = {"name": path.name, "ok": False, "problems": problems[:5], "how": None}
    aside = _set_aside(path)
    result["damaged_copy"] = str(aside)
    _say(f"{path.name} is damaged ({'; '.join(problems[:3])}); the original is kept in {aside}")
    tell(f"repairing the index: {path.name}")
    if not _reindex(path):
        result.update(ok=True, how="reindex")
        _say(f"{path.name} repaired by rebuilding its indexes")
        return result
    try:
        report = _rebuild(path, tell)
    except (OSError, sqlite3.Error) as exc:
        report = {"problems": [str(exc)], "tables": {}, "skipped": 0}
    if report["problems"]:
        # Put back exactly what was there: the REINDEX may have changed it.
        shutil.copy2(aside / path.name, path)
        for item in _companions(path):
            item.unlink(missing_ok=True)
            if (aside / item.name).exists():
                shutil.copy2(aside / item.name, item)
        result["left"] = report["problems"][:5]
        _say(f"{path.name} could not be repaired: {'; '.join(report['problems'][:3])}")
        return result
    result.update(ok=True, how="rebuild", rows=sum(report["tables"].values()),
                  skipped=report["skipped"])
    if path.name == "index.db":
        _rebuild_search_later(path)
    _say(f"{path.name} rebuilt into a fresh file: {result['rows']} rows"
         + (f", {report['skipped']} unreadable stretches skipped" if report["skipped"] else ""))
    return result


def _rebuild_search_later(path: Path) -> None:
    """The photo search is rebuilt again once Ninaivu runs (storage/db.py)."""
    try:
        from .db import FTS_REBUILD_KEY, set_meta
        with closing(sqlite3.connect(path, timeout=60)) as conn:
            set_meta(conn, FTS_REBUILD_KEY, "1")
            conn.commit()
    except (ImportError, sqlite3.Error):
        pass


def run(state: Path | str, tell: Tell = lambda _what: None) -> dict:
    """Check every database of the index folder, and repair what is damaged."""
    state = Path(state)
    why = wanted(state) or {"reason": "asked"}
    began = time.time()
    _say(f"checking the index in {state} ({why.get('reason')})")
    databases = []
    for name in DATABASES:
        path = state / name
        if not path.is_file():
            continue
        tell(f"checking the index: {name}")
        problems = check(path)
        if not problems:
            databases.append({"name": name, "ok": True, "how": None})
            continue
        try:
            databases.append(repair(path, problems, tell))
        except OSError as exc:
            _say(f"{name} could not be repaired: {exc}")
            databases.append({"name": name, "ok": False, "problems": problems[:5],
                              "left": [str(exc)]})
    result = {"at": time.time(), "seconds": round(time.time() - began, 1),
              "reason": why.get("reason"), "detail": why.get("detail", ""),
              "databases": databases, "ok": all(d["ok"] for d in databases),
              "repaired": [d["name"] for d in databases if d.get("how")]}
    _write(state / RESULT, result)
    (state / WANTED).unlink(missing_ok=True)
    _say("all sound" if result["ok"] and not result["repaired"] else
         f"repaired {', '.join(result['repaired'])}" if result["ok"] else
         "a database could not be repaired; restore it from a backup")
    return result
