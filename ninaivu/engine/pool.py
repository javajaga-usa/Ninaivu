"""Worker processes for the job engine.

Threads share one interpreter lock, so the parts of a photograph's work
written in Python (reading its metadata, working out its date, building its
record) used one core however many the computer has; only the parts inside
C libraries ran side by side. Worker processes each have their own, so a
twenty-core Mac keeps twenty cores busy.

Processes are started with ``spawn`` on every computer, the way macOS and
Windows start them anyway: a ``fork`` of a server with dozens of threads can
copy a lock held by one of them and hang the child. Each worker sets OpenCV
to one thread (the pool is the parallelism) and sets up what the server set
up for its whole process at start (:func:`server_setup`).
On Windows, a server with no console of its own starts its workers with
``pythonw`` so no console window opens for each of them (see nowindow.py).
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["BrokenProcessPool", "WorkerPool", "processes_for", "portable_config",
           "PROCESS_BYTES"]

#: What one worker process holds: Python, Pillow, NumPy, OpenCV and a
#: photograph decoded at full size, measured at 150 to 250 MB on 48 MP files.
PROCESS_BYTES = 350 * 1024 * 1024

#: Library thread counts set to one in every worker, before anything imports
#: the libraries that read them.
_ONE_THREAD = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
               "OPENCV_FOR_THREADS_NUM", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")


def processes_for(cfg) -> int:  # noqa: ANN001
    """How many worker processes: the scan workers the Tuning page chose,
    held to what half the free memory takes at :data:`PROCESS_BYTES` each."""
    try:
        wanted = max(1, int(getattr(cfg, "workers", 0) or 0))
    except (TypeError, ValueError):
        wanted = 1
    try:
        import psutil                                     # noqa: PLC0415
        free = int(psutil.virtual_memory().available)
    except Exception:                                     # noqa: BLE001
        return wanted
    return max(1, min(wanted, free // 2 // PROCESS_BYTES))


def portable_config(cfg):  # noqa: ANN001, ANN201
    """A copy of *cfg* with its settings only, to send to a worker.

    The server's own ``Config`` gathers private state as it runs (what the
    tuning filled in, what the console is doing); none of it is a setting a
    worker reads, and some of it need not survive being sent at all.
    """
    from dataclasses import fields                        # noqa: PLC0415
    clone = object.__new__(type(cfg))
    for item in fields(cfg):
        object.__setattr__(clone, item.name, getattr(cfg, item.name))
    return clone


def server_setup(cfg) -> dict:  # noqa: ANN001
    """What the server set up for the whole process at start (Services), for
    a worker to set up the same way: where the AI models are, what counts as
    a photograph, how much the index may cache."""
    from ..media import model_catalog                     # noqa: PLC0415
    from ..storage import db                              # noqa: PLC0415
    return {"models": str(model_catalog.models_root()),
            "state_dir": str(getattr(cfg, "state_dir", "") or ""),
            "min_media_bytes": getattr(cfg, "min_media_bytes", None),
            "cache_mb": int(db.CACHE_MB)}


def _worker_start(level: int, setup: dict | None = None) -> None:
    """Run first in every worker process."""
    for name in _ONE_THREAD:
        os.environ[name] = "1"
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s [worker] %(message)s")
    try:
        import cv2                                        # noqa: PLC0415
        cv2.setNumThreads(1)
    except Exception:                                     # noqa: BLE001
        pass
    if not setup:
        return
    from ..media import model_catalog                     # noqa: PLC0415
    from ..storage import db                              # noqa: PLC0415
    model_catalog.configure(setup.get("models") or None)
    db.set_cache_mb(int(setup.get("cache_mb") or db.CACHE_MB))
    if setup.get("state_dir"):
        from .. import archive                            # noqa: PLC0415
        archive.configure(setup["state_dir"], setup.get("min_media_bytes"))


def _quiet_executable() -> str | None:
    """``pythonw.exe`` beside this Python, on Windows without a console."""
    if sys.platform != "win32":
        return None
    from .. import nowindow                               # noqa: PLC0415
    if nowindow.has_console():
        return None
    here = Path(sys.executable)
    quiet = here.with_name("pythonw.exe")
    return str(quiet) if quiet.is_file() and here.name.lower() != "pythonw.exe" else None


class WorkerPool:
    """A pool of worker processes that can be started again after one dies.

    ``concurrent.futures`` marks a whole pool broken when any worker dies (a
    damaged file that crashes a decoder, the computer out of memory): every
    task in flight fails with :class:`BrokenProcessPool` and nothing more can
    be submitted. :meth:`restart` makes a fresh pool; the engine decides what
    happens to the tasks that were in flight.
    """

    def __init__(self, size: int, setup: dict | None = None) -> None:
        self.size = max(1, int(size))
        self.setup = setup
        self._pool: ProcessPoolExecutor | None = None
        self.restarts = 0

    def _make(self) -> ProcessPoolExecutor:
        context = multiprocessing.get_context("spawn")
        quiet = _quiet_executable()
        if quiet:
            context.set_executable(quiet)
        return ProcessPoolExecutor(
            max_workers=self.size, mp_context=context,
            initializer=_worker_start,
            initargs=(logging.getLogger().getEffectiveLevel(), self.setup))

    @property
    def executor(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = self._make()
        return self._pool

    def submit(self, fn, /, *args, **kwargs):  # noqa: ANN001, ANN201
        """Send one task. A pool found broken here (a worker died since the
        last answer was read) is started again first: the tasks that were in
        flight on it fail on their own, and the caller sorts those out."""
        try:
            return self.executor.submit(fn, *args, **kwargs)
        except BrokenProcessPool:
            self.restart()
            return self.executor.submit(fn, *args, **kwargs)

    def restart(self) -> None:
        """Drop a broken pool; the next submit starts a fresh one."""
        self.restarts += 1
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def terminate(self) -> None:
        """End the workers now, mid-task: for one long task (grouping the
        faces of a whole library) that a stop must not wait for. What they
        were doing is lost; the caller does it again another time."""
        pool, self._pool = self._pool, None
        if pool is None:
            return
        ending = getattr(pool, "terminate_workers", None)     # Python 3.14
        if ending is not None:
            ending()
            return
        for process in list((getattr(pool, "_processes", None) or {}).values()):
            try:
                process.terminate()
            except Exception:                                 # noqa: BLE001
                pass
        pool.shutdown(wait=False, cancel_futures=True)

    def shutdown(self, wait: bool = True) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=True)

    def __enter__(self) -> "WorkerPool":
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
