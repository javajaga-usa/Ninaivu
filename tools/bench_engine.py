#!/usr/bin/env python3
"""A first scan with the job engine off and then on: time, cores, write waits.

    python tools/bench_engine.py [photos] [workers]

Builds a throwaway library of *photos* 12-megapixel JPEGs with words on them
(default 120; tools/bench_cores.py makes them) and scans it from nothing
twice into fresh indexes: once as 1.12 does (``job_engine`` off) and once on
the job engine. Text reading is on, and finding faces when the face models
are in NINAIVU_AI_MODELS_DIR. While each scan runs, a stand-in family member
saves a caption every 50 ms, the way somebody tagging photographs during a
scan does, and the time each save waited is measured.

For each scan it prints the time of the indexing and of the text and face
analysis, how many cores were busy on average (processor time of the server
and its worker processes over wall time), how many times the index's write
lock was taken during the analysis, and how long the family member's saves
waited. *workers* is the Tuning page's scan workers (default: every core).
Nothing here touches the real index.
"""
from __future__ import annotations

import os
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_cores import build_photos                   # noqa: E402

sys.path.insert(0, os.environ.get("NINAIVU_SRC") or str(Path(__file__).resolve().parents[1]))

PHOTOS = int(sys.argv[1]) if len(sys.argv) > 1 else 120
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else (os.cpu_count() or 1)


def processor() -> float:
    """Processor seconds of this process and every worker it has had."""
    import psutil
    me = psutil.Process()
    used = me.cpu_times()
    total = used.user + used.system + used.children_user + used.children_system
    for child in me.children(recursive=True):
        try:
            times = child.cpu_times()
            total += times.user + times.system
        except psutil.Error:
            pass
    return total


class Locks:
    """Counts the times the scan takes the index's write lock afresh (the
    stand-in family member's own saves are not counted)."""

    def __init__(self) -> None:
        from ninaivu.storage import db
        self.lock = db._write_lock
        self.count = 0
        self._acquire = type(self.lock).acquire
        counter = self

        def acquire(lock, blocking=True, timeout=-1):
            fresh = lock._owner != threading.get_ident()
            got = counter._acquire(lock, blocking, timeout)
            if got and fresh and threading.current_thread().name != "family":
                counter.count += 1
            return got
        type(self.lock).acquire = acquire

    def close(self) -> None:
        type(self.lock).acquire = self._acquire


def scan(work: Path, root: Path, engine: bool, faces: bool) -> dict:
    from ninaivu.media import scanner as scanner_mod
    from ninaivu.server import auth, tuning
    from ninaivu.server.config import Config
    from ninaivu.storage import db
    from ninaivu.utils import resources

    cfg = Config()
    cfg.state_dir = work / f"state-{'on' if engine else 'off'}"
    cfg.roots = [str(root)]
    cfg.active_root = str(root)
    cfg.ai_enabled = False
    cfg.ai_engine = "off"
    cfg.orientation_ai = False
    cfg.watch = False
    cfg.min_media_bytes = 0
    cfg.workers = WORKERS
    cfg.ocr_enabled = True
    cfg.faces_enabled = faces
    cfg.job_engine = engine
    cfg.ensure_dirs()
    resources.tune(compute_threads=WORKERS)
    tuning._threads_now(WORKERS)
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)

    phases: dict[str, list[float]] = {}
    originals = {}
    for name in ("_index", "_read_text", "_find_faces", "_derive"):
        method = getattr(scanner_mod.Scanner, name)
        originals[name] = method

        def timed(self, *args, _name=name, _method=method, **kwargs):
            start = time.perf_counter()
            locks_then = counter.count
            try:
                return _method(self, *args, **kwargs)
            finally:
                phases.setdefault(_name, []).extend(
                    [time.perf_counter() - start, counter.count - locks_then])
        setattr(scanner_mod.Scanner, name, timed)

    waits: list[float] = []
    done = threading.Event()

    def family_member() -> None:
        mine = db.connect(cfg.db_path)
        rng = random.Random(5)
        while not done.wait(0.05):
            ids = [r[0] for r in mine.execute("SELECT id FROM assets LIMIT 500")]
            if not ids:
                continue
            start = time.perf_counter()
            db.update_asset(mine, rng.choice(ids), caption=f"tagged at {time.time():.2f}")
            waits.append(time.perf_counter() - start)
        db.close_all()

    counter = Locks()
    try:
        scanner = scanner_mod.Scanner(cfg)
        family = threading.Thread(target=family_member, name="family", daemon=True)
        family.start()
        wall, cpu = time.perf_counter(), processor()
        scanner._run(root, full=True)
        wall, cpu = time.perf_counter() - wall, processor() - cpu
        done.set()
        family.join()
        assert scanner.progress.status == "done", scanner.progress.snapshot()
        scanner.close()
    finally:
        counter.close()
        for name, method in originals.items():
            setattr(scanner_mod.Scanner, name, method)
        db.close_all()
    analysis = [phases[n] for n in ("_read_text", "_find_faces", "_derive") if n in phases]
    return {"wall": wall, "cores": cpu / wall if wall else 0,
            "index": phases.get("_index", [0, 0])[0],
            "analysis": sum(p[0] for p in analysis), "locks": sum(p[1] for p in analysis),
            "waits": sorted(waits)}


def main() -> int:
    from ninaivu.media import faces as faces_mod, ocr

    if not ocr.available():
        print("  reading words is not installed (requirements/requirements-ocr.txt)")
    faces = bool(faces_mod.model_status().get("ready"))
    print(f"  {os.cpu_count()} cores, {WORKERS} workers; faces {'on' if faces else 'off (no models)'}")
    keep = os.environ.get("BENCH_KEEP")
    work = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="ninaivu-bench-engine-"))
    root = work / "Library"
    try:
        if not root.exists():
            print(f"  building {PHOTOS} photographs…", flush=True)
            build_photos(root, PHOTOS)
        for engine in (False, True):
            r = scan(work, root, engine, faces)
            waits = r["waits"] or [0.0]
            print(f"\n  job engine {'on' if engine else 'off'}", flush=True)
            print(f"    whole scan       {r['wall']:8.2f} s   {r['cores']:5.2f} cores busy")
            print(f"    indexing         {r['index']:8.2f} s")
            print(f"    text and faces   {r['analysis']:8.2f} s   write lock taken {r['locks']:,} times")
            print(f"    family saves     {len(waits):,}: waited median {statistics.median(waits) * 1000:.1f} ms, "
                  f"95% under {waits[int(len(waits) * 0.95) - 1 if len(waits) > 1 else 0] * 1000:.1f} ms, "
                  f"longest {waits[-1] * 1000:.1f} ms", flush=True)
            shutil.rmtree(work / f"state-{'on' if engine else 'off'}", ignore_errors=True)
        return 0
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
