#!/usr/bin/env python3
"""Where does an archive run actually spend its time?

    python tools/bench_archive.py [files] [avg_kb]

Builds a throwaway corpus, archives it, and reports both the wall clock and a
cProfile breakdown — so a change can be judged against a measurement instead of
an intuition.
"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# A redirected stdout on Windows is cp1252, which has no arrow: the report used
# to crash on its own result line whenever it was saved to a file.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 400
AVG_KB = int(sys.argv[2]) if len(sys.argv) > 2 else 300


def build_corpus(root: Path, count: int, avg_kb: int) -> int:
    from PIL import Image
    total = 0
    for i in range(count):
        folder = root / f"DCIM/{100 + i // 50}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"IMG_{i:05d}.jpg"
        side = 8
        while True:
            Image.frombytes("RGB", (side, side),
                            os.urandom(side * side * 3)).save(path, "JPEG", quality=95)
            if path.stat().st_size >= avg_kb * 1024 or side > 4096:
                break
            side *= 2
        total += path.stat().st_size
    return total


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="ninaivu-bench-"))
    source, dest = work / "card", work / "Master"
    source.mkdir(parents=True)

    print(f"\n  building {COUNT} files of ~{AVG_KB} KB…", flush=True)
    started = time.time()
    total_bytes = build_corpus(source, COUNT, AVG_KB)
    print(f"  corpus: {total_bytes / 1e6:.0f} MB in {time.time() - started:.1f}s")

    from ninaivu import archive
    from ninaivu.archive import database as adb
    from ninaivu.archive.scanner import ArchiveJob

    archive.configure(work / "state", 0)
    adb.init_db()

    job = ArchiveJob([str(source)], str(dest))
    profiler = cProfile.Profile()
    started = time.time()
    profiler.enable()
    job.run()
    profiler.disable()
    elapsed = time.time() - started

    rate = total_bytes / elapsed / 1e6
    print(f"\n  archived in {elapsed:.2f}s  →  {rate:.1f} MB/s, "
          f"{COUNT / elapsed:.0f} files/s")

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats("cumulative")
    stats.print_stats(18)
    for line in stream.getvalue().splitlines():
        if line.strip() and not line.startswith("   Ordered"):
            print("   ", line)

    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
