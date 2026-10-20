"""The job engine (ninaivu/engine): a durable list of small tasks, worked on
by worker processes, written to the index in groups.

The engine is opt-in in 2.0 (the ``job_engine`` setting); with it off a
scan runs exactly as in 1.12, and with it on a scan must leave the same
index behind.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import engine_helpers
from ninaivu import engine as jobs
from ninaivu.engine import derivations, tasks
from ninaivu.engine.engine import JobEngine
from ninaivu.engine.pool import WorkerPool
from ninaivu.storage import db


# -- the task list ---------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    out = tasks.TaskStore(tmp_path / "engine.db")
    yield out
    out.close()


def test_queuing_the_same_work_twice_queues_it_once(store):
    assert store.add("text", "/lib", 1, [(1, 1), (2, 2)], priority=5) == 2
    assert store.add("text", "/lib", 1, [(1, 1), (2, 2), (3, 3)], priority=5) == 1
    assert store.counts("/lib") == {"text": {"queued": 3}}


def test_tasks_are_handed_out_by_priority_then_rank(store):
    store.add("faces", "/lib", 1, [(10, -5.0), (11, -9.0)], priority=10)
    store.add("text", "/lib", 1, [(12, 3.0), (13, 1.0)], priority=20)
    order = [(kind, item) for _, kind, item, _ in store.claim(["text", "faces"], "/lib", 10)]
    assert order == [("text", 13), ("text", 12), ("faces", 11), ("faces", 10)]
    assert store.claim(["text", "faces"], "/lib", 10) == []
    assert store.counts("/lib") == {"text": {"running": 2}, "faces": {"running": 2}}


def test_work_left_running_by_a_stop_or_a_crash_is_queued_again(tmp_path):
    first = tasks.TaskStore(tmp_path / "engine.db")
    first.add("text", "/lib", 1, [(1, 0), (2, 0)])
    assert len(first.claim(["text"], "/lib", 1)) == 1
    first.close()                       # Ninaivu stops with one in hand
    again = JobEngine(cfg=None, store=tasks.TaskStore(tmp_path / "engine.db"))
    assert again.store.counts("/lib") == {"text": {"queued": 2}}
    again.close()


def test_a_task_that_fails_twice_is_set_aside(store):
    store.add("faces", "/lib", 1, [(7, 0)])
    (task_id, _, _, attempts), = store.claim(["faces"], "/lib", 5)
    assert not store.failed(task_id, "boom", attempts)
    (task_id, _, _, attempts), = store.claim(["faces"], "/lib", 5)
    assert attempts == tasks.MAX_ATTEMPTS
    assert store.failed(task_id, "boom", attempts)
    assert store.claim(["faces"], "/lib", 5) == []
    assert store.failed_items("faces", "/lib") == {7}
    # And it is not queued again at this version.
    assert store.add("faces", "/lib", 1, [(7, 0)]) == 0


def test_failed_tasks_go_with_their_photograph_and_are_tried_again_later(store):
    """Audit C-15: failed rows were never pruned, and never retried."""
    store.add("faces", "/lib", 1, [(7, 0), (8, 0)])
    for _ in range(tasks.MAX_ATTEMPTS):
        for task_id, _, _, attempts in store.claim(["faces"], "/lib", 5):
            store.failed(task_id, "boom", attempts)
    assert store.failed_items("faces", "/lib") == {7, 8}
    store.prune("faces", 1, keep=[7], root="/lib")         # 8 was deleted since
    assert store.failed_items("faces", "/lib") == {7}
    store.prune("faces", 1, keep=[7], root="/lib")
    assert store.failed_items("faces", "/lib") == {7}, "not retried at once"
    old = __import__("time").time() - (tasks.RETRY_FAILED_DAYS + 1) * 86400
    store._conn.execute("UPDATE tasks SET updated_at=?", (old,))   # noqa: SLF001
    store.prune("faces", 1, keep=[7], root="/lib")
    (_, _, item, attempts), = store.claim(["faces"], "/lib", 5)
    assert (item, attempts) == (7, 1), "queued again with its attempts back"


def test_a_crash_gives_back_the_attempt_it_charged(tmp_path):
    """Audit C-30: two crashes and one ordinary failure set a file aside."""
    first = tasks.TaskStore(tmp_path / "engine.db")
    first.add("text", "/lib", 1, [(1, 0)])
    first.claim(["text"], "/lib", 1)
    first.recover()
    first.claim(["text"], "/lib", 1)
    first.recover()
    (_, _, _, attempts), = first.claim(["text"], "/lib", 1)
    assert attempts == 1
    first.close()


def test_handing_back_a_task_does_not_count_the_attempt(store):
    store.add("text", "/lib", 1, [(1, 0)])
    (task_id, *_), = store.claim(["text"], "/lib", 1)
    store.release([task_id])
    (_, _, _, attempts), = store.claim(["text"], "/lib", 1)
    assert attempts == 1


def test_a_new_version_drops_the_old_tasks_and_what_is_no_longer_owed(store):
    store.add("faces", "/lib", 1, [(1, 0), (2, 0)])
    store.add("faces", "/lib", 2, [(1, 0), (2, 0), (3, 0)])
    store.prune("faces", 2, keep=[1, 3], root="/lib")
    assert store.counts("/lib") == {"faces": {"queued": 2}}
    items = sorted(item for _, _, item, _ in store.claim(["faces"], "/lib", 9))
    assert items == [1, 3]


# -- writing in groups -----------------------------------------------------

@pytest.fixture()
def index(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    for i in range(1, 6):
        db.upsert_asset(conn, {"root": "/lib", "rel_path": f"{i}.jpg", "filename": f"{i}.jpg",
                               "folder": "", "ext": "jpg", "kind": "picture", "size": 1,
                               "mtime": float(i), "thumb": f"t{i}"})
    yield conn
    db.close_all()


def test_a_group_of_writes_is_one_commit_and_one_hold_of_the_lock(index):
    ids = [r[0] for r in index.execute("SELECT id FROM assets ORDER BY id")]
    commits = []
    index.set_trace_callback(lambda sql: commits.append(sql) if sql.strip().upper() == "COMMIT" else None)
    with db.write_group(index) as group:
        for asset_id in ids:
            with group.item() as c:
                # The functions every pass already writes with: they take the
                # lock again and commit, and neither waits nor commits here.
                db.store_ai_fields(c, asset_id, ocr_text=f"words {asset_id}", ocr_version=1)
        assert db._write_lock.locked()
    index.set_trace_callback(None)
    assert len(commits) == 1
    assert not db._write_lock.locked()
    assert group.written == len(ids) and group.failed == 0
    assert index.execute("SELECT COUNT(*) FROM assets WHERE ocr_version=1").fetchone()[0] == len(ids)


def test_one_failed_write_does_not_undo_the_rest_of_its_group(index):
    ids = [r[0] for r in index.execute("SELECT id FROM assets ORDER BY id")]
    with db.write_group(index) as group:
        for asset_id in ids:
            with group.item() as c:
                c.execute("UPDATE assets SET ocr_text='x' WHERE id=?", (asset_id,))
                if asset_id == ids[2]:
                    c.execute("UPDATE no_such_table SET x=1")
    assert group.failed == 1 and group.written == len(ids) - 1
    texts = [r[0] for r in index.execute("SELECT ocr_text FROM assets ORDER BY id")]
    assert texts == ["x", "x", None, "x", "x"]


def test_another_thread_waits_for_the_group_and_then_writes(index):
    asset_id = index.execute("SELECT MIN(id) FROM assets").fetchone()[0]
    started, done = threading.Event(), threading.Event()

    def family_member():
        started.set()
        conn = db.connect(index.execute("PRAGMA database_list").fetchone()[2])
        db.update_asset(conn, asset_id, caption="Amma")
        done.set()

    with db.write_group(index) as group:
        with group.item() as c:
            c.execute("UPDATE assets SET ocr_text='y' WHERE id=?", (asset_id,))
        thread = threading.Thread(target=family_member)
        thread.start()
        started.wait(5)
        assert not done.wait(0.2)       # the lock is the group's until it ends
    thread.join(10)
    assert done.is_set()
    row = index.execute("SELECT caption, ocr_text FROM assets WHERE id=?", (asset_id,)).fetchone()
    assert tuple(row) == ("Amma", "y")


def test_the_write_lock_is_still_a_plain_lock_to_other_threads():
    lock = db._write_lock
    with lock:
        held = []
        thread = threading.Thread(target=lambda: held.append(lock.acquire(timeout=0.1)))
        thread.start()
        thread.join()
        assert held == [False]
        with lock:                      # the same thread may take it again
            assert lock.locked()
        assert lock.locked()
    assert not lock.locked()
    with pytest.raises(RuntimeError):
        lock.release()


# -- runs on worker processes ----------------------------------------------

def _shout_jobs(fn, *extra):
    def jobs_for(ctx, conn, items):
        rows = {int(r[0]): r[1] for r in conn.execute(
            f"SELECT id, filename FROM assets WHERE id IN ({','.join('?' * len(items))}) "
            "AND ocr_version < 1 AND visibility < 2", items)}
        return {i: (fn, (ctx.cfg.log, i, name, *extra)) for i, name in rows.items()}
    return jobs_for


def _store(ctx, conn, item, word):
    db.store_ai_fields(conn, item, ocr_text=word, ocr_version=1)
    return True


def _give_up(ctx, conn, item):
    db.store_ai_fields(conn, item, ocr_version=1)


def _owed(conn, root, version):
    return [(int(r[0]), float(r[0])) for r in conn.execute(
        "SELECT id FROM assets WHERE root=? AND ocr_version < ? AND visibility < 2",
        (root, version))]


@pytest.fixture()
def shouting(monkeypatch, tmp_path):
    """A derivation that stores each file's name in capitals as its text."""
    def install(fn=engine_helpers.shout, *extra):
        monkeypatch.setitem(derivations.DERIVATIONS, "shout", derivations.Derivation(
            "shout", 5, lambda: 1, _owed, _shout_jobs(fn, *extra), _store, _give_up))
    install()
    return install


