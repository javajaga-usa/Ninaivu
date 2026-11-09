"""Check the index's databases at start when there is reason to, and repair them.

There is reason to after the index has moved to another folder (relocate.py),
after "database disk image is malformed" or a like error was logged while
Ninaivu ran (:class:`Watch`), and when an administrator asks from the console.
It runs before anything opens the index, with the server lock held, so
nothing else is reading or writing it. A check on a sound index is one pass
of ``PRAGMA quick_check``, and only then.

``quick_check`` reads a database in the order of its trees, not of the file:
on a hard disk (an index on a USB drive) that is hundreds of thousands of
small reads far apart, and a large index took an hour or more. So each
database is first read straight through, which a hard disk does at full
speed, while there is memory free to hold it (:class:`_ReadAhead`, on a
thread of its own, a database ahead of the check); the check then finds every
page in memory. After a move the full check is left out: the move read every
file back and compared it with the original, so only that each database
opens and has its tables is looked at (*light*).

A damaged database is first copied aside, unchanged, as
``<name>.damaged-<when>``, and then repaired, each step checked again:

1. ``REINDEX``. Damage confined to indexes, the commonest kind after a crash
   or a bad write, is rebuilt from the tables.
2. A rebuild into a fresh file: the schema, then every row that can still be
   read, table by table, skipping over unreadable stretches; then the
   indexes and triggers, and the photo search rebuilt from the photographs.

Each repaired database must pass the full ``integrity_check`` before it is
used. When neither step gives one that does, the original is put back exactly
as it was and the console says so. Restoring from a backup is then the way
back, and it is left to the administrator (Backup & health): it would undo
everything done since that backup, which only a person can weigh. Repairing
needs room for two more copies of the database; without it nothing is changed.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
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
#: ``tell(what, done=None, total=None)``: the step in words, and how far it
#: has got when that is known (server/starting.py, for the Control Panel's bar).
Tell = Callable[..., None]
#: How many of SQLite's progress steps a check takes for each page of the
#: database, before a check here has measured it: what a table of short rows
#: takes, the most there is, so that a first estimate runs ahead of nothing.
#: Each check measures its own and keeps it (``pace`` in the result) for the
#: next one.
PACE = {"quick_check": 62.0, "integrity_check": 195.0}
#: SQLite calls the progress handler once every this many of its steps.
_STEPS = 1000
#: Reasons a database is only opened and its tables listed, not read through:
#: a move compared every file it copied with the original.
LIGHT = frozenset({"moved"})
#: Of a database's share of the bar, the reading through it; the rest is the check.
READ_SHARE = 0.6
#: How much is read at a time, and at most what share of the memory free.
_READ_CHUNK = 8 * 1024 * 1024
_READ_MEMORY = 0.5
_MAGIC = b"SQLite format 3\x00"


def _say(message: str) -> None:
    """Into the server's log: this runs before logging is set up at start."""
    print(f"Index check: {message}", flush=True)


def _record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sync_folder(folder: Path) -> None:
    """Make a rename in *folder* durable (POSIX; Windows has no folder sync)."""
    if os.name == "nt":
        return
    try:
        handle = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def _sync_file(path: Path) -> None:
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_folder(path.parent)


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


def check(path: Path, *, thorough: bool = False, light: bool = False,
          progress: Callable[[float], None] | None = None,
          pace: dict | None = None, measured: dict | None = None) -> list[str]:
    """What is wrong with the database at *path*: nothing, when it is sound.

    ``quick_check`` when looking for damage: it reads every page but does not
    compare indexes with their tables, so it is quick enough for a start.
    *thorough*, after a repair, is the full ``integrity_check``, which does.
    *progress* is told how far it has got (:func:`_follow`); *measured* is
    given the steps a page took, for the *pace* of the next check. *light*
    only opens it and lists its tables (:data:`LIGHT`).
    """
    if light:
        return _opens(path)
    pragma = "integrity_check" if thorough else "quick_check"
    try:
        with closing(sqlite3.connect(path, timeout=60)) as conn:
            counted = _follow(conn, pragma, progress, pace) if progress else None
            rows = [str(row[0]) for row in conn.execute(f"PRAGMA {pragma}({_SAMPLE})")]
            if counted and measured is not None and counted["pages"]:
                measured[pragma] = round(counted["steps"] * _STEPS / counted["pages"], 1)
    except sqlite3.DatabaseError as exc:
        return [str(exc)]
    return [] if rows == ["ok"] else rows


