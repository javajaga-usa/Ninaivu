"""The eighth audit's findings about the scan's pools (2026-10-20): a stop
that waited for a hung build (D-21) and a run's share of the worker
processes fixed at its start (D-22)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import engine_helpers
from test_job_engine import _Cfg, _give_up, _owed, _shout_jobs, _store, _texts, index  # noqa: F401

from ninaivu.media import scanner as scanner_mod
from ninaivu.media.scanner import Scanner
from ninaivu.server import auth
from ninaivu.storage import db


def test_a_stop_does_not_wait_for_a_hung_build(cfg, monkeypatch):
    """D-21: the loop noticed Stop (C-14), then waited for the hung build
    when it left ``with pool``; the next scan and the server's exit queued
    behind it."""
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    release = threading.Event()
    real = scanner_mod.build_record

    def hangs(root, rel, *args, **kwargs):
        if rel.endswith("shot2.jpg"):
            release.wait(30)                 # a damaged clip OpenCV cannot time out
        return real(root, rel, *args, **kwargs)

    monkeypatch.setattr(scanner_mod, "build_record", hangs)
    scanner = Scanner(cfg)
    ended = threading.Event()
    runner = threading.Thread(
        target=lambda: (scanner._run(Path(cfg.active_root), full=True), ended.set()),
        daemon=True)
    runner.start()
    try:
        time.sleep(2.0)
        scanner._stop.set()
        assert ended.wait(5.0), "the scan ended without waiting for the hung build"
    finally:
        release.set()
        runner.join(10)
        scanner.close()


def test_a_run_grows_when_the_other_gives_its_workers_back(index, tmp_path, monkeypatch):  # noqa: F811
    """D-22: a run that started while another held every worker process kept
    one for the whole run. Asked every GROW_SECONDS, it lets the tasks in
    hand finish and goes on with a larger pool."""
    from ninaivu import engine as jobs
    from ninaivu.engine import derivations
    from ninaivu.engine import engine as engine_mod
    from ninaivu.engine.engine import JobEngine
    from ninaivu.engine.pool import WorkerPool

    monkeypatch.setitem(derivations.DERIVATIONS, "shout", derivations.Derivation(
        "shout", 5, lambda: 1, _owed, _shout_jobs(engine_helpers.slow), _store, _give_up))
    monkeypatch.setattr(engine_mod, "GROW_SECONDS", 0.0)
    for i in range(6, 21):
        db.upsert_asset(index, {"root": "/lib", "rel_path": f"{i}.jpg", "filename": f"{i}.jpg",
                                "folder": "", "ext": "jpg", "kind": "picture", "size": 1,
                                "mtime": float(i)})
    cfg = _Cfg(tmp_path)
    pool = WorkerPool(1)
    asked: list[int] = []

    def grow(current):
        asked.append(current)
        return 3

    try:
        report = JobEngine(cfg).run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"],
                                    grow=grow, pool=pool)
    finally:
        pool.shutdown()
    assert asked[0] == 1 and pool.size == 3
    assert report.done == {"shout": 20} and not report.stopped
    assert _texts(index) == {f"{i}.jpg": f"{i}.JPG" for i in range(1, 21)}


def test_the_scanner_gives_a_run_what_the_other_handed_back(scanned, monkeypatch):
    from ninaivu import engine as jobs
    cfg, conn, scanner = scanned
    monkeypatch.setattr(jobs, "processes_for", lambda cfg: 6)
    monkeypatch.setattr(scanner._jobs().__class__, "fill",
                        lambda self, conn, root, kinds: {k: 100 for k in kinds})
    seen = {}

    def fake_with(self, conn, root, ctx, kinds, engine, total, size, *, grow=None, **_):
        seen["start"] = size
        assert grow(size) == size, "nothing free yet"
        with self._processes_lock:
            self._processes_held -= 5            # the other run ends
        seen["grown"] = grow(size)
        return jobs.RunReport()

    monkeypatch.setattr(scanner_mod.Scanner, "_engine_run_with", fake_with)
    with scanner._processes_lock:
        scanner._processes_held = 5              # another run holds five
    scanner._engine_run(conn, cfg.active_root, jobs.Context(cfg=cfg, root=cfg.active_root),
                        ["text"], status="x", says=lambda t, p: "", turn=None)
    assert seen == {"start": 1, "grown": 6}
    assert scanner._processes_held == 0