class _Cfg:
    def __init__(self, tmp_path):
        self.log = str(tmp_path / "worked")
        self.db_path = tmp_path / "index.db"
        self.workers = 2


def _worked(cfg) -> list[int]:
    path = Path(cfg.log)
    return [int(f.name.split("-")[0]) for f in path.iterdir()] if path.exists() else []


def _texts(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT filename, ocr_text FROM assets ORDER BY id")}


def test_a_run_works_out_every_owed_item_on_worker_processes(index, tmp_path, shouting):
    cfg = _Cfg(tmp_path)
    engine = JobEngine(cfg)
    seen = []
    report = engine.run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"],
                        on_progress=lambda done, total, _: seen.append((done, total)))
    assert report.done == {"shout": 5} and report.left == 0 and not report.stopped
    assert _texts(index) == {f"{i}.jpg": f"{i}.JPG" for i in range(1, 6)}
    assert sorted(_worked(cfg)) == [1, 2, 3, 4, 5]
    assert seen[-1] == (5, 5)
    # Nothing owed: a second run does nothing at all.
    assert engine.run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"]).total_done() == 0
    assert engine.store.counts() == {}
    engine.close()


def test_a_hidden_item_is_not_worked_on(index, tmp_path, shouting):
    cfg = _Cfg(tmp_path)
    index.execute("UPDATE assets SET visibility=2 WHERE filename='2.jpg'")
    index.commit()
    JobEngine(cfg).run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"])
    assert _texts(index)["2.jpg"] is None
    assert 2 not in _worked(cfg)


