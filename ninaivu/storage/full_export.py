"""The whole index, out of one computer and into another, from the console.

A backup (storage/backup.py) keeps what cannot be worked out again: the
index, the settings, the keys. A full export is the same bundle with what can
be worked out again as well — the thumbnails, the face crops, the films and
letters and books (:data:`backup.FULL_EXTRAS`) — because on a large library
"worked out again" is days of a computer's time, and the reason somebody is
moving the index is that they would rather not spend them.

Exporting runs while Ninaivu runs, like any backup. Importing cannot: it
replaces the folder the running server has open. So the console only *asks*
for an import (:func:`ask_for_import`), and the next start does it before
anything is opened (:func:`apply_pending`, from ``__main__``), with the same
checks and the same way back as ``ninaivu restore``. The folder that was
there is kept beside it, as a restore keeps it.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import zlib
from pathlib import Path
from typing import Any

from .. import __version__
from . import backup
from ..words import said

log = logging.getLogger(__name__)

#: The import the next start is to do: which bundle, asked for when, by whom.
PENDING = "import-pending.json"
#: What the last import did, for the console to say after the restart.
RESULT = "import-result.json"

#: What the last export did, so the console still says it after a restart.
EXPORT_RESULT = "export-result.json"

#: The steps of an export, in order (backup._snapshot).
EXPORT_STEPS = ("copying", "checking", "verifying", "packing")

#: Room a full export needs on top of what it holds: the manifest, the tar's
#: headers, and a disk that should not be left with nothing at all.
SPARE_BYTES = 256 * 1024 * 1024


class Refused(ValueError):
    """An export or import that was not done; :attr:`said` says why, for the
    console. Always one of this module's own sentences, never another
    error's own text."""

    def __init__(self, said: str) -> None:
        super().__init__(said)
        self.said = said


def _version_key(version: str | None) -> tuple[int, ...]:
    """'3.1.0' as (3, 1, 0); anything unreadable sorts first."""
    return tuple(int(part) for part in re.findall(r"\d+", str(version or ""))[:4]) or (0,)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Exporting
# ---------------------------------------------------------------------------

def size_of_export(state_dir: Path | str) -> int:
    """Roughly how many bytes a full export of *state_dir* holds."""
    state_dir = Path(state_dir)
    total = 0
    names = (*backup.DATABASES, *backup.EXTRAS, *backup.FULL_EXTRAS)
    for name in names:
        path = state_dir / name
        if path.is_file():
            total += path.stat().st_size
        elif path.is_dir() and not path.is_symlink():
            for here, _, files in os.walk(path):
                for file in files:
                    try:
                        total += os.stat(os.path.join(here, file)).st_size
                    except OSError:
                        continue
    return total


def check_export_folder(folder: Path, cfg) -> None:
    """Refused, in words for the console, if a full export must not go
    in *folder*."""
    if not str(folder).strip() or not folder.is_absolute():
        raise Refused(said("Give the full path of the folder to write the export to."))
    if not folder.is_dir():
        raise Refused(f"{folder} is not a folder on this computer. Check the drive "
                         "is plugged in and the path is right.")
    try:
        backup.check_folder(folder, cfg)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    if backup._inside(folder, Path(cfg.state_dir)):
        raise Refused(said(
            "The export cannot go inside Ninaivu's own folder. Choose a folder on the drive you are taking to the other computer."))


