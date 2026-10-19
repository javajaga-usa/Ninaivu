"""The job engine (1.13, opt-in): background work as small resumable tasks
run on every core.

Switched on by the ``job_engine`` setting (Settings → Advanced, or
``NINAIVU_JOB_ENGINE=1``). With it off, nothing here is used and every scan
runs exactly as in 1.12. With it on, a scan:

* indexes new and changed files in worker processes instead of threads
  (unless the orientation model is on, which lives in the server process);
* reads text and finds faces as tasks of one list (``engine.db``), side by
  side on worker processes, written to the index in groups.

Modules: :mod:`.tasks` (the durable list), :mod:`.derivations` (what can be
worked out and how it is stored), :mod:`.work` (what a worker does),
:mod:`.pool` (the processes), :mod:`.engine` (a run).
"""
from __future__ import annotations

from typing import Any

from .derivations import DERIVATIONS, Context, Derivation
from .engine import JobEngine, RunReport, database_path
from .pool import BrokenProcessPool, WorkerPool, portable_config, processes_for, server_setup
from .tasks import TaskStore

__all__ = ["DERIVATIONS", "BrokenProcessPool", "Context", "Derivation", "JobEngine",
           "RunReport", "TaskStore", "WorkerPool", "database_path", "enabled",
           "portable_config", "processes_for", "server_setup", "worker_pool"]


def worker_pool(cfg: Any, tasks: int | None = None) -> WorkerPool:
    """A pool sized for *cfg* (and no larger than *tasks*), its workers set
    up as this server is."""
    size = processes_for(cfg)
    if tasks is not None:
        size = max(1, min(size, int(tasks)))
    return WorkerPool(size, server_setup(cfg))


def enabled(cfg: Any) -> bool:
    """Whether this server runs its scans on the job engine."""
    return bool(getattr(cfg, "job_engine", False))