def test_a_file_that_kills_its_worker_is_set_aside_and_the_rest_are_done(index, tmp_path, shouting):
    """In 1.12 a file that crashed a decoder took the whole server with it.
    Here the worker dies, the tasks it may have had are tried again one at a
    time, and only the file that kills a worker on its own is set aside."""
    bad = index.execute("SELECT id FROM assets WHERE filename='3.jpg'").fetchone()[0]
    shouting(engine_helpers.crash_on, bad)
    cfg = _Cfg(tmp_path)
    engine = JobEngine(cfg)
    report = engine.run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"])
    assert report.failed == {"shout": 1} and report.done == {"shout": 4}
    assert report.restarts >= 1
    texts = _texts(index)
    assert texts.pop("3.jpg") is None
    assert texts == {f"{i}.jpg": f"{i}.JPG" for i in (1, 2, 4, 5)}
    assert engine.store.failed_items("shout", "/lib") == {bad}
    # Given up on: stamped (give_up), so nothing is owed any more.
    assert index.execute("SELECT ocr_version FROM assets WHERE id=?", (bad,)).fetchone()[0] == 1


def test_a_file_a_worker_cannot_read_is_tried_twice_then_set_aside(index, tmp_path, shouting):
    bad = index.execute("SELECT id FROM assets WHERE filename='4.jpg'").fetchone()[0]
    shouting(engine_helpers.fail_on, bad)
    cfg = _Cfg(tmp_path)
    report = JobEngine(cfg).run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"])
    assert report.failed == {"shout": 1} and report.done == {"shout": 4}
    assert report.restarts == 0


