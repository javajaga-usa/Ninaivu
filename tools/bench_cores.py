#!/usr/bin/env python3
"""How much of the computer does a first scan keep busy, pass by pass?

    python tools/bench_cores.py [photos] [workers ...]

Builds a throwaway library of *photos* 12-megapixel JPEGs (default 240) with
words written on them, then scans it from nothing the way the server does,
with reading the words on, and finding faces on when the face models are in
NINAIVU_AI_MODELS_DIR. Once for each worker count given (default: every
core), each time into a fresh index.

BENCH_GPU_SECONDS=0.15 adds an image model that takes that long a photograph
on a graphics processor and none of the processor: a stand-in, since the
cloud machines this runs on have no graphics processor, for the hours a real
one spends describing a library while the processor waits. For every pass it prints the time it
took and how many cores it kept busy on average (processor time over wall
time): a pass that keeps fewer cores busy than it was given is where a large
machine waits. Nothing here touches the real index.
"""
from __future__ import annotations

import functools
import os
import random
import resource
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PHOTOS = int(sys.argv[1]) if len(sys.argv) > 1 else 240
WORKERS = [int(w) for w in sys.argv[2:]] or [os.cpu_count() or 1]

#: The passes timed, in the order a scan runs them.
PASSES = ("_tag", "_index", "_link_live_photos", "_link_raw_pairs", "_link_duplicates",
          "_group_occasions", "_judge_screens", "_analyse",
          "_read_text", "_find_faces")


def build_photos(root: Path, count: int) -> None:
    from PIL import Image, ImageDraw, ImageFont
    rng = random.Random(7)
    base = datetime(2015, 1, 1, 9, 0)
    try:
        font = ImageFont.load_default(size=160)
    except TypeError:
        font = ImageFont.load_default()
    words = ["BIRTHDAY", "CHENNAI", "WELCOME", "BEACH ROAD", "PONGAL", "SCHOOL DAY"]
    for i in range(count):
        when = base + timedelta(hours=i * 37)
        folder = root / when.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        small = Image.frombytes("RGB", (64, 48), rng.randbytes(64 * 48 * 3))
        img = small.resize((4000, 3000), Image.BILINEAR)
        ImageDraw.Draw(img).text((400, 1200), rng.choice(words), fill="white", font=font)
        exif = img.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0110] = "BenchCam"
        img.save(folder / f"IMG_{i:05d}.jpg", "JPEG", quality=88, exif=exif)


def processor() -> float:
    use = resource.getrusage(resource.RUSAGE_SELF)
    return use.ru_utime + use.ru_stime


class GraphicsModel:
    """An image model on a graphics processor, as the scan sees one: it reads
    ahead, and each photograph takes *seconds* of waiting, not of processor."""

    name = "stand-in model"
    device = "gpu"
    model_id = "stand-in"
    semantic = False
    reads_ahead = True

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def prepare(self, paths):
        return None

    def analyse(self, paths, prepared=None):
        time.sleep(self.seconds * len(paths))
        return [{"tags": ["bench"]} for _ in paths]


def scan(work: Path, root: Path, workers: int, faces: bool) -> list[tuple[str, float, float]]:
    from ninaivu.media import scanner as scanner_mod
    from ninaivu.server import auth, tuning
    from ninaivu.server.config import Config
    from ninaivu.storage import db
    from ninaivu.utils import resources

    state = work / f"state-{workers}"
    cfg = Config()
    cfg.state_dir = state
    cfg.roots = [str(root)]
    cfg.active_root = str(root)
    gpu = float(os.environ.get("BENCH_GPU_SECONDS") or 0)
    cfg.ai_enabled = bool(gpu)
    cfg.ai_engine = "off"
    cfg.orientation_ai = False
    cfg.nsfw_filter = False
    cfg.watch = False
    cfg.min_media_bytes = 0
    cfg.workers = workers
    cfg.ocr_enabled = True
    cfg.faces_enabled = faces
    cfg.ensure_dirs()
    resources.tune(compute_threads=workers)
    tuning._threads_now(workers)
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)

    timings: list[tuple[str, float, float]] = []
    originals = {}
    for name in PASSES:
        method = getattr(scanner_mod.Scanner, name, None)
        if method is None:
            continue
        originals[name] = method

        @functools.wraps(method)
        def timed(self, *args, _name=name, _method=method, **kwargs):
            wall, cpu = time.perf_counter(), processor()
            try:
                return _method(self, *args, **kwargs)
            finally:
                timings.append((_name, time.perf_counter() - wall, processor() - cpu))
        setattr(scanner_mod.Scanner, name, timed)
    try:
        scanner = scanner_mod.Scanner(cfg, GraphicsModel(gpu) if gpu else None)
        wall, cpu = time.perf_counter(), processor()
        scanner._run(root, full=True)
        timings.append(("whole scan", time.perf_counter() - wall, processor() - cpu))
        assert scanner.progress.status == "done", scanner.progress.snapshot()
        scanner.close()
    finally:
        for name, method in originals.items():
            setattr(scanner_mod.Scanner, name, method)
        db.close_all()
    return timings


def main() -> int:
    from ninaivu.media import faces as faces_mod, ocr

    if not ocr.available():
        print("  reading words is not installed (requirements/requirements-ocr.txt)")
    faces = bool(faces_mod.model_status().get("ready"))
    print(f"  {os.cpu_count()} cores; faces {'on' if faces else 'off (no models)'}")
    keep = os.environ.get("BENCH_KEEP")
    work = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="ninaivu-bench-cores-"))
    root = work / "Library"
    try:
        if not root.exists():
            print(f"  building {PHOTOS} photographs…", flush=True)
            build_photos(root, PHOTOS)
        for workers in WORKERS:
            print(f"\n  {PHOTOS} photographs, {workers} workers", flush=True)
            for name, wall, cpu in scan(work, root, workers, faces):
                if wall < 0.05 and name != "whole scan":
                    continue
                print(f"    {name:<20} {wall:8.2f} s   {cpu / wall if wall else 0:5.2f} cores busy"
                      f"   {PHOTOS / wall if wall else 0:7.1f} photos/s", flush=True)
            shutil.rmtree(work / f"state-{workers}", ignore_errors=True)
        return 0
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
