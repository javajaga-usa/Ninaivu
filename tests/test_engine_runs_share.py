"""Two engine runs at once (audit C-16, C-31).

A run owns the tasks it holds: when one ends without handing them back, the
next run takes them up again, and a run still going never loses its tasks to
another. Two runs of one scanner share one budget of worker processes.
"""
from __future__ import annotations

import sqlite3
import threading

from ninaivu import engine as jobs
from ninaivu.engine import tasks
from ninaivu.engine.engine import JobEngine


def test_a_finished_run_s_stuck_tasks_go_back_but_a_live_run_s_stay(tmp_path):
    store = tasks.TaskStore(tmp_path / "engine.db")
    store.add("text", "/lib", 1, [(1, 0), (2, 0), (3, 0)])
    mine = store.claim(["text"], "/lib", 1, owner="live")
    gone = store.claim(["text"], "/lib", 1, owner="over")
    assert mine and gone
    # The run "over" ended and its hand-back failed: its task is stuck.
    assert store.recover_stale({"live"}) == 1
    assert store.counts("/lib") == {"text": {"queued": 2, "running": 1}}
    held = sqlite3.connect(tmp_path / "engine.db").execute(
        "SELECT item, owner FROM tasks WHERE state='running'").fetchall()
    assert held == [(mine[0][2], "live")]
    # A queued row is never claimed twice: the second claim finds none of it.
    assert {t[2] for t in store.claim(["text"], "/lib", 5, owner="next")}.isdisjoint({mine[0][2]})
    store.close()


def test_a_run_starting_takes_up_what_a_finished_run_left(tmp_path, monkeypatch):
    store = tasks.TaskStore(tmp_path / "engine.db")
    store.add("text", "/lib", 1, [(1, 0)])
    store.claim(["text"], "/lib", 1, owner="a-run-that-ended")
    engine = JobEngine(cfg=None, store=store)
    seen = {}
    monkeypatch.setattr(engine, "_run", lambda *a, **k: seen.update(
        counts=store.counts("/lib")) or jobs.RunReport())

    class Ctx:
        root = "/lib"

    engine.run(None, Ctx(), ["text"])
    assert seen["counts"] == {"text": {"queued": 1}}
    assert engine._live == set()
    engine.close()


def test_an_older_engine_list_gains_its_owner_column(tmp_path):
    path = tmp_path / "engine.db"
    old = sqlite3.connect(path)
    old.executescript(tasks._SCHEMA.replace("    owner      TEXT,\n", ""))
    old.execute("INSERT INTO tasks(kind, root, item, version, state, created_at, updated_at) "
                "VALUES ('text', '/lib', 7, 1, 'queued', 0, 0)")
    old.commit()
    old.close()
    store = tasks.TaskStore(path)
    assert store.claim(["text"], "/lib", 1, owner="r") == [(1, "text", 7, 1)]
    store.close()


def test_two_runs_of_one_scanner_share_its_worker_processes(scanned, monkeypatch):
    cfg, conn, scanner = scanned
    monkeypatch.setattr(jobs, "processes_for", lambda cfg: 6)
    sizes, release = [], threading.Event()
    both = threading.Barrier(3)

    def fake_with(self, conn, root, ctx, kinds, engine, total, size, **_):
        sizes.append(size)
        both.wait(5)
        release.wait(5)
        return jobs.RunReport()

    from ninaivu.media import scanner as scanner_mod
    monkeypatch.setattr(scanner_mod.Scanner, "_engine_run_with", fake_with)
    monkeypatch.setattr(scanner._jobs().__class__, "fill",
                        lambda self, conn, root, kinds: {k: 100 for k in kinds})
    runs = [threading.Thread(target=scanner._engine_run, args=(
        conn, cfg.active_root, None, ["text"]),
        kwargs={"status": "x", "says": lambda t, p: "", "turn": None}) for _ in range(2)]
    for run in runs:
        run.start()
    both.wait(5)
    release.set()
    for run in runs:
        run.join(5)
    assert sorted(sizes) == [1, 6]
    assert scanner._processes_held == 0
