"""Worker processes leave when the server that started them has gone
(audit 2026-10-19, C-03 and C-20).

A worker waits on its queue for ever. After a crash, a force quit or launchd
killing the server, each one stayed alive with no parent, about 350 MB a time
on a twenty-core Mac, and more piled up at every restart.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from ninaivu.engine import pool


#: A stand-in server: starts one worker that runs the real watch, says the
#: worker's id, then dies without shutting anything down.
_SERVER = """
import multiprocessing, os, sys, time
from ninaivu.engine import pool

def worker():
    pool.watch_parent(every=0.2)
    time.sleep(60)

if __name__ == "__main__":
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=worker)
    child.start()
    print(child.pid, flush=True)
    time.sleep(1)
    os._exit(0)
"""


def _alive(pid: int) -> bool:
    try:
        import psutil
    except ImportError:
        pytest.skip("psutil is needed to look for the worker")
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.timeout(90)
def test_a_worker_leaves_when_its_server_is_killed(tmp_path):
    import subprocess
    from pathlib import Path
    script = tmp_path / "server.py"
    script.write_text(_SERVER, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          timeout=60, env=env, check=False)
    worker_pid = int(done.stdout.split()[0])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _alive(worker_pid):
        time.sleep(0.2)
    assert not _alive(worker_pid), "the worker outlived the process that started it"


def test_the_engine_and_film_workers_both_watch():
    import inspect
    from ninaivu.media import films
    assert "watch_parent()" in inspect.getsource(pool._worker_start)
    assert "watch_parent()" in inspect.getsource(films._encode_part)


@pytest.mark.skipif(sys.platform == "win32", reason="a pool's own shutdown is enough there")
def test_a_living_parent_keeps_its_workers():
    with pool.WorkerPool(1) as workers:
        assert workers.submit(os.getpid).result(timeout=60) != os.getpid()
        time.sleep(pool.PARENT_CHECK_SECONDS * 2)
        assert workers.submit(os.getpid).result(timeout=60)
