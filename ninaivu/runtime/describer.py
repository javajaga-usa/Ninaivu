"""Describing every photograph in the background.

Every picture or video without a description, or with one from an older
model, is handed to the model host in small batches to be described from the
gallery's preview (runtime/florence.py), and what comes back is kept with it
(storage/descriptions.py), where the gallery's search reads it and the item's
route hands it to the viewer.

When it runs: shortly after start-up, after a scan has finished adding
photographs (:meth:`Describer.wake`, from Services), after the description
model is downloaded, and once every :data:`EVERY` seconds in case anything was
missed. Only with the on-device AI engine switched on (``ai_runtime``), the
description model downloaded and the image engine installed; otherwise it
does nothing and starts nothing.

It shares the model host with the voice stories, in batches small enough
that a story told meanwhile waits for one batch, not for the library. It is
analysis, so it gives way to the household as every analysis pass does
(server/workload.py), between batches.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..storage import descriptions
from . import florence
from .host import HostError, HostStopped, ModelHost, Unavailable

log = logging.getLogger(__name__)

#: Seconds between looks for pictures nobody woke this for.
EVERY = 1800.0
#: Pictures handed to the host per call.
BATCH = 8
#: Pictures read from the index at a time.
PAGE = 200
#: How long one picture may take, at most, before the host is taken to be
#: hung. A picture is a second or two on a processor; this is far past it.
PER_PICTURE = 120.0


def ready() -> str | None:
    """Why pictures cannot be described on this computer, or None."""
    return florence.ready()


class Describer:
    """Keeps every photograph's description up to date while the engine is on."""

    def __init__(self, cfg: Any, connect_db: Callable[[], sqlite3.Connection], *,
                 host: Callable[[], ModelHost],
                 hold: Callable[[], str | None] | None = None,
                 every: float = EVERY) -> None:
        self.cfg = cfg
        self._connect = connect_db
        self._host = host
        self._hold = hold
        self._every = every
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False, "done": 0, "failed": 0,
                                       "owed": 0, "earlier": 0, "message": "", "seconds": 0.0,
                                       "finished_at": 0.0}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "ai_runtime", False)) and \
            bool(getattr(self.cfg, "describe_photos", True))

    @property
    def running(self) -> bool:
        return bool(self._state["running"])

    def keep(self) -> None:
        """Start the background thread (once); it does nothing while off."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-descriptions",
                                        daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Look for pictures to describe now, not at the next half hour."""
        self._wake.set()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        self._wake.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(10)

    def _loop(self) -> None:
        # A first look a while after start-up, out of the way of the scan.
        self._wake.wait(120)
        while not self._stop.is_set():
            self._wake.clear()
            if self.enabled:
                try:
                    self.run()
                except Exception:                             # noqa: BLE001 - next time
                    log.exception("could not describe the photographs")
            self._wake.wait(self._every)

    def _say(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def _wait_for_the_household(self) -> bool:
        """False when stopped while waiting."""
        waited = False
        while self._hold is not None and not self._stop.is_set():
            reason = self._hold()
            if not reason:
                break
            waited = True
            self._say(message=f"Waiting: {reason}")
            self._stop.wait(5)
        if waited:
            self._say(message="")
        return not self._stop.is_set()

    def _preview(self, thumb: str) -> Path:
        ext = "webp" if str(getattr(self.cfg, "thumb_format", "WEBP")).upper() == "WEBP" else "jpg"
        return Path(self.cfg.thumbs_dir) / f"{thumb}_{max(self.cfg.thumb_sizes)}.{ext}"

    def run(self) -> dict[str, Any]:
        """One pass over every picture owed. Returns the state at its end."""
        why_not = ready()
        if why_not:
            self._say(message=why_not, owed=0)
            return self.status()
        version = florence.version()
        conn = self._connect()
        owed = descriptions.count_owed(conn, version)
        # Described before this pass (before a restart, too): counted as
        # done, so the console carries on from there rather than from 0.
        earlier = max(0, descriptions.count_describable(conn) - owed)
        self._say(running=True, done=0, failed=0, message="", seconds=0.0,
                  owed=owed, earlier=earlier)
        try:
            self._pass(conn, version)
        finally:
            self._say(running=False, finished_at=time.time(),
                      owed=descriptions.count_owed(conn, version))
        return self.status()

    def _pass(self, conn: sqlite3.Connection, version: int) -> None:
        """Every picture owed, a page at a time, newest first. A picture that
        fails is tried again on the next pass, not this one."""
        before: tuple[str, int] | None = None
        while not self._stop.is_set() and self.enabled:
            page = descriptions.owed(conn, version, limit=PAGE, before=before)
            if not page:
                return
            before = (str(page[-1]["date_key"]), int(page[-1]["id"]))
            for at in range(0, len(page), BATCH):
                if not self._wait_for_the_household() or not self.enabled:
                    return
                if not self._batch(conn, page[at:at + BATCH], version):
                    return

    def _batch(self, conn: sqlite3.Connection, rows: list[Any], version: int) -> bool:
        """Describe one batch. False when the pass should end."""
        found = []
        for row in rows:
            path = self._preview(str(row["thumb"]))
            if path.is_file():
                found.append((int(row["id"]), path))
            else:
                descriptions.fail(conn, int(row["id"]), version, "the preview is not there")
                self._say(failed=self._state["failed"] + 1)
        if not found:
            return True
        started = time.monotonic()
        try:
            results = self._host().call(
                "describe", stop=self._stop, timeout=PER_PICTURE * len(found),
                paths=[str(p) for _, p in found])
        except HostStopped:
            return False
        except Unavailable as exc:
            # Not these pictures' fault: the model went, or memory ran short
            # before it loaded. Said on the console; the pictures stay owed.
            self._say(message=str(exc))
            return False
        except HostError as exc:
            # The host died or hung on this batch: one of them may be the
            # cause, and nobody can tell which, so each is counted against.
            log.warning("a batch of %d photographs could not be described: %s", len(found), exc)
            for asset_id, _ in found:
                descriptions.fail(conn, asset_id, version, str(exc))
            self._say(failed=self._state["failed"] + len(found))
            return True
        took = time.monotonic() - started
        done = failed = 0
        stale = False
        for (asset_id, _), result in zip(found, results):
            text = str(result.get("text") or "").strip()
            if text:
                # Stamped by the model that wrote it: one loaded before a
                # better model came down leaves its photographs owed.
                made_by = str(result.get("model") or florence.MODEL)
                stale = stale or made_by != florence.chosen_model()
                done += descriptions.store(conn, asset_id, text, model=made_by,
                                           version=min(version, florence.version_of(made_by)))
            else:
                descriptions.fail(conn, asset_id, version,
                                  str(result.get("error") or "nothing was written"))
                failed += 1
        self._say(done=self._state["done"] + done, failed=self._state["failed"] + failed,
                  seconds=self._state["seconds"] + took,
                  owed=max(0, self._state["owed"] - len(found)))
        if stale:
            # A better model was downloaded since the host loaded this one:
            # the next batch starts a host that loads it.
            self._host().recycle()
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["enabled"] = self.enabled
        done = int(state.get("done") or 0) + int(state.get("failed") or 0)
        state["per_picture"] = round(state["seconds"] / done, 2) if done else None
        return state
