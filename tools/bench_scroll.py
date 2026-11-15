#!/usr/bin/env python3
"""Does a fast scroll through a big library ever show an empty tile?

    python tools/bench_scroll.py                    # 200,000 made-up items
    python tools/bench_scroll.py --keep /tmp/scroll # ...and keep the library
    python tools/bench_scroll.py --state /tmp/scroll/state   # reuse it
    python tools/bench_scroll.py -- --profiles phone --runs 1   # bench options

Builds a library of made-up rows (tools/bench_app.py, without the faces and
descriptions), serves its family app with the same waitress set-up Ninaivu
uses, and runs tools/bench_scroll.mjs against it: a desktop on the home
network and a phone over a slow link, scrolled at a reading pace, a flick and
a fling, counting on every frame what each tile on screen was showing. Nothing
is scanned and no background work is started, so the made-up rows (which have
no files of their own) are left as they are.

Everything after ``--`` goes to the browser script.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import bench_app  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    argv = sys.argv[1:]
    passed: list[str] = []
    if "--" in argv:
        at = argv.index("--")
        argv, passed = argv[:at], argv[at + 1:]
    parser = argparse.ArgumentParser()
    parser.add_argument("size", nargs="?", type=int, default=200_000)
    parser.add_argument("--keep", type=Path, help="build here and leave it behind")
    parser.add_argument("--state", type=Path, help="a state folder made by an earlier --keep")
    args = parser.parse_args(argv)

    work = args.keep or Path(tempfile.mkdtemp(prefix="ninaivu-bench-scroll-"))
    try:
        state = args.state or bench_app.made_up_index(work, args.size, extras=False)
        services, home, _admin = bench_app.build_apps(state)
        from ninaivu.__main__ import waitress_server

        port = free_port()
        server = waitress_server(home, "127.0.0.1", port, services.cfg)
        threading.Thread(target=server.run, daemon=True).start()
        env = dict(os.environ,
                   NINAIVU_HOME=f"http://127.0.0.1:{port}",
                   NINAIVU_USER=bench_app.ADMIN[0], NINAIVU_PASS=bench_app.ADMIN[1])
        done = subprocess.run(["node", str(HERE / "bench_scroll.mjs"), *passed],
                              env=env, check=False)
        server.close()
        return done.returncode
    finally:
        if not args.keep and not args.state:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
