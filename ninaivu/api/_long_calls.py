"""How many web threads the long AI answers may hold at once, together.

A life story (up to 300 s), a letter read aloud (420 s), a question about a
photograph (150 s) and the words for a year (120 s) each wait on the model on
a web thread. Each is limited to one or two at a time on its own, but four
features each taking their share could hold most of the server's threads
(``server_threads``, 8 by default), and then the gallery itself waits.

So each of them also takes a place here, without waiting for one: at most a
quarter of the web threads (and at least two) are ever answering a long AI
question, and the rest stay free for browsing. A request that finds no
place is told "try again in a moment", as when its own feature is busy.
"""

from __future__ import annotations

import threading

from flask import current_app, has_app_context

_lock = threading.Lock()
_held = 0


def most() -> int:
    """How many long AI answers may be waited on at once."""
    threads = 8
    if has_app_context():
        cfg = current_app.config.get("MV_CONFIG")
        threads = int(getattr(cfg, "server_threads", 0) or 8)
    return max(2, threads // 4)


def take() -> bool:
    """A place, if one is free now; never waits."""
    global _held
    with _lock:
        if _held >= most():
            return False
        _held += 1
        return True


def give() -> None:
    global _held
    with _lock:
        _held = max(0, _held - 1)


class Limited:
    """A feature's own limit, and a place among the long AI answers.

    Used as the feature used its semaphore: ``acquire(timeout=...)`` and
    ``release()``.
    """

    def __init__(self, own: int):
        self._own = threading.BoundedSemaphore(own)

    def acquire(self, timeout: float | None = None) -> bool:
        if not self._own.acquire(timeout=timeout):
            return False
        if not take():
            self._own.release()
            return False
        return True

    def release(self) -> None:
        give()
        self._own.release()