def _opens(path: Path) -> list[str]:
    """What is wrong with *path* that shows without reading it through: not a
    database at all, or tables SQLite cannot list."""
    try:
        with path.open("rb") as stream:
            if stream.read(len(_MAGIC)) != _MAGIC:
                return ["file is not a database"]
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=60)) as conn:
            conn.execute("SELECT name, sql FROM sqlite_master").fetchall()
            conn.execute("PRAGMA user_version").fetchone()
    except (OSError, sqlite3.DatabaseError) as exc:
        return [str(exc)]
    return []


def _memory_free() -> int:
    """Bytes of memory free for the system to keep files in; 0 when unknown."""
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:                                   # noqa: BLE001 - only an optimisation
        pass
    try:
        return int(os.sysconf("SC_AVPHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return 0


class _ReadAhead(threading.Thread):
    """Reads databases straight through, one after another, ahead of the check.

    What is read stays in the system's file cache, so the check that follows
    reads memory rather than the disk. A database bigger than what is left of
    :data:`_READ_MEMORY` of the free memory is not read (it would push out
    what was read before it), and is only checked. One thread, reading in
    order: two reading one hard disk at once would make it seek between them.
    """

    def __init__(self, paths: list[Path], room: int) -> None:
        super().__init__(daemon=True, name="ninaivu-index-read-ahead")
        self.paths, self.room = paths, room
        self.read = {path: 0 for path in paths}
        self.skipped: set[Path] = set()
        self.finished = {path: threading.Event() for path in paths}
        self.stop = threading.Event()

    def run(self) -> None:
        room = self.room
        buffer = bytearray(_READ_CHUNK)
        for path in self.paths:
            try:
                size = _size(path)
                if size > room:
                    self.skipped.add(path)
                    continue
                room -= size
                for item in (path, *_companions(path)):
                    self._through(item, buffer, path)
                    if self.stop.is_set():
                        return
            except OSError:
                self.skipped.add(path)
            finally:
                self.finished[path].set()

    def _through(self, item: Path, buffer: bytearray, path: Path) -> None:
        try:
            stream = item.open("rb", buffering=0)
        except FileNotFoundError:
            return
        with stream:
            try:
                os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
            except (AttributeError, OSError):
                pass                                    # not on Windows or a Mac
            view = memoryview(buffer)
            while not self.stop.is_set():
                got = stream.readinto(view)
                if not got:
                    break
                self.read[path] += got


def _follow(conn: sqlite3.Connection, pragma: str, progress: Callable[[float], None],
            pace: dict | None) -> dict:
    """Tell *progress* how far the check on *conn* has got, between 0 and 1.

    SQLite has no count of a check's steps beforehand: the pages of the
    database times the steps a page took the last time (*pace*), or
    :data:`PACE` before there was a last time. Held short of the end until
    the check is over: a bar that sat full while the check went on would say
    something false.
    """
    pages = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
    try:
        per_page = float((pace or {}).get(pragma) or PACE[pragma])
    except (TypeError, ValueError):
        per_page = PACE[pragma]
    expected = max(1.0, pages * per_page / _STEPS)
    counted = {"pages": pages, "steps": 0, "said": -1}

    def handler() -> int:
        counted["steps"] += 1
        share = min(0.99, counted["steps"] / expected)
        percent = int(share * 100)
        if percent != counted["said"]:
            counted["said"] = percent
            progress(share)
        return 0                                  # never interrupts the check
    conn.set_progress_handler(handler, _STEPS)
    progress(0.0)
    return counted


def _orphans(path: Path) -> int:
    """Rows whose parent row was lost in a rebuild (``foreign_key_check``):
    reported, not removed; Ninaivu's own tidying takes care of them."""
    try:
        with closing(sqlite3.connect(path, timeout=60)) as conn:
            return len(conn.execute("PRAGMA foreign_key_check").fetchall())
    except sqlite3.DatabaseError:
        return 0


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
            _sync_file(aside / item.name)
            # Nothing is changed until the copy is known to be the original.
            if _sha256(aside / item.name) != _sha256(item):
                raise OSError(f"the copy of {item.name} set aside does not match it")
    _sync_folder(aside)
    _sync_folder(path.parent)
    return aside


def _sha256(path: Path) -> bytes:
    import hashlib
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def _room(path: Path) -> bool:
    """Room for the copy set aside and a rebuilt file beside the original."""
    size = sum(p.stat().st_size for p in (path, *_companions(path)) if p.exists())
    return shutil.disk_usage(path.parent).free > 2 * size + 64 * 1024 * 1024


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
    return check(path, thorough=True)


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
            tell(f"repairing the index: {path.name}, table {number} of {len(tables)}",
                 number - 1, len(tables))
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
    left = check(new, thorough=True)
    if left:
        report["problems"] = left
        new.unlink(missing_ok=True)
        return report
    report["orphans"] = _orphans(new)
    # Written without a journal for speed: on the disk before it takes the
    # original's place, and the old journal never applied to it.
    _sync_file(new)
    for item in _companions(path):
        item.unlink(missing_ok=True)
    os.replace(new, path)
    _sync_folder(path.parent)
    return report


def repair(path: Path, problems: list[str], tell: Tell = lambda *_said: None) -> dict:
    """Repair the damaged database at *path*, keeping the original aside."""
    result: dict = {"name": path.name, "ok": False, "problems": problems[:5], "how": None}
    if not _room(path):
        result["left"] = ["There is not enough free space beside it to keep the damaged "
                          "original and build a repaired copy. Nothing was changed."]
        _say(f"{path.name} is damaged, but there is no room to repair it safely; nothing changed")
        return result
    aside = _set_aside(path)
    result["damaged_copy"] = str(aside)
    _say(f"{path.name} is damaged ({'; '.join(problems[:3])}); the original is kept in {aside}")
    tell(f"repairing the index: {path.name}")
    if not _reindex(path):
        result.update(ok=True, how="reindex", orphans=_orphans(path))
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
        _sync_file(path)
        _sync_folder(path.parent)
        result["left"] = report["problems"][:5]
        _say(f"{path.name} could not be repaired: {'; '.join(report['problems'][:3])}")
        return result
    result.update(ok=True, how="rebuild", rows=sum(report["tables"].values()),
                  skipped=report["skipped"], orphans=report.get("orphans", 0))
    if path.name == "index.db":
        _rebuild_search_later(path)
    _say(f"{path.name} rebuilt into a fresh file: {result['rows']} rows"
         + (f", {report['skipped']} unreadable stretches skipped" if report["skipped"] else "")
         + (f", {result['orphans']} rows whose parent row was lost" if result["orphans"] else ""))
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


def _size(path: Path) -> int:
    """*path* and its journal, in bytes (at least one, so each has a share)."""
    total = 0
    for item in (path, *_companions(path)):
        try:
            total += item.stat().st_size
        except OSError:
            pass
    return max(1, total)


def run(state: Path | str, tell: Tell = lambda *_said: None) -> dict:
    """Check every database of the index folder, and repair what is damaged."""
    state = Path(state)
    why = wanted(state) or {"reason": "asked"}
    began = time.time()
    _say(f"checking the index in {state} ({why.get('reason')})")
    databases = []
    present = [(name, state / name) for name in DATABASES if (state / name).is_file()]
    # One bar for the whole check, each database its share by size: the
    # index is most of it.
    sizes = {name: _size(path) for name, path in present}
    whole = sum(sizes.values()) or 1
    before = (_record(state / RESULT) or {}).get("pace")
    pace = before if isinstance(before, dict) else {}
    measured_all: dict[str, dict] = {}
    light = why.get("reason") in LIGHT
    ahead = None if light else _ReadAhead([path for _, path in present],
                                          int(_memory_free() * _READ_MEMORY))
    if ahead is not None:
        ahead.start()
    done = 0
    try:
        for name, path in present:
            problems = _check_one(name, path, done, sizes[name], whole, tell, light, ahead,
                                  pace, measured_all)
            done += sizes[name]
            if not problems:
                databases.append({"name": name, "ok": True, "how": None})
                continue
            try:
                databases.append(repair(path, problems, tell))
            except OSError as exc:
                _say(f"{name} could not be repaired: {exc}")
                databases.append({"name": name, "ok": False, "problems": problems[:5],
                                  "left": [str(exc)]})
    finally:
        if ahead is not None:
            ahead.stop.set()
    places = _locations(state / "index.db", databases, light)
    result = {"at": time.time(), "seconds": round(time.time() - began, 1),
              "reason": why.get("reason"), "detail": why.get("detail", ""),
              "light": light,
              "databases": databases, "ok": all(d["ok"] for d in databases),
              "repaired": [d["name"] for d in databases if d.get("how")],
              "pace": {**pace, **measured_all}}
    if places:
        result["locations"] = places
    _write(state / RESULT, result)
    (state / WANTED).unlink(missing_ok=True)
    _say("all sound" if result["ok"] and not result["repaired"] else
         f"repaired {', '.join(result['repaired'])}" if result["ok"] else
         "a database could not be repaired; restore it from a backup")
    return result


def _locations(path: Path, databases: list[dict], light: bool) -> dict:
    """Set ``locations`` right after the check (storage/locations.py).

    A repair puts each table together on its own, from what could be read,
    and the triggers that keep ``locations`` in step see none of it: after
    one it is rebuilt. Otherwise, when this check was asked for (damage seen
    while Ninaivu ran, or an administrator's asking) it is compared, and
    rebuilt only if it differs. A move (*light*) changed no row. Never stops
    the start: what the triggers keep is set right again by the next start
    that checks.
    """
    index = next((d for d in databases if d["name"] == "index.db"), None)
    if index is None or not index["ok"] or (light and not index.get("how")):
        return {}
    from . import locations                              # noqa: PLC0415
    try:
        with closing(sqlite3.connect(path, timeout=60)) as conn:
            if not locations.present(conn):
                return {}
            found = None if index.get("how") else locations.check(conn)
            if found is not None and found["agrees"]:
                return {"agrees": True}
            rebuilt = locations.rebuild(conn)
    except sqlite3.Error as exc:
        _say(f"the places of the photographs could not be set right: {exc}")
        return {"agrees": False, "error": str(exc)}
    _say(f"the places of {rebuilt} photographs written again"
         + (f" ({found['missing']} missing, {found['extra']} extra, "
            f"{found['wrong_fingerprint']} with the wrong fingerprint)" if found else
            " after the repair"))
    out = {"agrees": True, "rebuilt": rebuilt}
    if found is not None:
        out["found"] = {k: found[k] for k in ("missing", "extra", "wrong_fingerprint")}
    return out


def _check_one(name: str, path: Path, done: int, share: int, whole: int, tell: Tell,
               light: bool, ahead: _ReadAhead | None, pace: dict, measured_all: dict) -> list[str]:
    """Check one database, telling how far the whole check has got: first
    while it is read through (:class:`_ReadAhead`), then while it is checked."""
    words = f"checking the index: {name}"
    tell(words, done, whole)
    read_part = 0.0
    if ahead is not None:
        while not ahead.finished[path].wait(0.25):
            tell(words, done + READ_SHARE * share * min(1.0, ahead.read[path] / share), whole)
        if path not in ahead.skipped:
            read_part = READ_SHARE

    def told(part: float) -> None:
        tell(words, done + share * (read_part + (1 - read_part) * part), whole)
    measured: dict = {}
    own = pace.get(name)
    problems = check(path, light=light, progress=told,
                     pace=own if isinstance(own, dict) else None, measured=measured)
    if measured:
        measured_all[name] = measured
    return problems

