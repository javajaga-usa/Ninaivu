"""Updating a portable Ninaivu without carrying its data across by hand.

A portable Ninaivu is one folder, and until 1.12 an update was a second
folder: the newer download unpacked somewhere new, and then the household
moved the old folder's data into it by hand, following README-PORTABLE.txt.
For a library of two hundred thousand photographs that is an index, a
thumbnail cache and the AI models, in folders whose names differed between
Windows, the Mac and Linux. Moving the wrong one, or opening the new Ninaivu
before moving anything, started an empty library.

From 1.12 the program and what Ninaivu keeps are apart in every portable
folder::

    Ninaivu.exe | ninaivu | Ninaivu.app    opens the Control Panel
    app/          the program: its own Python and packages (not on the Mac,
                  where the program is inside Ninaivu.app)
    data/         the index, the settings, the thumbnails (data/ai-models:
                  the AI models; data/control: the Control Panel's own files)
    logs/         what Ninaivu wrote about itself

and updating is unpacking the newer download beside the folder in use and
opening it. Its Control Panel finds the older folder beside it and offers to
bring the library across (:func:`bring_here`): the older Ninaivu is stopped
if it is running, and its data is *moved*, not copied, so it takes a moment
on the same drive. The older folder keeps its program and a note saying where
the library went. The first start of the newer version then copies the index
before changing it, as every update does (``storage/upgrade.py``), and
"Go back" in the Control Panel (:func:`go_back`) uses that copy to move the
library back to the older folder as the older version left it.

A folder whose data is still where 1.11 and earlier kept it on Windows and
Linux (``app/data``, ``app/ai-models``, ``app/.ninaivu-control``) is put in
the new places on its first start (:func:`settle`): that is where somebody who
followed the old advice by hand put it.

The photographs are never read or written by any of this.

    python -m ninaivu.desktop.portable --status | --bring FOLDER | --go-back
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = ["Folder", "root", "this_folder", "settle", "settle_quietly", "older_beside", "bring_here",
           "brought_from", "can_go_back", "go_back", "PortableError", "MOVED_NOTE",
           "MOVED_RECORD", "BROUGHT_RECORD"]

#: In an older folder whose library was brought into a newer one: for people.
MOVED_NOTE = "LIBRARY-MOVED.txt"
#: The same, for the next Ninaivu that looks at the folder.
MOVED_RECORD = ".ninaivu-moved.json"
#: In the newer folder's data: where its library came from, for "Go back".
BROUGHT_RECORD = "brought-from.json"

#: The program files that make a folder a portable Ninaivu, per system.
_LAUNCHERS = {"win32": "Ninaivu.exe", "linux": "ninaivu", "darwin": ".ninaivu-portable"}
#: Where the index has been kept in a portable folder, newest layout first.
_STATE_PLACES = ("data", "app/data")
#: How long a running older Ninaivu is given to stop when asked.
STOP_PATIENCE = 90.0


class PortableError(RuntimeError):
    """The step could not be taken; the text says why, for the person."""


def _system(platform: str | None = None) -> str:
    platform = platform or sys.platform
    return "win32" if platform.startswith("win") else "darwin" if platform == "darwin" else "linux"


def _version_key(version: str | None) -> tuple[int, ...]:
    """'1.11.0' as (1, 11, 0); anything unreadable sorts first."""
    return tuple(int(part) for part in re.findall(r"\d+", str(version or ""))[:4]) or (0,)


@dataclass(frozen=True)
class Folder:
    """One portable Ninaivu folder and where it keeps each thing."""

    path: Path
    state: Path
    models: Path
    control: Path

    @property
    def has_library(self) -> bool:
        return (self.state / "index.db").is_file()

    @property
    def version(self) -> str | None:
        """The version that last started on this folder's data."""
        from ..storage import upgrade                    # noqa: PLC0415
        record = upgrade.recorded(self.state)
        return str(record["version"]) if record else None

    @property
    def moved_to(self) -> Path | None:
        try:
            told = json.loads((self.path / MOVED_RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return Path(told["to"]) if isinstance(told, dict) and told.get("to") else None

    def last_used(self) -> float:
        try:
            return (self.state / "index.db").stat().st_mtime
        except OSError:
            return 0.0


def _layout(path: Path, platform: str | None = None) -> Folder | None:
    """*path* as a portable Ninaivu folder of this system, or None."""
    system = _system(platform)
    if not (path / _LAUNCHERS[system]).exists():
        return None
    if system == "darwin":
        if not (path / "Ninaivu.app").is_dir():
            return None
        # The Mac app has always kept everything in data/.
        state = path / "data"
        return Folder(path, state, state / "ai-models", state / "control" / ".ninaivu-control")
    if not (path / "app" / "python").is_dir():
        return None
    for place in _STATE_PLACES:
        state = path / place
        if (state / "index.db").is_file():
            break
    else:
        state = path / "data"
    if state == path / "app" / "data":
        # 1.11 and earlier on Windows and Linux: everything inside app/.
        return Folder(path, state, path / "app" / "ai-models", path / "app" / ".ninaivu-control")
    return Folder(path, state, state / "ai-models", state / "control" / ".ninaivu-control")


def root(environ: dict[str, str] | None = None) -> Path | None:
    """The portable folder this Ninaivu runs from, or None when it is not one.

    Its launcher says so (``NINAIVU_PORTABLE_ROOT``); a launcher from before
    1.12 did not, and its logs folder is at the top of the portable folder.
    """
    environ = os.environ if environ is None else environ
    if not environ.get("NINAIVU_PORTABLE"):
        return None
    told = environ.get("NINAIVU_PORTABLE_ROOT", "").strip()
    if told:
        return Path(told)
    logs = environ.get("NINAIVU_LOG_DIR", "").strip()
    return Path(logs).parent if logs else None


def this_folder(environ: dict[str, str] | None = None) -> Folder | None:
    """Where this Ninaivu keeps each thing, as its launcher told it."""
    environ = os.environ if environ is None else environ
    path = root(environ)
    if path is None:
        return None
    state = Path(environ.get("NINAIVU_STATE_DIR") or path / "data")
    models = Path(environ.get("NINAIVU_AI_MODELS_DIR") or state / "ai-models")
    home = Path(environ.get("NINAIVU_HOME") or state / "control")
    return Folder(path, state, models, home / ".ninaivu-control")


# ---------------------------------------------------------------------------
# Moving


def _empty(path: Path) -> bool:
    """Whether *path* is missing, or a folder with nothing that matters in it."""
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    return all(child.is_dir() and _empty(child) for child in path.iterdir())


def _move(source: Path, target: Path, skipped: list[str]) -> None:
    """Move *source* to *target*: renamed whole when *target* is not there or
    holds nothing (the launcher makes empty folders before anything else
    runs), and merged item by item into a folder that does hold something.
    A file already at *target* is left alone, and so is the one at *source*,
    and both are named in *skipped*: nothing is overwritten."""
    if not source.exists() and not source.is_symlink():
        return
    if _empty(target) and target.is_dir():
        shutil.rmtree(target)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)
        return
    if source.is_dir() and target.is_dir() and not source.is_symlink():
        for child in list(source.iterdir()):
            _move(child, target / child.name, skipped)
        try:
            source.rmdir()
        except OSError:
            pass                         # something was skipped and is still there
        return
    skipped.append(str(source))


def _same_drive(one: Path, other: Path) -> bool:
    def device(path: Path) -> int | None:
        while not path.exists():
            if path == path.parent:
                return None
            path = path.parent
        try:
            return path.stat().st_dev
        except OSError:
            return None
    first, second = device(one), device(other)
    return first is not None and first == second


def _running(state: Path) -> dict[str, Any] | None:
    """The run file of a server using *state*, or None when none is."""
    from ..server import runfile                         # noqa: PLC0415
    if (state / "ninaivu-server.lock").exists():
        try:
            with runfile.server_lock(state):
                pass
        except runfile.AlreadyRunning:
            return runfile.read(state) or {"pid": 0}
        except OSError:
            pass
    record = runfile.read(state)
    if record:
        try:
            if runfile.is_running(int(record["pid"])):
                return record
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    return None


def _stop(state: Path, *, patience: float = STOP_PATIENCE,
          clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> None:
    """Stop the server using *state*, as its own Stop button would, or raise."""
    record = _running(state)
    if record is None:
        return
    from ..server import stop                            # noqa: PLC0415
    asked = False
    try:
        asked = stop.ask_to_stop(int(record.get("port") or record.get("admin_port") or 0),
                                 str(record.get("token") or ""),
                                 scheme=str(record.get("scheme") or "http"), state_dir=state)
    except (TypeError, ValueError):
        asked = False
    deadline = clock() + patience
    while asked and clock() < deadline:
        if _running(state) is None:
            return
        sleep(1.0)
    raise PortableError(
        "The older Ninaivu is still running and could not be asked to stop. Open its "
        "Control Panel, press Stop and wait until it says Stopped, then try again.")


def settle(folder: Folder | None = None) -> list[str]:
    """Put a folder's data where 1.12 keeps it, if it is where 1.11 kept it.

    On Windows and Linux the index, the models and the Control Panel's files
    were inside ``app/``. A 1.12 folder that finds them there (somebody moved
    them in by hand, as the older README said) moves them to ``data/`` before
    anything opens them. Returns what was moved, for the log.
    """
    folder = folder or this_folder()
    if folder is None:
        return []
    old = folder.path / "app"
    moves = [(old / "data", folder.state), (old / "ai-models", folder.models),
             (old / ".ninaivu-control", folder.control)]
    if not (old / "data" / "index.db").is_file() or (folder.state / "index.db").is_file():
        return []
    if _running(old / "data") is not None:
        return []
    done, skipped = [], []
    for source, target in moves:
        if source.exists() and source != target:
            _move(source, target, skipped)
            done.append(f"{source} -> {target}")
    for name in skipped:
        log.warning("left where it was, a file of that name being there already: %s", name)
    for line in done:
        log.info("moved to where Ninaivu 1.12 keeps it: %s", line)
    return done


def settle_quietly() -> None:
    """:func:`settle` for a start that must go on whatever happens: a
    failure is logged, and the data stays where it was."""
    try:
        settle()
    except Exception:                                    # noqa: BLE001
        log.exception("could not move the data to where Ninaivu 1.12 keeps it")


def older_beside(folder: Folder | None = None, version: str | None = None) -> Folder | None:
    """The portable Ninaivu beside this one whose library could be brought here.

    Only when this folder has no library of its own. Folders beside it with a
    library of the same system's portable Ninaivu are candidates; one whose
    library a newer Ninaivu than this one last opened is not (this version
    could not open it); the one used most recently is chosen.
    """
    from .. import __version__                           # noqa: PLC0415

    folder = folder or this_folder()
    if folder is None or folder.has_library:
        return None
    mine = _version_key(version or __version__)
    found = []
    try:
        siblings = list(folder.path.parent.iterdir())
    except OSError:
        return None
    for path in siblings:
        try:
            if not path.is_dir() or path.resolve() == folder.path.resolve():
                continue
        except OSError:
            continue
        other = _layout(path)
        if other is None or not other.has_library or other.moved_to:
            continue
        if _version_key(other.version) > mine:
            continue
        found.append(other)
    return max(found, key=Folder.last_used) if found else None


def _note_moved(older: Folder, newer: Folder, version: str) -> None:
    record = {"to": str(newer.path), "version": version, "at": time.time()}
    (older.path / MOVED_RECORD).write_text(json.dumps(record, indent=2), encoding="utf-8")
    (older.path / MOVED_NOTE).write_text(
        f"The library this folder kept was moved to Ninaivu {version}, in\n\n"
        f"    {newer.path}\n\n"
        f"on {time.strftime('%Y-%m-%d %H:%M')}. Open Ninaivu from that folder.\n\n"
        f"Opened from here, this Ninaivu would start with an empty library. To\n"
        f"use this version again, choose \"Go back\" in the newer Control Panel:\n"
        f"it brings the library back here as this version left it.\n"
        f"Your photographs were not moved or changed.\n", encoding="utf-8")


def _follow_sign_in(older: Folder) -> bool:
    """Start this copy at sign-in if the older one was set to start then.

    The sign-in start names the folder it starts, so left alone it would
    open the older folder, now without its library, at the next sign-in.
    """
    from . import autostart                              # noqa: PLC0415
    platform = sys.platform
    old = os.path.normcase(str(older.path))
    try:
        if platform == "win32":
            stored = autostart._stored_command()          # noqa: SLF001
            if not stored or old not in os.path.normcase(stored):
                return False
        elif platform == "darwin":
            import plistlib                              # noqa: PLC0415
            agent = autostart.agent_path()
            if not agent.is_file():
                return False
            if old not in os.path.normcase(json.dumps(plistlib.loads(agent.read_bytes()),
                                                      default=str)):
                return False
        else:
            return False
        autostart.enable()
        return True
    except Exception:                                    # noqa: BLE001
        log.exception("could not move the sign-in start to this folder")
        return False


def bring_here(older: Folder, folder: Folder | None = None, *, version: str | None = None,
               stop_older: Callable[[Path], None] = _stop) -> dict[str, Any]:
    """Move *older*'s library, models and Control Panel files into this folder.

    The older Ninaivu is stopped first if it is running. Everything is moved
    on the same drive, so nothing is copied and nothing is left half-done: a
    rename is whole or not at all. Raises :class:`PortableError`, having
    changed nothing, when the move cannot be made.
    """
    from .. import __version__                           # noqa: PLC0415

    version = version or __version__
    folder = folder or this_folder()
    if folder is None:
        raise PortableError("This Ninaivu is not a portable copy.")
    if folder.has_library:
        raise PortableError(f"This Ninaivu already has a library, in {folder.state}.")
    if not older.has_library:
        raise PortableError(f"There is no library in {older.path}.")
    # Read now: it is in the data about to be moved.
    older_version = older.version
    if _version_key(older_version) > _version_key(version):
        raise PortableError(
            f"The library in {older.path} was last opened by Ninaivu {older_version}, "
            f"which is newer than this one ({version}). Use Ninaivu {older_version} or later.")
    if not _same_drive(older.state, folder.path):
        raise PortableError(
            f"{older.path} is on another drive. Unpack the newer Ninaivu on the same drive "
            f"as the older one, beside it, and open it from there: the library is then moved "
            f"in a moment instead of copied.")
    stop_older(older.state)
    _stop(folder.state)

    skipped: list[str] = []
    moved = []
    try:
        for source, target in ((older.state, folder.state), (older.models, folder.models),
                               (older.control, folder.control)):
            if source.exists() and os.path.normcase(str(source)) != os.path.normcase(str(target)):
                _move(source, target, skipped)
                moved.append({"from": str(source), "to": str(target)})
    except OSError as exc:
        raise PortableError(
            f"The library could not be moved ({exc}). Close the older Ninaivu's Control Panel "
            f"and any window showing its folder, then try again. Whatever was moved already "
            f"is in {folder.path}, and the older folder has the rest.") from exc

    record = {"folder": str(older.path), "version": older_version, "at": time.time(),
              "moved": moved, "to": version}
    folder.state.mkdir(parents=True, exist_ok=True)
    (folder.state / BROUGHT_RECORD).write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        _note_moved(older, folder, version)
    except OSError as exc:
        log.warning("could not leave a note in %s: %s", older.path, exc)
    record["sign_in"] = _follow_sign_in(older)
    record["skipped"] = skipped
    log.info("brought the library from %s (Ninaivu %s) into %s", older.path, older_version,
             folder.path)
    return record


# ---------------------------------------------------------------------------
# Going back


def brought_from(folder: Folder | None = None) -> dict[str, Any] | None:
    """What :func:`bring_here` recorded, when this folder's library came from
    an older one that is still there."""
    folder = folder or this_folder()
    if folder is None:
        return None
    try:
        record = json.loads((folder.state / BROUGHT_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("folder"):
        return None
    return record if _layout(Path(record["folder"])) is not None else None


def can_go_back(folder: Folder | None = None) -> str | None:
    """The version "Go back" would return to, or None when there is none."""
    record = brought_from(folder)
    return str(record.get("version") or "the older version") if record else None


def _copy_before(state: Path, version: str) -> Path | None:
    """The newest copy taken before *version* first changed the index here."""
    from ..storage import backup, upgrade                # noqa: PLC0415
    import tarfile                                       # noqa: PLC0415

    for bundle in backup.bundles(state / upgrade.BEFORE_UPDATE):
        try:
            with tarfile.open(bundle["path"], "r:gz") as tar:
                member = tar.extractfile("state/backup_manifest.json")
                manifest = json.loads(member.read()) if member else {}
        except (OSError, ValueError, KeyError, tarfile.TarError, EOFError):
            continue
        noted = manifest.get("note") if isinstance(manifest, dict) else None
        note = noted.get("before_update") if isinstance(noted, dict) else None
        if isinstance(note, dict) and note.get("to") == version:
            return Path(bundle["path"])
    return None


def _put_back_index(state: Path, bundle: Path) -> None:
    """The databases and settings in *state* replaced by those in *bundle*,
    checked first; nothing else in the folder is touched."""
    from ..storage import backup                         # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix=".ninaivu-go-back-", dir=state) as tmp:
        copy = backup.extract(bundle, Path(tmp))
        backup.verify_state(copy)
        for name in (*backup.DATABASES, "config.json"):
            if not (copy / name).is_file():
                continue
            for suffix in ("-wal", "-shm", "-journal"):
                (state / f"{name}{suffix}").unlink(missing_ok=True)
            os.replace(copy / name, state / name)


def go_back(folder: Folder | None = None, *, version: str | None = None,
            stop_this: Callable[[Path], None] = _stop) -> dict[str, Any]:
    """Move this folder's library back to the older folder it came from.

    When this version has changed the index since (its first start does),
    the index and settings are put back from the copy that start took, so
    the older version finds them as it left them; what was done in this
    version since is not kept. Raises :class:`PortableError` when it cannot.
    """
    from .. import __version__                           # noqa: PLC0415
    from ..storage import upgrade                        # noqa: PLC0415

    version = version or __version__
    folder = folder or this_folder()
    record = brought_from(folder)
    if folder is None or record is None:
        raise PortableError("This library was not brought from an older Ninaivu beside this one.")
    older = _layout(Path(record["folder"]))
    if older is None:
        raise PortableError(f"The older Ninaivu is no longer in {record['folder']}.")
    if older.has_library:
        raise PortableError(f"{older.path} has a library of its own again, so nothing was moved.")
    stop_this(folder.state)

    bundle = None
    last = upgrade.recorded(folder.state)
    if last and last.get("version") != record.get("version"):
        bundle = _copy_before(folder.state, str(last.get("version")))
        if bundle is None:
            raise PortableError(
                "No copy of the index from before this update was found, so the older "
                "version could not be given the index as it left it. Nothing was moved.")
        _put_back_index(folder.state, bundle)
        (folder.state / upgrade.VERSION_FILE).unlink(missing_ok=True)
    (folder.state / BROUGHT_RECORD).unlink(missing_ok=True)

    skipped: list[str] = []
    for moved in reversed(record.get("moved") or []):
        source, target = Path(moved["to"]), Path(moved["from"])
        if source.exists():
            _move(source, target, skipped)
    for name in (MOVED_RECORD, MOVED_NOTE):
        (older.path / name).unlink(missing_ok=True)
    log.info("took the library back to %s (Ninaivu %s)", older.path, record.get("version"))
    return {"folder": str(older.path), "version": record.get("version"),
            "index_put_back": str(bundle) if bundle else None, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true")
    action.add_argument("--bring", metavar="FOLDER")
    action.add_argument("--go-back", action="store_true")
    args = parser.parse_args(argv)
    folder = this_folder()
    if folder is None:
        print("This Ninaivu is not a portable copy.", file=sys.stderr)
        return 1
    try:
        if args.status:
            older = older_beside(folder)
            print(f"this folder: {folder.path}")
            print(f"library here: {'yes' if folder.has_library else 'no'}")
            print(f"older Ninaivu beside it: {older.path if older else 'none'}")
            print(f"go back to: {can_go_back(folder) or 'nothing'}")
        elif args.bring:
            older = _layout(Path(args.bring).expanduser())
            if older is None:
                raise PortableError(f"{args.bring} is not a portable Ninaivu folder.")
            done = bring_here(older, folder)
            print(f"Brought the library from {done['folder']}.")
        else:
            done = go_back(folder)
            print(f"The library is back in {done['folder']}.")
    except PortableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
