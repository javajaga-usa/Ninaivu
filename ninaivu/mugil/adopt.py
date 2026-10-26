"""On the Secondary: taking in a copy of the library made by hand.

Sending ten terabytes over the house network takes days. A household can
instead plug the Primary's library disk into the Secondary, or carry the
files over on another disk, and copy the library folder into the originals
folder itself, under ``<originals folder>/<library folder name>/`` with the
same folders and names as on the Primary. That is the **manual** way
(``mugil_copy_mode``); the **automatic** way has the Primary send them all.

While the copy is being made the Primary is told to wait (``copying``).
When the household says it is finished, every file copied is read here, end
to end, and its fingerprint written down (``checking``), the same kind of
fingerprint the Primary uses. Then the Primary is let in (``ready``), and as
it names each original (:meth:`.hold.Holding.have`) a file found at that path
with that very content is kept without crossing the network; anything
missing, cut short or different is sent as usual, and a different file is
set aside, never written over (:mod:`.hold`).

Nothing here changes a file's bytes. A copy made by hand may have given the
files the day they were copied as their modified time; the Primary's is put
back when the file is kept, as it is for a file that was sent.

The checking is written down as it goes, file by file, so a restart, a power
cut or the disk unplugged halfway carries on where it was: a file already
fingerprinted, and not changed since, is not read again.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import SECONDARY, role
from .hold import Holding, folder_problem, hasher
from ..storage.fingerprints import ALGO, CHUNK
from ..utils.files import atomic_write

log = logging.getLogger(__name__)

#: Where the state of a copy made by hand is kept, in the state folder.
STATE_FILE = "mugil-manual.json"

#: The household is still copying; the Primary waits.
COPYING = "copying"
#: The files copied are being fingerprinted; the Primary waits.
CHECKING = "checking"
#: Checked: the Primary sends what is missing or different.
READY = "ready"

#: Fingerprints written down at a time.
BATCH = 200


def _load(state_dir: str | Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(state_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(state_dir: str | Path, data: dict[str, Any]) -> None:
    atomic_write(Path(state_dir) / STATE_FILE, json.dumps(data, indent=2))


def manual(cfg) -> bool:
    return role(cfg) == SECONDARY and \
        str(getattr(cfg, "mugil_copy_mode", "automatic") or "automatic") == "manual"


def state(cfg) -> str:
    """Where a copy by hand stands; ``ready`` when the copy is automatic."""
    if not manual(cfg):
        return READY
    return str(_load(cfg.state_dir).get("state") or COPYING)


def walk(folder: Path):
    """``(path from the top, Path)`` of every plain file in *folder*, leaving
    out Ninaivu's own and the system's hidden ones (names starting with a
    dot: the record, set-aside files, partial files, ``.DS_Store``)."""
    for top, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                         and not os.path.islink(os.path.join(top, d)))
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = Path(top) / name
            if full.is_symlink():
                continue
            yield full.relative_to(folder).as_posix(), full


class Adopter:
    """The fingerprinting of a copy made by hand, on its own thread."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._progress: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        saved = _load(self.cfg.state_dir)
        with self._lock:
            progress = dict(self._progress)
        out = {"mode": "manual" if manual(self.cfg) else "automatic", "state": state(self.cfg),
               "running": self.running, "progress": progress,
               "started": saved.get("started"), "finished": saved.get("finished"),
               "files": saved.get("files"), "bytes": saved.get("bytes"),
               "error": saved.get("error")}
        folder = str(getattr(self.cfg, "mugil_originals_dir", "") or "")
        if folder and manual(self.cfg):
            try:
                out["found"] = Holding(folder).found_summary()
            except Exception:                            # noqa: BLE001 - shown as unknown
                out["found"] = None
        return out

    def copying(self) -> dict[str, Any]:
        """The household is copying (again): the Primary waits."""
        self.stop(join=True)
        _save(self.cfg.state_dir, {**_load(self.cfg.state_dir), "state": COPYING,
                                   "error": None})
        return self.status()

    def finished_copying(self) -> dict[str, Any]:
        """The copy is made: fingerprint it, then let the Primary in."""
        if not manual(self.cfg):
            return {**self.status(), "started": False}
        if self.running:
            return self.status()
        _save(self.cfg.state_dir, {**_load(self.cfg.state_dir), "state": CHECKING,
                                   "started": time.time(), "finished": None, "error": None})
        self._start()
        return self.status()

    def resume(self) -> None:
        """At start-up: carry on with a check a restart cut short."""
        if manual(self.cfg) and state(self.cfg) == CHECKING and not self.running:
            self._start()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        thread = self._thread
        if join and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_logged, name="mugil-adopt", daemon=True)
        self._thread.start()

    def _run_logged(self) -> None:
        try:
            outcome = self.run()
        except Exception as exc:                          # noqa: BLE001 - said, not raised
            log.exception("Checking the copy made by hand stopped")
            outcome = {"error": str(exc)}
        saved = _load(self.cfg.state_dir)
        if outcome.get("stopped"):
            # Stopped by a restart or by Still copying: whichever said so
            # wrote the state; a restart carries on from here.
            pass
        elif outcome.get("error"):
            saved.update(error=outcome["error"])
            _save(self.cfg.state_dir, saved)
        else:
            saved.update(state=READY, finished=time.time(), error=None,
                         files=outcome.get("files"), bytes=outcome.get("bytes"))
            _save(self.cfg.state_dir, saved)
        with self._lock:
            self._progress = {}

    def run(self) -> dict[str, Any]:
        folder_s = str(getattr(self.cfg, "mugil_originals_dir", "") or "")
        problem = folder_problem(folder_s, list(self.cfg.roots), self.cfg.state_dir) \
            if folder_s else "Choose the originals folder first."
        if problem:
            return {"error": problem}
        holding = Holding(folder_s)
        folder = holding.folder
        algo = str(_load(self.cfg.state_dir).get("algo") or ALGO)
        if hasher(algo) is None:
            algo = ALGO
        kept = holding.kept_paths()
        seen = holding.found_index()
        # Counted first, so the page can say how far it has got.
        todo: list[tuple[str, Path, int, float]] = []
        total = 0
        for rel, full in walk(folder):
            if self._stop.is_set():
                return {"stopped": True}
            if rel in kept:
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            todo.append((rel, full, st.st_size, st.st_mtime))
            total += st.st_size
        with self._lock:
            self._progress = {"files_total": len(todo), "bytes_total": total,
                              "files_done": 0, "bytes_done": 0, "unreadable": 0}
        batch: list[tuple[str, int, float, str, str]] = []
        done_files = done_bytes = unreadable = 0
        for rel, full, size, mtime in todo:
            if self._stop.is_set():
                holding.record_found(batch)
                return {"stopped": True}
            before = seen.get(rel)
            if not (before and before[0] == size and before[2] == algo
                    and abs(before[1] - mtime) <= 0.001):
                digest = self._fingerprint(full, algo, size)
                if digest is None:
                    if self._stop.is_set():
                        holding.record_found(batch)
                        return {"stopped": True}
                    unreadable += 1
                else:
                    batch.append((rel, size, mtime, algo, digest))
            done_files += 1
            done_bytes += size
            if len(batch) >= BATCH:
                holding.record_found(batch)
                batch = []
            with self._lock:
                self._progress.update(files_done=done_files, bytes_done=done_bytes,
                                      unreadable=unreadable)
        holding.record_found(batch)
        return {"files": done_files, "bytes": done_bytes, "unreadable": unreadable}

    def _fingerprint(self, path: Path, algo: str, size: int) -> str | None:
        """The fingerprint of *path*, or None when it could not be read whole
        or changed while it was read (it is sent by the Primary instead)."""
        fresh = hasher(algo)
        got = 0
        try:
            with open(path, "rb") as handle:
                while True:
                    if self._stop.is_set():
                        return None
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    got += len(chunk)
                    fresh.update(chunk)
            if got != size or path.stat().st_size != size:
                return None
        except OSError as exc:
            log.info("Could not read %s to check it: %s", path, exc)
            return None
        return fresh.hexdigest()


def remember_algo(state_dir: str | Path, algo: str) -> None:
    """The kind of fingerprint the Primary uses, so the check uses it too."""
    saved = _load(state_dir)
    if algo and saved.get("algo") != algo:
        saved["algo"] = algo
        _save(state_dir, saved)


__all__ = ["Adopter", "COPYING", "CHECKING", "READY", "STATE_FILE", "manual", "state", "walk",
           "remember_algo"]