def test_a_stopped_run_carries_on_where_it_was_and_does_little_twice(index, tmp_path, shouting):
    shouting(engine_helpers.slow)
    for i in range(6, 31):
        db.upsert_asset(index, {"root": "/lib", "rel_path": f"{i}.jpg", "filename": f"{i}.jpg",
                                "folder": "", "ext": "jpg", "kind": "picture", "size": 1,
                                "mtime": float(i)})
    cfg = _Cfg(tmp_path)
    engine = JobEngine(cfg)
    stop = {"now": False}

    def progress(done, total, _):
        stop["now"] = done >= 1

    first = engine.run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"],
                       should_stop=lambda: stop["now"], on_progress=progress)
    assert first.stopped and 1 <= first.total_done() < 30
    assert engine.store.counts("/lib") == {"shout": {"queued": first.left}}
    engine.close()

    again = JobEngine(cfg)          # as after a restart
    second = again.run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"])
    assert first.total_done() + second.total_done() == 30
    assert _texts(index) == {f"{i}.jpg": f"{i}.JPG" for i in range(1, 31)}
    worked = _worked(cfg)
    # Only what was in a worker's hands at the stop is worked on twice.
    assert len(worked) - 30 <= cfg.workers * 2
    again.close()


def test_the_household_coming_first_ends_the_run(index, tmp_path, shouting):
    cfg = _Cfg(tmp_path)
    report = JobEngine(cfg).run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"],
                                take_turn=lambda: False)
    assert report.stopped and report.left == 5 - report.total_done()


def test_a_hold_ends_the_workers_and_a_fresh_pool_carries_on(index, tmp_path, shouting):
    """D-20, D-36: held, the workers went on with what they had in hand, and
    a run held until the next night kept every worker for most of a day.
    Now the workers are ended before the hold, what they had goes back on
    the list, and a fresh pool finishes the run when the turn comes back."""
    import time
    shouting(engine_helpers.slow)
    for i in range(6, 31):
        db.upsert_asset(index, {"root": "/lib", "rel_path": f"{i}.jpg", "filename": f"{i}.jpg",
                                "folder": "", "ext": "jpg", "kind": "picture", "size": 1,
                                "mtime": float(i)})
    cfg = _Cfg(tmp_path)
    pool = WorkerPool(2)
    state = {"done": 0, "held": 0}
    during: dict[str, object] = {}

    def holding():
        return state["done"] >= 1 and not state["held"]

    def take_turn():
        if holding():
            state["held"] += 1
            during["pool"] = pool._pool
            before = len(_worked(cfg))
            time.sleep(1.0)                          # the household comes first
            during["worked"] = len(_worked(cfg)) - before
        return True

    def progress(done, total, _):
        state["done"] = done

    try:
        report = JobEngine(cfg).run(index, jobs.Context(cfg=cfg, root="/lib"), ["shout"],
                                    take_turn=take_turn, holding=holding,
                                    on_progress=progress, pool=pool)
    finally:
        pool.shutdown()
    assert state["held"] == 1
    assert during["pool"] is None, "no worker processes kept through the hold"
    assert during["worked"] == 0, "nothing worked on while held"
    assert report.done == {"shout": 30} and report.left == 0 and not report.stopped
    assert _texts(index) == {f"{i}.jpg": f"{i}.JPG" for i in range(1, 31)}


