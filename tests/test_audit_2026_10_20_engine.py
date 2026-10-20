"""The eighth audit's findings about the scan's pools (2026-10-20): a stop
that waited for a hung build (D-21) and a run's share of the worker
processes fixed at its start (D-22)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

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
