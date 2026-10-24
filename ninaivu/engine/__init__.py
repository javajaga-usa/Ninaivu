"""The job engine: background work as small resumable tasks run on every
core.

Since 3.0 it is the only way a scan works (it was the ``job_engine`` switch
in 2.x, and scans without it ran as in 1.12). A scan:

* indexes new and changed files in worker processes (unless the
  orientation model is on, which lives in the server process);
* reads text and finds faces as tasks of one list (``engine.db``), side by
  side on worker processes, written to the index in groups;
* draws the pictures of sound files that have none, reads the moments of
  the videos to describe (the image model, in the server, describes them)
  and takes content fingerprints, as tasks of the same list;
* works out how the faces group into people on a worker process.

Describing photographs, naming places, imports, the storage check, the
mirror and backups do not run on it.

A run of fewer than :data:`~.pool.PROCESSES_FROM` tasks works on threads of
the server instead (:class:`~.pool.InProcessPool`): the same list, the same
stop, carry-on and report, without a second spent starting each process.
Faces, and clips without ffmpeg, are always worked on processes: they decode
originals with libraries that can take the process they are in down.

Modules: :mod:`.tasks` (the durable list), :mod:`.derivations` (what can be
worked out and how it is stored), :mod:`.work` (what a worker does),
:mod:`.pool` (the processes), :mod:`.engine` (a run).
"""
from __future__ import annotations

from typing import Any

from . import work
from .derivations import DERIVATIONS, Context, Derivation
from .engine import JobEngine, RunReport, database_path
from .pool import (PROCESSES_FROM, BrokenProcessPool, InProcessPool, WorkerPool,
                   portable_config, processes_for, server_setup)
from .tasks import TaskStore

__all__ = ["DERIVATIONS", "PROCESSES_FROM", "BrokenProcessPool", "Context", "Derivation",
           "InProcessPool", "JobEngine", "RunReport", "TaskStore", "WorkerPool",
           "database_path", "portable_config", "processes_for", "server_setup", "work",
           "worker_pool"]


def worker_pool(cfg: Any, tasks: int | None = None, size: int | None = None,
                kinds: Any = None) -> WorkerPool:
    """A pool sized for *cfg* (or *size*, and no larger than *tasks*), its
    workers set up as this server is: processes, or threads of this one for
    fewer than :data:`PROCESSES_FROM` tasks when every one of *kinds* may
    work there (:attr:`Derivation.in_process`; no *kinds*: the indexer's
    own files, which it reads on threads too)."""
    size = processes_for(cfg) if size is None else int(size)
    if tasks is not None:
        size = max(1, min(size, int(tasks)))
        if int(tasks) < PROCESSES_FROM and all(
                DERIVATIONS[k].in_process() for k in (kinds or ())):
            for kind in kinds or ():
                room = DERIVATIONS[kind].threads_room
                if room is not None:
                    size = max(1, min(size, int(room())))
            return InProcessPool(size)
    return WorkerPool(size, server_setup(cfg))