class Exporter:
    """One full export at a time, in the background, with what it is doing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self, state_dir: Path | str | None = None) -> dict[str, Any]:
        """What the export is doing now, or what the last one did: from
        memory, or after a restart from :data:`EXPORT_RESULT` in *state_dir*."""
        with self._lock:
            state = {**self._state, "running": self.running}
        if not state["running"] and "finished" not in state and state_dir is not None:
            state = {**(_read_json(Path(state_dir) / EXPORT_RESULT) or {}), "running": False}
        now = time.time()
        if state.get("started"):
            state["seconds"] = round((state.get("finished") or now) - state["started"], 1)
        if state["running"] and state.get("step") in EXPORT_STEPS:
            state["step_number"] = EXPORT_STEPS.index(state["step"]) + 1
            state["steps"] = len(EXPORT_STEPS)
        return state

    def start(self, cfg, folder: Path, *, without_secrets: bool = False) -> dict[str, Any]:
        """Begin an export of *cfg*'s state into *folder*. Refused if it
        cannot be made there, or one is already being made."""
        folder = Path(folder).expanduser()
        check_export_folder(folder, cfg)
        with self._lock:
            if self.running:
                raise Refused(said("An export is already being made."))
            needed = size_of_export(cfg.state_dir) + SPARE_BYTES
            free = shutil.disk_usage(folder).free
            if free < needed:
                raise Refused(
                    f"{folder} has {free // 1048576:,} MB free, and the export needs about "
                    f"{needed // 1048576:,} MB. Choose a bigger drive.")
            self._state = {"running": True, "step": "copying", "folder": str(folder),
                           "started": time.time(), "expected_bytes": needed - SPARE_BYTES,
                           "without_secrets": without_secrets}
            self._thread = threading.Thread(
                target=self._run, args=(Path(cfg.state_dir), folder, without_secrets),
                name="full-export", daemon=True)
            self._thread.start()
        return self.status()

    def _step(self, step: str, **counts: int) -> None:
        with self._lock:
            self._state["step"] = step
            self._state.update(counts)

    def _run(self, state_dir: Path, folder: Path, without_secrets: bool) -> None:
        made = None
        error = None
        try:
            made = backup.snapshot(
                state_dir, folder, full=True, progress=self._step,
                leave_out=backup.KEPT_HOME if without_secrets else (),
                blank_secrets=without_secrets)
            if made is None:
                error = "The export could not be made. Ninaivu's log says what went wrong."
        except Exception:                                 # noqa: BLE001
            log.exception("the full export failed")
            error = "The export could not be made. Ninaivu's log says what went wrong."
        with self._lock:
            self._state.update({"step": None, "finished": time.time(), "error": error,
                                "ok": made is not None})
            if made is not None:
                self._state.update({"bundle": str(made), "bytes": made.stat().st_size})
            kept = {key: value for key, value in self._state.items() if key != "running"}
        try:
            _write_json(state_dir / EXPORT_RESULT, kept)
        except OSError as exc:
            log.warning("could not note how the export went: %s", exc)
        if made is not None:
            log.info("full export written: %s", made)


# ---------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------

def find_bundle(path: Path) -> Path:
    """*path* itself, or the newest export (then backup) in it when it is a
    folder. Refused when there is none."""
    path = Path(path).expanduser()
    if path.is_file():
        return path
    if path.is_dir():
        for prefix in (backup.EXPORT_PREFIX, backup.PREFIX):
            found = sorted(path.glob(f"{prefix}*.tar.gz"), reverse=True)
            if found:
                return found[0]
        raise Refused(f"There is no Ninaivu export in {path}.")
    raise Refused(f"{path} is not on this computer. Check the drive is plugged in "
                     "and the path is right.")


def look(path: Path) -> dict[str, Any]:
    """What the bundle at *path* holds, read from its manifest without
    unpacking it. Refused when it is not one Ninaivu can import.

    Everything is checked again when it is imported, before anything is
    replaced; this is so the console can say what is about to come in.
    """
    archive = find_bundle(path)
    manifest = None
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if member.name == "state/backup_manifest.json" and member.isfile():
                    handle = tar.extractfile(member)
                    manifest = json.loads(handle.read().decode("utf-8")) if handle else None
                    break
    except (OSError, ValueError, tarfile.TarError, EOFError, zlib.error) as exc:
        log.warning("could not read %s as an export: %s", archive, exc)
        raise Refused(f"{archive.name} cannot be read as a Ninaivu export. It may be "
                      "damaged, or not a Ninaivu export at all.") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise Refused(f"{archive.name} is not a Ninaivu export: it has no list of what is in it.")
    files = manifest["files"]
    if "index.db" not in files:
        raise Refused(f"{archive.name} has no index in it.")
    from ..cloud import index_copy                         # noqa: PLC0415
    if manifest.get("kind") == index_copy.KIND:
        raise Refused(said(
            "That is the copy of the index kept in Drive. Bring it back from the Restore page instead."))
    if _version_key(manifest.get("version")) > _version_key(__version__):
        raise Refused(f"It was made by Ninaivu {manifest.get('version')}, newer than this "
                         f"one ({__version__}). Update Ninaivu here first, then import it.")
    folders: dict[str, int] = {}
    for name in files:
        top = str(name).split("/", 1)[0]
        if top in backup.FULL_EXTRAS:
            folders[top] = folders.get(top, 0) + 1
    return {
        "bundle": str(archive),
        "name": archive.name,
        "bytes": archive.stat().st_size,
        "full": manifest.get("kind") == backup.FULL_KIND,
        "version": manifest.get("version"),
        "created_at": manifest.get("created_at"),
        "from": manifest.get("source_state_dir"),
        "files": len(files),
        "counts": manifest.get("counts") or {},
        "folders": folders,
        "thumbnails": folders.get("thumbs", 0),
        "left_out": manifest.get("left_out") or [],
    }


def ask_for_import(state_dir: Path | str, path: Path, *, by: str = "") -> dict[str, Any]:
    """Have the next start import the bundle at *path*. Refused if it
    cannot be imported. Returns what :func:`look` said of it."""
    found = look(path)
    _write_json(Path(state_dir) / PENDING, {
        "bundle": found["bundle"], "asked_at": time.time(), "by": by,
        "bytes": found["bytes"]})
    return found


def pending(state_dir: Path | str) -> dict[str, Any] | None:
    return _read_json(Path(state_dir) / PENDING)


def cancel(state_dir: Path | str) -> bool:
    try:
        (Path(state_dir) / PENDING).unlink()
        return True
    except FileNotFoundError:
        return False


def last_result(state_dir: Path | str) -> dict[str, Any] | None:
    return _read_json(Path(state_dir) / RESULT)


class _Both(io.TextIOBase):
    """Writes to two streams at once."""

    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            try:
                stream.write(text)
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass


def apply_pending(state_dir: Path | str) -> bool | None:
    """Do the import the console asked for, before Ninaivu opens anything.

    None when none was asked for; otherwise whether it was done. Taken off
    first, so an import that fails is tried once and not at every start: the
    folder that was there stays in place, and the console says what went
    wrong.
    """
    marker = Path(state_dir) / PENDING
    asked = _read_json(marker)
    try:
        marker.unlink()
    except FileNotFoundError:
        return None                 # none asked for, or another start took it
    except OSError as exc:
        log.error("the import asked for could not be started: %s", exc)
        return False
    bundle = Path(str((asked or {}).get("bundle") or ""))
    started = time.time()
    try:
        found = look(bundle)
    except (Refused, OSError):
        found = {}
    print(f"Importing the index from {bundle} before starting.", flush=True)
    from ..cli import backup_restore                       # noqa: PLC0415
    said = io.StringIO()
    try:
        # What the restore says went wrong is what the console shows; it
        # still reaches the server's log as well.
        with contextlib.redirect_stderr(_Both(said, sys.stderr)):
            done = bool(str(bundle)) and backup_restore.restore_backup(bundle)
        lines = [line.strip(" ✗") for line in said.getvalue().splitlines() if line.strip()]
        error = None if done else (lines[-1] if lines else "The import was refused.")
    except Exception:                                     # noqa: BLE001
        log.exception("the import failed")
        done, error = False, "The import failed part-way; Ninaivu's log says why."
    try:
        _write_json(Path(state_dir) / RESULT, {
            "ok": done, "at": time.time(), "started": started,
            "seconds": round(time.time() - started, 1), "bundle": str(bundle),
            "asked_at": (asked or {}).get("asked_at"), "error": error,
            "files": found.get("files"), "bytes": found.get("bytes"),
            "thumbnails": found.get("thumbnails"), "counts": found.get("counts") or {},
            "made_by": found.get("version"), "full": found.get("full")})
    except OSError:
        pass
    return done
