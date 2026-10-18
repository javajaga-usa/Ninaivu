#!/usr/bin/env python3
"""Where do the index and the library spend their time?

    python tools/bench_index.py [photos] [library_size]

Builds a throwaway library of *photos* real JPEGs (default 600) and scans it
the way the server does, then grows the index to *library_size* rows (default
200,000, about the size of a real family library) by copying those rows under
new names, with a hard link on disk for each so the walk sees a real file.
On that it times what runs again and again on a library that is already
indexed: a rescan that finds nothing new, a rescan of one folder, and the
gallery's commonest reads. Nothing here touches the real index.
"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PHOTOS = int(sys.argv[1]) if len(sys.argv) > 1 else 600
LIBRARY = int(sys.argv[2]) if len(sys.argv) > 2 else 200_000
PROFILE = os.environ.get("BENCH_PROFILE", "")


def build_photos(root: Path, count: int) -> None:
    from PIL import Image
    rng = random.Random(7)
    base = datetime(2015, 1, 1, 9, 0)
    for i in range(count):
        when = base + timedelta(hours=i * 37)
        folder = root / when.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        # Noise over a gradient, so every picture hashes differently and
        # compresses like a photograph rather than a flat colour.
        small = Image.frombytes("RGB", (48, 36), rng.randbytes(48 * 36 * 3))
        img = small.resize((2400, 1800), Image.BILINEAR)
        exif = img.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0110] = "BenchCam"
        exif[0x010F] = "Acme"
        img.save(folder / f"IMG_{i:05d}.jpg", "JPEG", quality=88, exif=exif)


def grow(cfg, conn, root: Path, target: int) -> int:
    """Copy the indexed rows under new names until there are *target*."""
    from ninaivu.storage import db
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM assets WHERE root=?", (str(root),))]
    have = len(rows)
    if have >= target:
        return have
    cols = [c for c in rows[0] if c != "id"]
    rng = random.Random(11)
    batch = []
    made = have
    copy = 0
    while made < target:
        copy += 1
        for row in rows:
            if made >= target:
                break
            rel = row["rel_path"]
            new_rel = f"{rel.rsplit('/', 1)[0]}/c{copy:03d}_{rel.rsplit('/', 1)[1]}"
            dest = root / new_rel
            if not dest.exists():
                os.link(root / rel, dest)
            clone = dict(row)
            clone["rel_path"] = new_rel
            clone["phash"] = f"{rng.getrandbits(64):016x}"
            clone["dup_group"] = None
            clone["thumb"] = row["thumb"]
            batch.append(tuple(clone[c] for c in cols))
            made += 1
            if len(batch) >= 5000:
                _insert(db, conn, cols, batch)
                batch.clear()
    _insert(db, conn, cols, batch)
    db.refresh_statistics(conn, force=True)
    return made


def _insert(db, conn, cols, batch) -> None:
    if not batch:
        return
    with db._write_lock:
        conn.executemany(
            f"INSERT INTO assets ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)})", batch)
        conn.commit()


def timed(label: str, fn, *, repeat: int = 1):
    best = None
    result = None
    for _ in range(repeat):
        started = time.perf_counter()
        if PROFILE and PROFILE in label:
            prof = cProfile.Profile()
            result = prof.runcall(fn)
            out = io.StringIO()
            pstats.Stats(prof, stream=out).sort_stats(os.environ.get("BENCH_SORT", "cumulative")).print_stats(45)
            print(out.getvalue())
        elif os.environ.get("BENCH_SQL") and os.environ["BENCH_SQL"] in label:
            result = _sql_breakdown(fn)
        else:
            result = fn()
        took = time.perf_counter() - started
        best = took if best is None else min(best, took)
    print(f"  {label:<44} {best * 1000:9.1f} ms", flush=True)
    return result


def _sql_breakdown(fn):
    """Run *fn* and print the statements that took longest (the gap to the
    next statement, so Python between them is counted against the one before)."""
    from ninaivu.storage import db
    seen: list[tuple[float, str]] = []
    conn = db.connect(_DB[0])
    conn.set_trace_callback(lambda sql: seen.append((time.perf_counter(), sql)))
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)
    seen.append((time.perf_counter(), "<end>"))
    spans = sorted(((seen[i + 1][0] - seen[i][0], seen[i][1])
                    for i in range(len(seen) - 1)), reverse=True)
    for took, sql in spans[:15]:
        print(f"      {took * 1000:8.1f} ms  {' '.join(sql.split())[:150]}")
    return result


_DB: list = []


def main() -> int:
    import resource
    keep = os.environ.get("BENCH_KEEP")
    work = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="ninaivu-bench-index-"))
    work.mkdir(parents=True, exist_ok=True)
    root = work / "Library"
    root.mkdir()
    try:
        from ninaivu.server.config import Config
        from ninaivu.storage import db
        from ninaivu.media.scanner import Scanner

        cfg = Config()
        cfg.state_dir = work / "state"
        cfg.roots = [str(root)]
        cfg.active_root = str(root)
        cfg.ai_enabled = False
        cfg.ai_engine = "off"
        cfg.ai_models_dir = str(work / "models")
        cfg.watch = False
        cfg.min_media_bytes = 0
        cfg.workers = max(1, os.cpu_count() or 1)
        cfg.ensure_dirs()

        print(f"\n  building {PHOTOS} photographs…", flush=True)
        build_photos(root, PHOTOS)
        from ninaivu.server import auth
        conn = db.init_db(cfg.db_path)
        _DB.append(cfg.db_path)
        auth.init_auth_schema(conn)
        scanner = Scanner(cfg)
        def first_scan():
            scanner._run(root, full=True)
            assert scanner.progress.status == "done", scanner.progress.snapshot()
        timed(f"first scan of {PHOTOS} photographs",
              first_scan)
        timed(f"rescan, nothing new ({PHOTOS})",
              lambda: scanner._run(root, full=False), repeat=3)

        print(f"\n  growing the index to {LIBRARY:,} rows…", flush=True)
        made = grow(cfg, conn, root, LIBRARY)
        print(f"  {made:,} rows")
        # The first rescan after growing links the new rows; the ones after
        # are what a watcher-driven rescan costs every time.
        timed(f"rescan after growing ({made:,})",
              lambda: scanner._run(root, full=False))
        timed(f"rescan, nothing new ({made:,})",
              lambda: scanner._run(root, full=False), repeat=3)
        folder = conn.execute(
            "SELECT rel_path FROM assets WHERE root=? LIMIT 1 OFFSET 500",
            (str(root),)).fetchone()[0].rsplit("/", 1)[0]
        timed("rescan of one folder, nothing new",
              lambda: scanner._run(root, full=False, only=[folder]), repeat=3)

        roots = [str(root)]
        from ninaivu.api.api import _SEGMENT_COLUMNS
        timed("gallery layout, first 25,000",
              lambda: db.query_assets(conn, roots, limit=25_000, columns=_SEGMENT_COLUMNS,
                                      remember_total=True), repeat=5)
        timed("gallery layout, 25,000 from 150,000",
              lambda: db.query_assets(conn, roots, limit=25_000, offset=150_000,
                                      columns=_SEGMENT_COLUMNS, remember_total=True),
              repeat=5)
        timed("viewer page of 200 rows",
              lambda: db.query_assets(conn, roots, limit=200, offset=60_000,
                                      with_total=False), repeat=5)
        timed("timeline", lambda: db.timeline(conn, roots), repeat=5)
        timed("library stats", lambda: db.library_stats(conn, roots), repeat=5)
        timed("facets", lambda: db.facets(conn, roots), repeat=5)
        timed("duplicate groups", lambda: db.duplicate_groups(conn, roots), repeat=3)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            peak *= 1024
        print(f"\n  peak memory {peak / 1e6:.0f} MB")
        scanner.close()
        return 0
    finally:
        db.close_all()
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
