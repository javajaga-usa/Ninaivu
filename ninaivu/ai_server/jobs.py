"""AI server jobs that run in the background while the Playground watches.

A generative edit on a busy GPU can wait in a queue and then take a minute. A
request held open that long says nothing while it waits, so the Playground
starts a job, polls its state — queued and how many are ahead, or running and
how far along — and fetches the result when it is done.

Jobs live in memory. They belong to the profile that started them, results
are handed over once, and anything not collected is dropped after a while so
finished images do not pile up in the server's memory.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from .comfyui import AIServerError

#: Seconds a job (finished or not) is kept after its last activity.
KEEP_FOR = 15 * 60
#: Jobs this Ninaivu runs on the AI server at once, across every profile.
MAX_RUNNING = 2

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _sweep(now: float) -> None:
    for job_id in [j for j, job in _jobs.items() if now - job["touched"] > KEEP_FOR]:
        _jobs.pop(job_id, None)


def start(owner: int, kind: str, work: Callable[[Callable[[dict[str, Any]], None]], bytes]) -> str:
    """Run ``work(report)`` on a background thread; returns the job id.

    ``work`` receives a function to report progress through and returns PNG
    bytes. AIServerError if this Ninaivu is already running its limit of jobs.
    """
    now = time.time()
    with _lock:
        _sweep(now)
        active = sum(1 for job in _jobs.values() if job["state"] in ("starting", "queued", "running"))
        if active >= MAX_RUNNING:
            raise AIServerError("The AI server is already working on two edits from this Ninaivu. "
                                "Try again when one finishes.")
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"owner": owner, "kind": kind, "state": "starting", "ahead": None,
                         "value": 0, "max": 0, "error": "", "result": None,
                         "started": now, "touched": now}

    def report(update: dict[str, Any]) -> None:
        with _lock:
            job = _jobs.get(job_id)
            if job is None or job["state"] in ("done", "error"):
                return
            job["state"] = update.get("stage", job["state"])
            job["ahead"] = update.get("ahead")
            job["value"] = update.get("value", 0)
            job["max"] = update.get("max", 0)
            job["touched"] = time.time()

    def run() -> None:
        try:
            result = work(report)
            outcome = {"state": "done", "result": result}
        except (AIServerError, ValueError, OSError) as error:
            outcome = {"state": "error", "error": str(error)}
        except Exception as error:                 # noqa: BLE001 - reported, not raised
            outcome = {"state": "error", "error": f"The edit failed: {error}"}
        with _lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.update(outcome, touched=time.time())

    threading.Thread(target=run, name=f"ai-server-job-{kind}", daemon=True).start()
    return job_id


def status(job_id: str, owner: int) -> dict[str, Any] | None:
    """What the Playground shows, or None for a job that is not this profile's."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job["owner"] != owner:
            return None
        job["touched"] = time.time()
        return {"id": job_id, "kind": job["kind"], "state": job["state"], "ahead": job["ahead"],
                "value": job["value"], "max": job["max"], "error": job["error"],
                "elapsed": round(time.time() - job["started"], 1)}


def take_result(job_id: str, owner: int) -> bytes | None:
    """The finished image, once. The job is forgotten when it is collected."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job["owner"] != owner or job["state"] != "done":
            return None
        _jobs.pop(job_id, None)
        return job["result"]


def reset() -> None:
    """Forget every job — for tests."""
    with _lock:
        _jobs.clear()
