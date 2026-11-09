"""Updating a portable Ninaivu without carrying its data across by hand.

A portable Ninaivu is one folder, and until 1.12 an update was a second
folder: the newer download unpacked somewhere new, and then the household
moved the old folder's data into it by hand, following README-PORTABLE.txt.
For a library of two hundred thousand photographs that is an index, a
thumbnail cache and the AI models, in folders whose names differed between
Windows, the Mac and Linux. Moving the wrong one, or opening the new Ninaivu
before moving anything, started an empty library.

The traditional portable layout separates program and library data::

    Ninaivu.exe | ninaivu | Ninaivu.app    opens the Control Panel
    app/          the program: its own Python and packages (not on the Mac,
                  where the program is inside Ninaivu.app)
    data/         the index, the settings, the thumbnails (data/ai-models:
                  the AI models; data/control: the Control Panel's own files)
    logs/         what Ninaivu wrote about itself

New Linux installations instead keep their library in ``~/.ninaivu``; existing
app-local libraries keep their location. An index relocated from the console
also stays in the chosen folder, including state-relative models and panel
settings. Shared library ownership is recorded so an inactive program cannot
reopen and upgrade the index after a Go back.

Updating means unpacking the newer download beside the folder in use and
opening it. Its Control Panel finds the older folder beside it and offers to
bring the library across (:func:`bring_here`): the older Ninaivu is stopped
if it is running. App-local data is moved on the same drive; a home-folder or
relocated index stays in place and the new program takes over. The older folder keeps its program and a note saying where
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
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = ["Folder", "root", "this_folder", "settle", "settle_quietly", "older_beside", "bring_here",
           "brought_from", "can_go_back", "go_back", "launch", "PortableError", "MOVED_NOTE",
           "MOVED_RECORD", "BROUGHT_RECORD"]

#: In an older folder whose library was brought into a newer one: for people.
MOVED_NOTE = "LIBRARY-MOVED.txt"
#: The same, for the next Ninaivu that looks at the folder.
MOVED_RECORD = ".ninaivu-moved.json"
#: In the newer folder's data: where its library came from, for "Go back".
BROUGHT_RECORD = "brought-from.json"
#: In the newer folder itself, while a go-back is under way: the same record,
#: kept outside the data that is moving, so a go-back cut short can finish.
GOING_BACK = ".ninaivu-going-back.json"
OWNER = "portable-owner.json"

#: The program files that make a folder a portable Ninaivu, per system.
_LAUNCHERS = {"win32": "Ninaivu.exe", "linux": "ninaivu", "darwin": ".ninaivu-portable"}
#: Where the index has been kept in a portable folder, newest layout first.
_STATE_PLACES = (".ninaivu", "data", "app/data")
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
    state_home: Path | None = None

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


def _live(folder: Folder) -> Folder:
    """Resolve a launcher path without treating its recovery copy as live data."""
    from ..storage import relocate
    try:
        state = relocate.resolve(folder.state)
    except (OSError, ValueError) as exc:
        raise PortableError(str(exc)) from exc
    def follow(path):
        return state / path.relative_to(folder.state) if path.is_relative_to(folder.state) else path
    return replace(folder, state=state, models=follow(folder.models), control=follow(folder.control),
                   state_home=folder.state_home or folder.state)


def _point_to(home: Path, state: Path) -> None:
    from ..storage import relocate
    if home.resolve() != state.resolve():
        home.mkdir(parents=True, exist_ok=True)
        relocate.write(home / relocate.REDIRECT, {'destination': str(state)})


def _layout(path: Path, platform: str | None = None) -> Folder | None:
    """*path* as a portable Ninaivu folder of this system, or None."""
    path = path.expanduser().resolve()
    system = _system(platform)
    if not (path / _LAUNCHERS[system]).exists():
        return None
    if system == "darwin":
        if not (path / "Ninaivu.app").is_dir():
            return None
        # The Mac app has always kept everything in data/.
        state = path / "data"
        return _live(Folder(path, state, state / "ai-models", state / "control" / ".ninaivu-control"))
    if not (path / "app" / "python").is_dir():
        return None
    for place in _STATE_PLACES:
        state = path / place
        if (state / "index.db").is_file() or (state / "index-location.json").is_file():
            break
    else:
        home_state = Path.home() / '.ninaivu'
        if system == 'linux' and _owner(home_state) == path.resolve():
            state = home_state
        else:
            # A program whose library was moved away must not accidentally
            # claim the user's unrelated home-folder index.
            state = path / 'data'
    if state == path / "app" / "data":
        # 1.11 and earlier on Windows and Linux: everything inside app/.
        return _live(Folder(path, state, path / "app" / "ai-models", path / "app" / ".ninaivu-control"))
    return _live(Folder(path, state, state / "ai-models", state / "control" / ".ninaivu-control"))


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
    default_state = (Path.home() / '.ninaivu' if sys.platform.startswith('linux')
                     and not (path / 'data').is_dir() else path / 'data')
    state = Path(environ.get("NINAIVU_STATE_DIR") or default_state)
    models = Path(environ.get("NINAIVU_AI_MODELS_DIR") or state / "ai-models")
    home = Path(environ.get("NINAIVU_HOME") or state / "control")
    return _live(Folder(path, state, models, home / ".ninaivu-control"))


def _owner(state: Path) -> Path | None:
    try:
        from ..storage.relocate import resolve
        state = resolve(state)
        record = json.loads((state / OWNER).read_text(encoding='utf-8'))
        path = record.get('folder') if isinstance(record, dict) else None
        return Path(path) if isinstance(path, str) and Path(path).is_absolute() else None
    except (OSError, ValueError):
        return None


def note_active(state: Path, path: Path | None = None) -> None:
    from ..storage.relocate import resolve, write
    path = path or root()
    if path is not None:
        write(resolve(state) / OWNER, {'folder': str(path.resolve())})


def require_active() -> None:
    """An inactive program must not upgrade the shared index after Go back."""
    path = root()
    if path is None:
        return
    try:
        record = json.loads((path / MOVED_RECORD).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        record = {}
    folder = this_folder()
    owner = _owner(folder.state) if folder else None
    if owner is not None and owner != path.resolve():
        record = {'to': str(owner)}
    if isinstance(record, dict) and record.get('to'):
        raise PortableError(f"This portable copy is inactive. Open Ninaivu from {record['to']}. "
                            "Bring the library across again before using this copy.")


def activate_environment() -> None:
    """Carry state-relative model and panel folders through forwarding notes.

    For any launcher that names the index folder (a portable folder, a
    checkout, a service): without this a start after the one that moved the
    index found the index in its new folder but went on taking its models
    from the recovery copy, until cleaning that copy took them away.
    """
    if not os.environ.get('NINAIVU_STATE_DIR'):
        return
    from ..storage.relocate import resolve
    configured = Path(os.environ['NINAIVU_STATE_DIR']).expanduser().resolve()
    live = resolve(configured)
    if live == configured:
        return
    for name in ('NINAIVU_HOME', 'NINAIVU_AI_MODELS_DIR'):
        if os.environ.get(name):
            path = Path(os.environ[name]).expanduser().resolve()
            if path.is_relative_to(configured):
                os.environ[name] = str(live / path.relative_to(configured))
    os.environ['NINAIVU_STATE_DIR'] = str(live)


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


def _undo_moves(moved: list[dict[str, str]]) -> bool:
    """Put back what :func:`bring_here` moved, newest first. True when all of
    it went back."""
    whole = True
    for step in reversed(moved):
        source, target = Path(step["to"]), Path(step["from"])
        try:
            if source.exists():
                _move(source, target, [])
        except OSError as exc:
            log.warning("could not put %s back in %s: %s", source, target, exc)
            whole = False
    return whole


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
    from ..storage.relocate import resolve
    state = resolve(state)
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
        activate_environment()
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
    if folder is None:
        return None
    if folder.has_library and not folder.moved_to:
        owner = _owner(folder.state)
        if owner is None or owner == folder.path.resolve():
            return None
        other = _layout(owner)
        if other is None or _version_key(other.version) > _version_key(version or __version__):
            return None
        return other
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
        f"This program is now inactive. To undo an upgrade, choose \"Go back\"\n"
        f"in the newer Control Panel. To use an inactive newer copy again,\n"
        f"bring the library across again from its current folder.\n"
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
        elif platform.startswith('linux'):
            path = autostart.desktop_path()
            from .linux_menu import quote_exec
            if not path.is_file() or quote_exec(str(older.path / 'ninaivu')) not in path.read_text():
                return False
        else:
            return False
        autostart.enable()
        return True
    except Exception:                                    # noqa: BLE001
        log.exception("could not move the sign-in start to this folder")
        return False


@contextlib.contextmanager
def _exclusive_libraries(source: Path, target: Path):
    """Block competing starts without holding a file inside a folder we rename."""
    from ..server import runfile
    from ..storage.relocate import resolve
    try:
        with contextlib.ExitStack() as held:
            states = sorted({source.resolve(), target.resolve()}, key=str)
            for state in states:
                held.enter_context(runfile.starting_lock(state))
                if (state / 'ninaivu-server.lock').exists():
                    with runfile.server_lock(state):
                        pass
            if resolve(source) != source:
                raise PortableError('The index moved during the update. Open Ninaivu again and retry.')
            yield
    except runfile.AlreadyRunning as exc:
        raise PortableError('Ninaivu is still running or starting. Wait until it stops and retry.') from exc


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
    older, folder = _live(older), _live(folder)
    reactivating = (folder.moved_to == older.path or _owner(folder.state) == older.path) \
        and folder.state == older.state
    if folder.has_library and not reactivating:
        raise PortableError(f"This Ninaivu already has a library, in {folder.state}.")
    if not older.has_library:
        raise PortableError(f"There is no library in {older.path}.")
    # Read now: it is in the data about to be moved.
    older_version = older.version
    if _version_key(older_version) > _version_key(version):
        raise PortableError(
            f"The library in {older.path} was last opened by Ninaivu {older_version}, "
            f"which is newer than this one ({version}). Use Ninaivu {older_version} or later.")
    shared_state = not older.state.is_relative_to(older.path)
    if not shared_state and not _same_drive(older.state, folder.state):
        # The library is renamed into folder.state, which on Linux is
        # ~/.ninaivu rather than inside this folder: a rename across drives
        # fails part-way, so the drive that matters is the one it goes to.
        if not folder.state.is_relative_to(folder.path):
            raise PortableError(
                f"The library in {older.path} is on another drive from {folder.state}, where "
                f"this Ninaivu keeps it. Start this Ninaivu with NINAIVU_STATE_DIR set to a "
                f"folder on that drive, or move the older folder to this computer's disk first.")
        raise PortableError(
            f"{older.path} is on another drive. Unpack the newer Ninaivu on the same drive "
            f"as the older one, beside it, and open it from there: the library is then moved "
            f"in a moment instead of copied.")
    stop_older(older.state)
    _stop(folder.state)

    with _exclusive_libraries(older.state, folder.state):
        skipped: list[str] = []
        moved = []
        state_to = folder.state
        try:
            for source, target in ((older.state, folder.state), (older.models, folder.models),
                                   (older.control, folder.control)):
                if shared_state and source.is_relative_to(older.state):
                    continue  # The chosen data folder stays in place, including its models/control.
                if shared_state and target.is_relative_to(folder.state):
                    target = older.state / target.relative_to(folder.state)
                if source.exists() and os.path.normcase(str(source)) != os.path.normcase(str(target)):
                    _move(source, target, skipped)
                    moved.append({"from": str(source), "to": str(target)})
        except OSError as exc:
            # What was moved goes back, so the older folder is whole again and
            # this one has no library: "try again" works, and the older Ninaivu
            # opened meanwhile finds its own library rather than an empty one
            # (audit C-04). Each move was a rename, so each goes back as one.
            undone = _undo_moves(moved)
            where = (f"Everything is back in {older.path}." if undone else
                     f"Some of it could not be put back: whatever was moved is in {folder.path}, "
                     f"and the older folder has the rest.")
            raise PortableError(
                f"The library could not be moved ({exc}). Close the older Ninaivu's Control Panel "
                f"and any window showing its folder, then try again. {where}") from exc

        if shared_state:
            try:
                _point_to(folder.state_home, older.state)
            except OSError as exc:
                _undo_moves(moved)
                raise PortableError(f"The new launcher could not be connected to the index ({exc}).") from exc
            folder = _live(folder)
        record = {"folder": str(older.path), "version": older_version, "at": time.time(),
                  "moved": moved, "to": version, "state_to": str(state_to),
                  "old_state_home": str(older.state_home), "shared_state": shared_state}
        folder.state.mkdir(parents=True, exist_ok=True)
        (folder.state / BROUGHT_RECORD).write_text(json.dumps(record, indent=2), encoding="utf-8")
        note_active(folder.state, folder.path)
        try:
            _note_moved(older, folder, version)
        except OSError as exc:
            log.warning("could not leave a note in %s: %s", older.path, exc)
        for name in (MOVED_RECORD, MOVED_NOTE):
            (folder.path / name).unlink(missing_ok=True)
        if root() == folder.path:
            activate_environment()
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
    record = None
    for place in (folder.state / BROUGHT_RECORD, folder.path / GOING_BACK):
        try:
            record = json.loads(place.read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            continue
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


def _changed_since(last: dict[str, Any] | None, record: dict[str, Any]) -> bool:
    """Whether this folder's program changed the index after the library was
    brought here: its first start after the bring made the copy before an
    update (storage/upgrade.py records when, and which copy)."""
    if not last or not last.get("backup"):
        return False
    try:
        return float(last.get("since") or 0) > float(record.get("at") or 0)
    except (TypeError, ValueError):
        return False


def _put_back_index(state: Path, bundle: Path) -> None:
    """The databases and settings in *state* replaced by those in *bundle*,
    checked first; nothing else in the folder is touched."""
    from ..storage import backup                         # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix=".ninaivu-go-back-", dir=state) as tmp:
        # Unpacked and checked in one pass over the bundle (extract_verified).
        copy = backup.extract_verified(bundle, Path(tmp))
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
    folder = _live(folder) if folder is not None else None
    record = brought_from(folder)
    if folder is None or record is None:
        raise PortableError("This library was not brought from an older Ninaivu beside this one.")
    older = _layout(Path(record["folder"]))
    if older is None:
        raise PortableError(f"The older Ninaivu is no longer in {record['folder']}.")
    resuming = (folder.path / GOING_BACK).is_file()
    # Whether the index stayed where it was (relocated, or in a home folder)
    # is what bring_here recorded, not where it is now: on Linux a library
    # brought from an older folder's data/ lives in ~/.ninaivu, outside this
    # folder, and must still be moved back. Taking it for a shared index left
    # the older folder with only a forwarding note, which a version before
    # 3.6 cannot follow, so it opened an empty library.
    # It is shared when bring_here left it in place, or when it has been
    # relocated from the console since (it is no longer where it was put).
    recorded_shared = record.get("shared_state")
    put_at = record.get("state_to")
    if isinstance(recorded_shared, bool) and isinstance(put_at, str) and put_at:
        shared_state = recorded_shared or (
            os.path.normcase(str(folder.state.resolve()))
            != os.path.normcase(str(Path(put_at).resolve())))
    else:                                # recorded by 3.5.1 or earlier
        shared_state = not folder.state.is_relative_to(folder.path)
    same_library = shared_state and older.state == folder.state and older.moved_to == folder.path
    if older.has_library and not same_library and not (resuming and not folder.has_library):
        raise PortableError(f"{older.path} has a library of its own again, so nothing was moved.")
    stop_this(folder.state)

    target_state = Path(record.get('old_state_home') or older.state)
    with _exclusive_libraries(folder.state, target_state):
        bundle = kept = None
        last = upgrade.recorded(folder.state)
        newer = bool(last) and last.get("version") != record.get("version")
        if newer:
            bundle = _copy_before(folder.state, str(last.get("version")))
        elif _changed_since(last, record):
            # The same version number with an index of another shape (a
            # build made before its version is raised): its first start here
            # took a copy, and that copy is what the older one can read.
            # Handing the index back as it is left the older version an index
            # it refuses (from 4.0, 3.x does not open one 4.0 has opened).
            made = Path(str(last.get("backup")))
            bundle = made if made.is_file() else None
            newer = True
        if newer:
            if bundle is None:
                raise PortableError(
                    "No copy of the index from before this update was found, so the older "
                    "version could not be given the index as it left it. Nothing was moved.")
            # What was done in this version since the update is about to be
            # replaced: a copy of it is kept first, as a restore keeps one, so a
            # go-back pressed months later can be undone (audit C-04). It goes
            # back with the library, under backups/before-go-back.
            kept = _copy_now(folder.state, str(last.get("version")))
            try:
                _put_back_index(folder.state, bundle)
            except (OSError, ValueError, EOFError, zlib.error, tarfile.TarError) as exc:
                # A copy damaged on its disk fails in gzip (EOFError,
                # zlib.error) or tar, not as OSError: Go back crashed on it.
                raise PortableError(f"The index could not be put back ({exc}). "
                                    f"Nothing was moved.") from exc
            (folder.state / upgrade.VERSION_FILE).unlink(missing_ok=True)

        # The record goes last, once everything is back: a move that fails (a
        # file held open on Windows, antivirus) leaves it, so "Go back" is still
        # offered and finishes the rest when pressed again. It is written beside
        # the data too, since the copy inside the data moves with it.
        (folder.path / GOING_BACK).write_text(json.dumps(record, indent=2), encoding="utf-8")
        skipped: list[str] = []
        try:
            for moved in reversed(record.get("moved") or []):
                source, target = Path(moved["to"]), Path(moved["from"])
                if shared_state:
                    original_state = Path(record.get('state_to') or folder.path / 'data')
                    if source == original_state:
                        continue
                    if source.is_relative_to(original_state):
                        source = folder.state / source.relative_to(original_state)
                if source.exists():
                    _move(source, target, skipped)
            if shared_state:
                _point_to(Path(record.get('old_state_home') or older.state_home), folder.state)
                _note_moved(folder, older, str(record.get('version') or 'the older version'))
        except OSError as exc:
            raise PortableError(
                f"The library could not all be moved back ({exc}). Close any window showing "
                f"either folder and press Go back again: it carries on from here.") from exc
        for name in (MOVED_RECORD, MOVED_NOTE):
            (older.path / name).unlink(missing_ok=True)
        # The record travelled back inside the data; the older version has no
        # use for it, and the copy kept now lies where the data went.
        state_back = next((Path(m["from"]) for m in record.get("moved") or []
                           if os.path.normcase(str(m.get("to"))) ==
                           os.path.normcase(str(folder.state))), older.state)
        if shared_state:
            state_back = folder.state
        note_active(state_back, older.path)
        (state_back / BROUGHT_RECORD).unlink(missing_ok=True)
        (folder.state / BROUGHT_RECORD).unlink(missing_ok=True)
        (folder.path / GOING_BACK).unlink(missing_ok=True)
        if kept is not None:
            with contextlib.suppress(ValueError):
                kept = state_back / kept.relative_to(folder.state)
        log.info("took the library back to %s (Ninaivu %s)", older.path, record.get("version"))
        return {"folder": str(older.path), "version": record.get("version"),
                "index_put_back": str(bundle) if bundle else None,
                "kept": str(kept) if kept else None, "skipped": skipped}


def launch(path: Path, platform: str | None = None) -> bool:
    """Open the portable Ninaivu in *path* (its Control Panel); whether it
    could be started."""
    import subprocess                                    # noqa: PLC0415
    system = _system(platform)
    if system == "darwin":
        command = ["open", "-n", str(path / "Ninaivu.app")]
    elif system == "win32":
        command = [str(path / "Ninaivu.exe")]
    else:
        command = [str(path / "ninaivu")]
    try:
        subprocess.Popen(command, cwd=str(path), start_new_session=True,  # noqa: S603
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("could not open the Ninaivu in %s: %s", path, exc)
        return False
    return True


#: Under the state folder: the copy of the index kept before a go-back.
BEFORE_GO_BACK = Path("backups") / "before-go-back"


def _copy_now(state: Path, version: str) -> Path:
    """A verified copy of *state* as *version* left it, before a go-back
    replaces it. Raises :class:`PortableError` when none could be made:
    going back without one would lose the newer version's work for good."""
    from ..storage import backup                         # noqa: PLC0415
    try:
        made = backup.snapshot(state, state / BEFORE_GO_BACK,
                               note={"before_go_back": {"from": version, "at": time.time()}})
    except Exception as exc:                             # noqa: BLE001
        raise PortableError(f"A copy of the library as it is now could not be made ({exc}), "
                            f"so nothing was changed.") from exc
    if made is None:
        raise PortableError("A copy of the library as it is now could not be made, "
                            "so nothing was changed.")
    return Path(made)


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
