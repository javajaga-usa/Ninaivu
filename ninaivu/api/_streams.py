"""How many status streams the console may hold open at once, all told.

A server-sent stream keeps one of the web server's threads for as long as the
page that opened it stays open. Every console tab holds the progress stream
(/api/events) and the Archive page a second one (/api/archive/stream); each
had a limit of its own, or none, and neither knew about the thread pool. On a
Raspberry Pi, with six threads, three console tabs on the Archive page took
them all, and the console stopped answering anything else — Stop and Pause
included. Together they now leave at least half of the threads for ordinary
requests; a stream refused here is answered 429, and the page polls instead.

"At least half" holds at every thread count the Tuning page allows. The limit
was once ``max(2, threads // 2)``, which at the two-thread minimum let two
streams take both threads and left none for Stop (external audit 2026-10-11,
PERF-01). One stream is always allowed, so a console still gets live progress
on the smallest setting; the rest poll.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_open = 0

def limit(cfg) -> int:  # noqa: ANN001
    """Streams allowed at once: half the web threads, rounded down, and at
    least one, so as many threads as streams are always left for everything
    else."""
    threads = int(getattr(cfg, "server_threads", 0) or 8)
    return max(1, threads // 2)


def take(cfg) -> bool:  # noqa: ANN001
    """Claim a stream; False when the console already holds its share."""
    global _open
    with _lock:
        if _open >= limit(cfg):
            return False
        _open += 1
        return True


def give() -> None:
    global _open
    with _lock:
        _open = max(0, _open - 1)


def held() -> int:
    with _lock:
        return _open