def test_a_library_that_goes_away_stops_the_run_and_leaves_its_work_owed(
        index, tmp_path, monkeypatch):
    """Faces read the original: a drive unplugged part way must not have
    every photograph after it marked as looked at (faceindex.detect_pass)."""
    library = tmp_path / "gone-lib"
    library.mkdir()

    def jobs_for(ctx, conn, items):
        library.rmdir() if library.exists() else None
        return {i: (engine_helpers.gone, (str(library / f"{i}.jpg"),)) for i in items}

    monkeypatch.setitem(derivations.DERIVATIONS, "look", derivations.Derivation(
        "look", 1, lambda: 1, lambda conn, root, v: _owed(conn, "/lib", v), jobs_for,
        _store, _give_up, missing_settles=False))
    cfg = _Cfg(tmp_path)
    engine = JobEngine(cfg)
    report = engine.run(index, jobs.Context(cfg=cfg, root=str(library)), ["look"])
    assert report.gone and report.total_done() == 0
    assert all(v is None for v in _texts(index).values())


# -- the derivations 2.0 ships --------------------------------------------

def test_text_is_owed_once_a_thumbnail_exists_and_reads_the_preview(index, cfg):
    cfg.ocr_min_score, cfg.ocr_max_chars = 0.4, 300
    ctx = jobs.Context(cfg=cfg, root="/lib", ocr_threads=3)
    text = derivations.DERIVATIONS["text"]
    owed = [item for item, _ in text.owed(index, "/lib", text.version())]
    assert len(owed) == 5
    index.execute("UPDATE assets SET thumb=NULL WHERE id=?", (owed[0],))
    index.commit()
    owed_now = [item for item, _ in text.owed(index, "/lib", text.version())]
    assert owed[0] not in owed_now
    jobs_for = text.jobs(ctx, index, owed)
    assert set(jobs_for) == set(owed_now)
    fn, args = jobs_for[owed_now[0]]
    assert fn.__name__ == "read_text"
    assert args[0].endswith(f"_{max(cfg.thumb_sizes)}.{ctx.preview_ext}")
    assert args[1:] == (0.4, 300, 3)


def test_text_and_faces_store_what_the_1x_passes_store(index, cfg):
    ctx = jobs.Context(cfg=cfg, root="/lib")
    ids = [r[0] for r in index.execute("SELECT id FROM assets ORDER BY id")]
    text, faces = derivations.DERIVATIONS["text"], derivations.DERIVATIONS["faces"]
    with db.write_group(index) as group:
        with group.item() as c:
            assert text.store(ctx, c, ids[0], "PONGAL")
        with group.item() as c:
            assert not text.store(ctx, c, ids[1], "missing")   # no preview: stamped empty
        with group.item() as c:
            assert not faces.store(ctx, c, ids[2], None)        # unreadable: stamped done
    rows = {r[0]: tuple(r[1:]) for r in index.execute(
        "SELECT id, ocr_text, ocr_version, face_version FROM assets")}
    assert rows[ids[0]][:2] == ("PONGAL", text.version())
    assert rows[ids[1]][:2] == (None, text.version())
    assert rows[ids[2]][2] == faces.version()
    assert not faces.missing_settles and text.missing_settles


def test_faces_of_a_photograph_hidden_meanwhile_are_not_kept(index, cfg, tmp_path):
    ctx = jobs.Context(cfg=cfg, root="/lib")
    asset_id = index.execute("SELECT MIN(id) FROM assets").fetchone()[0]
    index.execute("UPDATE assets SET visibility=2 WHERE id=?", (asset_id,))
    index.commit()
    assert derivations.DERIVATIONS["faces"].prepare(ctx, index, asset_id, ["a face"]) == []
    assert not ctx.crops_dir.exists() or not any(ctx.crops_dir.iterdir())


# -- the scan ---------------------------------------------------------------

def _rows(conn):
    skip = {"id", "indexed_at", "added_at"}
    return {r["rel_path"]: {k: r[k] for k in r.keys() if k not in skip}
            for r in conn.execute("SELECT * FROM assets")}


