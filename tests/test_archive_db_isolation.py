"""The Archive's database follows `archive.configure()`, on every thread.

`DB_PATH` is set process-wide, and each thread keeps its own connection. When
the path changed, only the calling thread's connection was closed: every other
thread went on writing to the file the engine had just been told to stop
using. In the test suite that showed up as a "database is locked" now and then
in a full run and never in a single file — a thread left over from one test
holding the next test's brand-new archive.db while it was being set up.
"""

import sqlite3
import threading

from ninaivu.archive import database


def test_a_thread_follows_the_path_to_the_new_file(tmp_path, monkeypatch):
    first, second = tmp_path / "one.db", tmp_path / "two.db"
    monkeypatch.setattr(database, "DB_PATH", str(first))

    seen: dict[str, str] = {}
    moved = threading.Event()
    done = threading.Event()

    def worker():
        seen["before"] = database.get_db().execute(
            "PRAGMA database_list").fetchone()[2]
        moved.wait(5)
        seen["after"] = database.get_db().execute(
            "PRAGMA database_list").fetchone()[2]
        database.close_db()
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    # Wait until the worker has its connection, then move the database the way
    # `archive.configure()` does — from a different thread.
    for _ in range(200):
        if "before" in seen:
            break
        threading.Event().wait(0.01)
    database.DB_PATH = str(second)
    moved.set()
    assert done.wait(5)
    thread.join(5)

    assert seen["before"].endswith("one.db")
    assert seen["after"].endswith("two.db"), (
        "the thread kept writing to the database it was told to stop using")


def test_the_same_path_keeps_the_same_connection(tmp_path, monkeypatch):
    """One connection per thread, reused — the point of caching it at all."""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "a.db"))
    try:
        assert database.get_db() is database.get_db()
    finally:
        database.close_db()


def test_a_new_file_waits_rather_than_failing_when_another_is_opening_it(
        tmp_path, monkeypatch):
    """Switching a brand-new file into WAL needs it to itself for a moment. The
    wait was set after that switch rather than before it, so a second
    connection arriving at the same instant failed at once."""
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))

    # The holder lives on its own thread: an SQLite connection may only be
    # used by the thread that made it, so its commit has to happen there too.
    locked = threading.Event()

    def holder():
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(x)")
        conn.execute("BEGIN IMMEDIATE")          # a write lock...
        locked.set()
        threading.Event().wait(0.5)
        conn.commit()                            # ...let go half a second later
        conn.close()

    holding = threading.Thread(target=holder)
    holding.start()
    assert locked.wait(5)

    errors: list[Exception] = []

    def opener():
        try:
            database.get_db()
        except sqlite3.OperationalError as exc:
            errors.append(exc)
        finally:
            database.close_db()

    thread = threading.Thread(target=opener)
    thread.start()
    thread.join(10)
    holding.join(5)
    assert not errors, f"gave up instead of waiting: {errors}"
