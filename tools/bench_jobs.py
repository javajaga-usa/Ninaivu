#!/usr/bin/env python3
"""How long do a scan, an import and a storage check take started together?

    python tools/bench_jobs.py [photos in the library] [new photos] [photos to import]

Builds a throwaway library (default 200 photographs, already indexed), adds
new photographs to it (default 200) and an export folder to import (default
200), then starts a scan of the library and, the moment the scan has finished
walking it, an import and a storage check: the three jobs a household starts
together after copying a phone's photographs over. It prints when each job
finished and when everything was done.

The library's disk is taken to be solid-state, as the Mac mini's is.
BENCH_FILE_SECONDS=0.02 makes the import and the storage check take that much
longer a file, a stand-in for a library of hundreds of thousands of files on
an external disk, where each takes hours rather than the second or two it
takes here. BENCH_GPU_SECONDS=0.2 adds an image model that takes that long a photograph
on a graphics processor (see tools/bench_cores.py), so the scan has an
analysis to carry on with. NINAIVU_SRC runs another checkout of Ninaivu (an
older one, for "before") with this same script. Nothing here touches the real
index.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_cores import GraphicsModel                  # noqa: E402

# After bench_cores, which puts this checkout first on the path.
sys.path.insert(0, os.environ.get("NINAIVU_SRC") or str(Path(__file__).resolve().parents[1]))

LIBRARY = int(sys.argv[1]) if len(sys.argv) > 1 else 200
NEW = int(sys.argv[2]) if len(sys.argv) > 2 else 200
IMPORT = int(sys.argv[3]) if len(sys.argv) > 3 else 200


def build(folder: Path, count: int, start: int) -> None:
    from datetime import datetime, timedelta
    import random
    from PIL import Image
    rng = random.Random(start)
    base = datetime(2015, 1, 1, 9, 0)
    for i in range(start, start + count):
        when = base + timedelta(hours=i * 37)
        here = folder / when.strftime("%Y/%m/%d")
        here.mkdir(parents=True, exist_ok=True)
        small = Image.frombytes("RGB", (64, 48), rng.randbytes(64 * 48 * 3))
        img = small.resize((3000, 2250), Image.BILINEAR)
        exif = img.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        img.save(here / f"IMG_{i:05d}.jpg", "JPEG", quality=88, exif=exif)


def main() -> int:
    from ninaivu.api import admin_api
    from ninaivu.media import scanner as scanner_mod
    from ninaivu.server import auth, capacity
    from ninaivu.server.config import Config
    from ninaivu.storage import db
    from ninaivu.storage.importer import Importer

    capacity.storage = lambda *a, **k: {"solid_state": True}
    slow = float(os.environ.get("BENCH_FILE_SECONDS") or 0)
    if slow:
        hash_file, copy_one = admin_api._hash_file, Importer._one

        def hashed(*args, **kwargs):
            time.sleep(slow)
            return hash_file(*args, **kwargs)

        def copied(self, *args, **kwargs):
            time.sleep(slow)
            return copy_one(self, *args, **kwargs)
        admin_api._hash_file, Importer._one = hashed, copied
    work = Path(tempfile.mkdtemp(prefix="ninaivu-bench-jobs-"))
    root, export = work / "Library", work / "Export"
    try:
        print(f"  {os.cpu_count()} cores; building {LIBRARY} + {NEW} photographs "
              f"and {IMPORT} to import…", flush=True)
        build(root, LIBRARY, 0)
        build(export, IMPORT, 50_000)

        cfg = Config()
        cfg.state_dir = work / "state"
        cfg.roots = [str(root)]
        cfg.active_root = str(root)
        gpu = float(os.environ.get("BENCH_GPU_SECONDS") or 0)
        cfg.ai_enabled = bool(gpu)
        cfg.ai_engine = "off"
        cfg.orientation_ai = False
        cfg.nsfw_filter = False
        cfg.watch = False
        cfg.min_media_bytes = 0
        cfg.workers = os.cpu_count() or 1
        cfg.ocr_enabled = False
        cfg.faces_enabled = False
        cfg.ensure_dirs()
        auth.init_auth_schema(db.init_db(cfg.db_path))
        scanner = scanner_mod.Scanner(cfg, GraphicsModel(gpu) if gpu else None)
        scanner.watch = lambda roots=(): None
        scanner.start()
        scanner._thread.join()
        build(root, NEW, 10_000)

        importer = Importer(cfg, lambda: db.connect(cfg.db_path), scanner=scanner)
        finished: dict[str, float] = {}
        began = time.perf_counter()
        scanner.start()
        while scanner.progress.snapshot().get("status") != "indexing":
            time.sleep(0.01)
        importer.start(str(export), str(root), None)
        admin_api.start_scrubber_job(cfg.db_path, scanner=scanner, on_done=lambda: None)
        quiet_since = None
        while True:
            now = time.perf_counter() - began
            if not importer.running:
                finished.setdefault("import", now)
            if not admin_api.scrubber_running():
                finished.setdefault("storage check", now)
            busy = (scanner.running or scanner.waiting or importer.running
                    or admin_api.scrubber_running())
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since > 1.0:
                finished["everything"] = quiet_since
                break
            time.sleep(0.02)
        conn = db.connect(cfg.db_path)
        indexed = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        tagged = conn.execute("SELECT COUNT(*) FROM assets WHERE ai_version > 0").fetchone()[0]
        for name, at in finished.items():
            print(f"    {name:<14} done after {at:7.2f} s", flush=True)
        print(f"    {indexed} photographs indexed, {tagged} described", flush=True)
        scanner.close()
        db.close_all()
        assert threading.active_count() < 50
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