def test_a_scan_on_the_engine_leaves_the_same_index_as_without(cfg, tmp_path, monkeypatch):
    from ninaivu.media import scanner as scanner_mod
    from ninaivu.media.scanner import Scanner
    from ninaivu.server import auth
    monkeypatch.setattr(scanner_mod, "PROCESSES_FROM", 1)
    pools = []
    real_pool = jobs.worker_pool
    monkeypatch.setattr(jobs, "worker_pool",
                        lambda *a: pools.append(a) or real_pool(*a))
    library = Path(cfg.active_root)
    (library / "broken.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 300)

    def scan(engine_on: bool, name: str):
        cfg.state_dir = tmp_path / name
        cfg.job_engine = engine_on
        cfg.ensure_dirs()
        conn = db.init_db(cfg.db_path)
        auth.init_auth_schema(conn)
        scanner = Scanner(cfg)
        scanner._run(library, full=True)
        snap = scanner.progress.snapshot()
        scanner.close()
        return conn, snap

    before, before_snap = scan(False, "off")
    after, after_snap = scan(True, "on")
    assert before_snap["status"] == after_snap["status"] == "done"
    assert after_snap["errors"] == before_snap["errors"] >= 1
    assert len(pools) == 1             # indexed in worker processes
    assert _rows(after) == _rows(before)
    thumbs = sorted(p.name for p in (tmp_path / "on" / "thumbs").rglob("*") if p.is_file())
    assert thumbs == sorted(p.name for p in (tmp_path / "off" / "thumbs").rglob("*") if p.is_file())


def test_a_handful_of_new_files_is_indexed_on_threads(scanned, monkeypatch):
    """The watcher's rescan of the folder one photograph landed in would
    otherwise start a process per core to index one file."""
    cfg, _, scanner = scanned
    cfg.job_engine = True
    monkeypatch.setattr(jobs, "worker_pool", lambda *a: pytest.fail("processes started"))
    library = Path(cfg.active_root)
    from PIL import Image
    Image.new("RGB", (320, 240), (1, 2, 3)).save(library / "arrived.jpg")
    scanner._run(library, full=False)
    assert scanner.progress.snapshot()["status"] == "done"


def test_the_engine_is_off_unless_asked_for():
    from ninaivu.server.config import Config
    from ninaivu.server.settings_groups import GROUPS
    assert Config().job_engine is False
    assert any("job_engine" in names for names in GROUPS.values())


def test_text_and_faces_run_beside_a_graphics_processor_model_on_the_engine(scanned):
    cfg, _, scanner = scanned
    scanner.ai = type("Gpu", (), {"reads_ahead": True})()
    cfg.ai_enabled = True
    assert scanner._passes_beside([scanner._tag, scanner._derive]) == [scanner._derive]


def test_workers_on_windows_without_a_console_open_no_window(monkeypatch, tmp_path):
    from ninaivu import nowindow
    from ninaivu.engine import pool
    python = tmp_path / "python.exe"
    quiet = tmp_path / "pythonw.exe"
    python.write_bytes(b"")
    quiet.write_bytes(b"")
    monkeypatch.setattr(pool.sys, "platform", "win32")
    monkeypatch.setattr(pool.sys, "executable", str(python))
    monkeypatch.setattr(nowindow, "has_console", lambda: False)
    assert pool._quiet_executable() == str(quiet)
    monkeypatch.setattr(nowindow, "has_console", lambda: True)
    assert pool._quiet_executable() is None
    monkeypatch.setattr(pool.sys, "platform", "darwin")
    assert pool._quiet_executable() is None


def test_worker_processes_are_held_to_the_memory_free(monkeypatch):
    from types import SimpleNamespace
    from ninaivu.engine import pool
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=4 * pool.PROCESS_BYTES))
    assert pool.processes_for(SimpleNamespace(workers=20)) == 2
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=100 * pool.PROCESS_BYTES))
    assert pool.processes_for(SimpleNamespace(workers=20)) == 20
    assert pool.processes_for(SimpleNamespace(workers=0)) == 1


def test_a_worker_sees_the_models_folder_the_server_was_given(tmp_path):
    from ninaivu.media import model_catalog
    from ninaivu.server.config import Config
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    model_catalog.configure(tmp_path / "somewhere-else")
    setup = jobs.server_setup(cfg)
    with WorkerPool(1, setup) as workers:
        seen = workers.submit(_models_root_here).result(timeout=120)
    assert seen == str(tmp_path / "somewhere-else")


def _models_root_here() -> str:
    from ninaivu.media import model_catalog
    return str(model_catalog.models_root())
