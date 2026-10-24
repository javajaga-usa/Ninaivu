"""The Primary updates the Secondary: a newer portable Ninaivu sent across.

The Secondary is the Linux computer in the cupboard, often without a screen,
and until 3.0 updating it meant somebody at its keyboard (or over ssh)
unpacking the newer download beside the old folder and bringing the library
across with ``./ninaivu --bring``. Now the Primary's Mugil page does it: the
person gives it the Linux portable download of the version the Primary runs
(Ninaivu never asks the internet for one), and the Primary sends it over the
pairing, signed like every original it sends (mugil/pairing.py).

On the Secondary (:class:`Updater`) the download is

1. kept only if it arrived as signed, byte for byte (its SHA-256);
2. looked inside before anything is unpacked: one portable folder for Linux
   on this machine's processor, nothing outside it, a newer Ninaivu than the
   one running (:func:`look_inside`);
3. unpacked beside the folder in use, on the same drive, as a person would;
4. switched to only when the Secondary is quiet: no original kept for a
   couple of minutes and no off-site copy being sent. The switch is done by
   a small helper outside the server (:func:`finish`), started where stopping
   the server does not stop it too (``systemd-run`` under systemd). It stops
   Ninaivu, brings the library across (desktop/portable.py
   :func:`~ninaivu.desktop.portable.bring_here`, moved on the same drive),
   points the service at the newer folder and starts it;
5. watched: when the newer version is serving, that is the end. When it ends
   twice before it ever served, the helper gives the library back to the
   older folder as the older version left it
   (:func:`~ninaivu.desktop.portable.go_back`), points the service back and
   starts the older Ninaivu: the Control Panel's own rule for a portable
   update that cannot start (desktop/control.py ``failed_start``), for a
   computer with no Control Panel open.

What happened is written to ``mugil-update.json`` in the state folder, which
travels with the library, and the Primary's page reads it from there. The
photographs are never read or written by any of this.

    python -m ninaivu.mugil.update finish --old FOLDER --new FOLDER --to VERSION
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform as platform_mod
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable

from .. import __version__

log = logging.getLogger(__name__)

#: In the state folder: what the last update did, for both computers' pages.
RECORD = "mugil-update.json"
#: The largest download taken. The Linux portable folder is well under it.
PACKAGE_MAX = 6 * 1024 ** 3
#: How long nothing may have arrived before the Secondary counts as quiet.
QUIET = 120.0
#: How often the Secondary looks again while it is not.
LOOK_EVERY = 30.0
#: Starts of the newer version that ended before it was serving, after which
#: the library goes back (desktop/control.py ``GIVE_UP_AFTER``).
GIVE_UP_AFTER = 2
#: How long the helper watches a newer version that neither serves nor ends.
WATCH_LONGEST = 6 * 60 * 60.0
#: The systemd user service ``ninaivu mugil setup`` writes.
UNIT = "ninaivu.service"
#: The name a Linux portable download unpacks to.
_TOP = re.compile(r"^Ninaivu-(\d+(?:\.\d+){1,3}[A-Za-z0-9.+-]*)-linux-(x64|arm64)-portable$")
_VERSION_LINE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
_INIT = re.compile(r"^app/python/lib/python3\.\d+/site-packages/ninaivu/__init__\.py$")
#: Stages after which nothing more happens until the next download.
FINISHED = ("done", "went_back", "failed", "refused")


class UpdateRefused(ValueError):
    """The download was not taken; the text says why, for the person."""


def arch(machine: str | None = None) -> str:
    """This processor as the downloads name it: x64 or arm64."""
    machine = (machine or platform_mod.machine() or "").lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x64"
    if machine in ("aarch64", "arm64", "armv8l"):
        return "arm64"
    return machine


def _version_key(version: str | None) -> tuple[int, ...]:
    from ..desktop.portable import _version_key as key  # noqa: PLC0415
    return key(version)


# -- the record ----------------------------------------------------------------

def record(state_dir: str | Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(state_dir) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def note(state_dir: str | Path, stage: str, **fields: Any) -> dict[str, Any]:
    """Write down where the update is; what was written before is kept."""
    state = Path(state_dir)
    data = {**(record(state) or {}), **fields, "stage": stage, "at": time.time()}
    if stage in FINISHED:
        data["finished_at"] = data["at"]
    try:
        state.mkdir(parents=True, exist_ok=True)
        tmp = state / f".{RECORD}.tmp"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, state / RECORD)
    except OSError as exc:
        log.warning("could not write down the update's progress in %s: %s", state, exc)
    return data


def in_progress(state_dir: str | Path) -> bool:
    data = record(state_dir)
    return bool(data) and data.get("stage") not in FINISHED


# -- the download --------------------------------------------------------------

def _inside(top: str, name: str) -> str | None:
    """*name* relative to the folder *top*, or None when it is not in it."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return None
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts or parts[0] != top:
        return None
    return "/".join(parts[1:])


def look_inside(package: str | Path) -> dict[str, str]:
    """What the download is: ``{"top", "version", "arch"}``, or
    :class:`UpdateRefused` when it is not one portable Ninaivu for Linux
    with nothing outside its own folder."""
    try:
        with tarfile.open(package, "r:gz") as tar:
            members = tar.getmembers()
            tops = {PurePosixPath(m.name).parts[0] for m in members if PurePosixPath(m.name).parts}
            if len(tops) != 1:
                raise UpdateRefused("That is not a portable Ninaivu for Linux: it does not "
                                    "unpack to one folder.")
            top = tops.pop()
            found = _TOP.match(top)
            if not found:
                raise UpdateRefused(f"{top} is not a portable Ninaivu for Linux. Send the "
                                    f"Ninaivu-…-linux-…-portable.tar.gz download.")
            named, processor = found.group(1), found.group(2)
            seen: dict[str, tarfile.TarInfo] = {}
            for member in members:
                rel = _inside(top, member.name)
                if rel is None:
                    raise UpdateRefused("That download holds files outside its own folder.")
                if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                    raise UpdateRefused("That download holds something that is not a file.")
                if member.issym() or member.islnk():
                    if member.islnk():
                        leads = _inside(top, member.linkname)
                    elif PurePosixPath(member.linkname).is_absolute():
                        leads = None
                    else:
                        leads = _inside(top, os.path.normpath(
                            str(PurePosixPath(top, rel).parent / member.linkname)))
                    if leads is None:
                        raise UpdateRefused("That download holds a link that leads outside it.")
                seen[rel] = member
            launcher = seen.get("ninaivu")
            if launcher is None or not launcher.isfile() or "app/python/bin/python3" not in seen:
                raise UpdateRefused("That download has no Ninaivu in it to start.")
            init = next((m for rel, m in seen.items() if _INIT.match(rel) and m.isfile()), None)
            handle = tar.extractfile(init) if init is not None else None
            text = handle.read(64 * 1024).decode("utf-8", "replace") if handle else ""
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise UpdateRefused(f"That download could not be read ({exc}). Send it again.") from exc
    inside = _VERSION_LINE.search(text)
    if not inside:
        raise UpdateRefused("That download has no Ninaivu in it to start.")
    if inside.group(1) != named:
        raise UpdateRefused(f"That download says it is Ninaivu {named} but holds "
                            f"Ninaivu {inside.group(1)}.")
    return {"top": top, "version": named, "arch": processor}


def check(package: str | Path, *, mine: str = __version__,
          machine: str | None = None) -> dict[str, str]:
    """:func:`look_inside`, and whether it is for this computer and newer."""
    info = look_inside(package)
    here = arch(machine)
    if info["arch"] != here:
        raise UpdateRefused(f"That download is for {info['arch']} processors; this computer "
                            f"has {here}. Send the {here} download.")
    if _version_key(info["version"]) <= _version_key(mine):
        raise UpdateRefused(f"That is Ninaivu {info['version']}; this computer already runs "
                            f"Ninaivu {mine}.")
    return info


def unpack(package: str | Path, beside: Path, info: dict[str, str]) -> Path:
    """The download unpacked into *beside*, under its own name, or under that
    name with a number when a folder of that name is there already."""
    staging = Path(tempfile.mkdtemp(prefix=".ninaivu-update-", dir=beside))
    try:
        with tarfile.open(package, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        made = staging / info["top"]
        target = beside / info["top"]
        number = 2
        while target.exists():
            target = beside / f"{info['top']} ({number})"
            number += 1
        made.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def digest_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def take_in(stream: BinaryIO, size: int, into: Path) -> str:
    """Write *size* bytes of *stream* to *into*; their SHA-256. Raises
    :class:`UpdateRefused`, leaving nothing behind, when fewer arrive."""
    h = hashlib.sha256()
    got = 0
    partial = into.with_name(into.name + ".part")
    try:
        with open(partial, "wb") as out:
            while got < size:
                chunk = stream.read(min(1024 * 1024, size - got))
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
                got += len(chunk)
        if got != size:
            raise UpdateRefused("The download stopped before the end. Send it again.")
        os.replace(partial, into)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return h.hexdigest()


# -- on the Secondary ----------------------------------------------------------

def under_systemd(environ: dict[str, str] | None = None, home: Path | None = None) -> bool:
    """Whether this server was started by the service ``ninaivu mugil setup``
    wrote (which starts it again when it stops)."""
    from ..cli.mugil import unit_path                    # noqa: PLC0415
    environ = os.environ if environ is None else environ
    return bool(environ.get("INVOCATION_ID")) and unit_path(home).is_file()


def helper_command(old: Path, new: Path, to: str, *, systemd: bool,
                   server_args: list[str] | None = None,
                   python: str | None = None) -> list[str]:
    """The helper that finishes the update, run by the older folder's own
    Python: the one known to start here, should the newer one not."""
    command = [python or sys.executable, "-m", "ninaivu.mugil.update", "finish",
               "--old", str(old), "--new", str(new), "--to", to]
    if systemd:
        command.append("--systemd")
    if server_args:
        command += ["--args", json.dumps(server_args)]
    return command


def start_helper(command: list[str], *, systemd: bool, logs: Path,
                 runner=subprocess.run, popen=subprocess.Popen) -> None:
    """Start the helper where stopping this server does not stop it too.

    A process started by a systemd service is in its group and is stopped
    with it, however it was started; ``systemd-run`` starts it as a unit of
    its own. Outside systemd, a session of its own is enough.
    """
    env = {"PYTHONNOUSERSITE": "1", "NINAIVU_NO_DIALOG": "1"}
    if systemd:
        unit = f"ninaivu-update-{int(time.time())}"
        done = runner(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}",
                       *[f"--setenv={k}={v}" for k, v in env.items()], *command],
                      capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise UpdateRefused("The update could not be started here: "
                                + ((done.stderr or done.stdout or "").strip() or "systemd-run failed"))
        return
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "mugil-update.log", "ab") as out:
        popen(command, start_new_session=True, stdout=out, stderr=out,  # noqa: S603
              stdin=subprocess.DEVNULL, env={**os.environ, **env})


class Updater:
    """On the Secondary: takes a download from the Primary and sees it through.

    *quiet* says whether the Secondary is between originals (the server
    passes one that reads the originals ledger and the off-site copy);
    *folder* is the portable folder this Ninaivu runs from.
    """

    def __init__(self, state_dir: str | Path, *, folder=None,
                 quiet: Callable[[], bool] = lambda: True,
                 systemd: bool | None = None, server_args: list[str] | None = None,
                 spawn: Callable[..., None] = start_helper,
                 sleep: Callable[[float], None] = time.sleep,
                 version: str = __version__, machine: str | None = None) -> None:
        from ..desktop import portable                   # noqa: PLC0415
        self.state = Path(state_dir)
        self.folder = folder if folder is not None else portable.this_folder()
        self.quiet = quiet
        self.systemd = under_systemd() if systemd is None else systemd
        self.server_args = list(sys.argv[1:] if server_args is None else server_args)
        self.spawn = spawn
        self.sleep = sleep
        self.version = version
        self.machine = machine
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        return {**(record(self.state) or {"stage": None}), "version": self.version}

    def refusal(self) -> str | None:
        """Why no download can be taken now, or None."""
        if self.folder is None:
            return ("This computer does not run the portable Ninaivu, so it cannot be updated "
                    "from the Primary. Update it by hand.")
        if not sys.platform.startswith("linux"):
            return "Only a Secondary running Linux can be updated from the Primary."
        if self.running or self.switching():
            return "An update is already under way on this computer."
        return None

    def switching(self) -> bool:
        """Whether a helper took the update over and has not finished."""
        data = record(self.state)
        return bool(data) and data.get("stage") in ("switching", "starting")

    def receive(self, stream: BinaryIO, size: int, digest: str) -> dict[str, Any]:
        """The download, *size* bytes of *stream*, kept if its SHA-256 is
        *digest*; the rest is done in the background."""
        with self._lock:
            problem = self.refusal()
            if problem:
                raise UpdateRefused(problem)
            if not 0 < size <= PACKAGE_MAX:
                raise UpdateRefused("That download is not the size of a portable Ninaivu.")
            beside = self.folder.path.parent
            try:
                free = shutil.disk_usage(beside).free
            except OSError:
                free = 0
            if free < size * 4:
                raise UpdateRefused("There is not room beside this Ninaivu's folder to unpack "
                                    "the newer one.")
            package = beside / ".ninaivu-update.tar.gz"
            note(self.state, "receiving", started_at=time.time(), to=None,
                 **{"from": self.version}, message="", folder=None)
            try:
                got = take_in(stream, size, package)
            except (OSError, UpdateRefused) as exc:
                note(self.state, "failed", message=str(exc))
                raise UpdateRefused(str(exc)) from exc
            if got != digest:
                package.unlink(missing_ok=True)
                note(self.state, "failed", message="The download was changed on the way.")
                raise UpdateRefused("The download was changed on the way. Send it again.")
            try:
                info = check(package, mine=self.version, machine=self.machine)
            except UpdateRefused as exc:
                package.unlink(missing_ok=True)
                note(self.state, "refused", message=str(exc))
                raise
            note(self.state, "unpacking", to=info["version"])
            self._stop.clear()
            self._thread = threading.Thread(target=self._finish, args=(package, info),
                                            name="mugil-update", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _finish(self, package: Path, info: dict[str, str]) -> None:
        try:
            try:
                new = unpack(package, self.folder.path.parent, info)
            finally:
                package.unlink(missing_ok=True)
            note(self.state, "waiting", folder=str(new),
                 message="Waiting for a quiet moment: no originals arriving.")
            while not self._stop.is_set() and not self.quiet():
                self.sleep(LOOK_EVERY)
            if self._stop.is_set():
                note(self.state, "failed", message="Ninaivu was stopped before the update "
                     f"could be finished. The newer folder is in {new}.")
                return
            note(self.state, "switching", message="")
            self.spawn(helper_command(self.folder.path, new, info["version"],
                                      systemd=self.systemd, server_args=self.server_args),
                       systemd=self.systemd, logs=self.folder.path / "logs")
            log.info("handed the update to Ninaivu %s over to the helper", info["version"])
        except UpdateRefused as exc:
            note(self.state, "failed", message=str(exc))
        except Exception as exc:                         # noqa: BLE001 - written down for the page
            log.exception("the update could not be finished")
            note(self.state, "failed", message=f"The update could not be finished ({exc}).")


# -- the helper ----------------------------------------------------------------

def folder_environment(folder) -> dict[str, str]:
    """What the portable launcher sets for *folder*, as a service carries it."""
    return {"NINAIVU_STATE_DIR": str(folder.state), "NINAIVU_AI_MODELS_DIR": str(folder.models),
            "NINAIVU_LOG_DIR": str(folder.path / "logs"),
            "NINAIVU_HOME": str(folder.state / "control"), "NINAIVU_PORTABLE": "1",
            "NINAIVU_PORTABLE_ROOT": str(folder.path),
            "NINAIVU_PROGRAM_DIR": str(folder.path / "app")}


def unit_for(text: str, old, new) -> str:
    """The service *text* starting *new* instead of *old*: the older folder's
    path changed wherever it is, so anything else in it stays; written
    afresh when it does not name the older folder at all."""
    from ..cli import mugil as mugil_cli                 # noqa: PLC0415
    before, after = str(old.path), str(new.path)
    if before and before in text:
        return text.replace(before, after)
    env = folder_environment(new)
    return mugil_cli.unit_text(mugil_cli.start_command(env), env)


class Service:
    """Ninaivu as the systemd user service runs it."""

    def __init__(self, runner=subprocess.run, home: Path | None = None) -> None:
        from ..cli.mugil import unit_path                # noqa: PLC0415
        self.runner = runner
        self.path = unit_path(home)
        self.was_down = False
        self.ended = 0

    def _ctl(self, *args: str) -> str:
        done = self.runner(["systemctl", "--user", *args], capture_output=True, text=True,
                           check=False)
        if done.returncode != 0 and args[0] not in ("show",):
            log.warning("systemctl --user %s: %s", shlex.join(args),
                        (done.stderr or done.stdout or "").strip())
        return done.stdout or ""

    def unit(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def write(self, text: str) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)
        self._ctl("daemon-reload")

    def stop(self, folder) -> None:
        self._ctl("stop", UNIT)

    def start(self, folder) -> None:
        self.was_down, self.ended = False, 0
        self._ctl("reset-failed", UNIT)
        self._ctl("start", UNIT)

    def failures(self) -> int:
        """Times the service ended since :meth:`start` (systemd starts it again
        by itself, ten seconds later, each time)."""
        shown = self._ctl("show", "-p", "ActiveState", "-p", "SubState", UNIT)
        values = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
        down = values.get("ActiveState") in ("failed", "inactive") or \
            values.get("SubState") == "auto-restart"
        if down and not self.was_down:
            self.ended += 1
        self.was_down = down
        return self.ended


class Process:
    """Ninaivu started by the helper itself, where there is no systemd."""

    def __init__(self, server_args: list[str] | None = None, popen=subprocess.Popen) -> None:
        self.args = list(server_args or [])
        self.popen = popen
        self.child = None
        self.ended = 0

    def stop(self, folder) -> None:
        from ..desktop import portable                   # noqa: PLC0415
        portable._stop(folder.state)                     # noqa: SLF001

    def start(self, folder) -> None:
        self.ended = 0
        self._launch(folder)

    def _launch(self, folder) -> None:
        self.folder = folder
        logs = folder.path / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "server.log", "ab") as out:
            self.child = self.popen([str(folder.path / "ninaivu"), "--server", *self.args],
                                    cwd=str(folder.path), start_new_session=True,
                                    stdout=out, stderr=out, stdin=subprocess.DEVNULL)

    def failures(self) -> int:
        if self.child is not None and self.child.poll() is not None:
            self.ended += 1
            self.child = None
            if self.ended < GIVE_UP_AFTER:
                self._launch(self.folder)
        return self.ended


def watch(state: Path, version: str, service, *, patience: float = WATCH_LONGEST,
          clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> str:
    """``served``, ``failed`` (ended :data:`GIVE_UP_AFTER` times before it
    served) or ``slow`` (neither, for *patience* seconds)."""
    from ..storage import upgrade                        # noqa: PLC0415
    deadline = clock() + patience
    while clock() < deadline:
        if upgrade.has_served(state, version):
            return "served"
        if service.failures() >= GIVE_UP_AFTER:
            return "served" if upgrade.has_served(state, version) else "failed"
        sleep(1.0)
    return "slow"


def finish(old_path: Path, new_path: Path, to: str, *, service=None,
           sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.monotonic,
           patience: float = WATCH_LONGEST) -> str:
    """Switch from the older folder to the newer one, and back if the newer
    one cannot start. Returns how it ended: ``done``, ``went_back``,
    ``failed`` (nothing changed but the newer folder, and the older Ninaivu
    started again) or ``slow``."""
    from ..desktop import portable                       # noqa: PLC0415

    old, new = portable._layout(old_path), portable._layout(new_path)  # noqa: SLF001
    if old is None or new is None:
        raise UpdateRefused(f"{old_path if old is None else new_path} is not a portable Ninaivu.")
    service = service or Process()
    systemd = isinstance(service, Service)
    before = service.unit() if systemd else None
    came_from = old.version

    service.stop(old)
    try:
        brought = portable.bring_here(old, new, version=to)
    except portable.PortableError as exc:
        log.error("the library could not be brought across: %s", exc)
        note(old.state, "failed", message=str(exc))
        service.start(old)
        return "failed"
    new = portable._layout(new_path) or new                # noqa: SLF001
    note(new.state, "starting", message="", skipped=brought.get("skipped") or [])
    if systemd:
        service.write(unit_for(before, old, new))
    service.start(new)
    log.info("started Ninaivu %s from %s", to, new.path)

    ended = watch(new.state, to, service, patience=patience, clock=clock, sleep=sleep)
    if ended == "served":
        note(new.state, "done", message="")
        log.info("Ninaivu %s is serving", to)
        return "done"
    if ended == "slow":
        note(new.state, "starting", message=f"Ninaivu {to} has not finished starting yet.")
        return "slow"

    log.error("Ninaivu %s ended %d times before serving; going back to %s", to,
              GIVE_UP_AFTER, came_from)
    service.stop(new)
    try:
        back = portable.go_back(new, version=to)
    except portable.PortableError as exc:
        log.error("could not go back: %s", exc)
        note(new.state, "failed", message=f"Ninaivu {to} could not start here, and the library "
             f"could not be given back by itself ({exc}). Run ./ninaivu --go-back in {new.path}.")
        return "failed"
    if systemd:
        service.write(before)
    service.start(old)
    note(old.state, "went_back", message=(
        f"Ninaivu {to} could not start on this computer, twice, so the library went back to "
        f"Ninaivu {back.get('version') or came_from} by itself, as it was before. The logs of "
        f"the failed starts are in {new.path / 'logs'}."))
    return "went_back"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ninaivu.mugil.update",
                                     description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="what", required=True)
    one = sub.add_parser("finish", help="switch to the newer folder, and back if it cannot start")
    one.add_argument("--old", required=True)
    one.add_argument("--new", required=True)
    one.add_argument("--to", required=True)
    one.add_argument("--systemd", action="store_true")
    one.add_argument("--args", default="[]")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        server_args = [str(a) for a in json.loads(args.args)]
    except (ValueError, TypeError):
        server_args = []
    service = Service() if args.systemd else Process(server_args)
    try:
        ended = finish(Path(args.old), Path(args.new), args.to, service=service)
    except UpdateRefused as exc:
        log.error("%s", exc)
        return 1
    return 0 if ended in ("done", "slow") else 1


# -- on the Primary ------------------------------------------------------------

class _Counting:
    """A file read by http.client, counting what was sent."""

    def __init__(self, handle: BinaryIO, sender: "Sender") -> None:
        self.handle, self.sender = handle, sender

    def read(self, n: int = -1) -> bytes:
        chunk = self.handle.read(n)
        self.sender.sent += len(chunk)
        return chunk


class Sender:
    """On the Primary: the download the person gave, sent to the Secondary."""

    def __init__(self, state_dir: str | Path, partner: Callable[[], Any], *,
                 version: str = __version__) -> None:
        self.folder = Path(state_dir) / "mugil-update"
        self.partner = partner
        self.version = version
        self.sent = 0
        self.size = 0
        self.stage: str | None = None
        self.message = ""
        self.to: str | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        return {"stage": self.stage, "message": self.message, "to": self.to,
                "sent": self.sent, "size": self.size, "running": self.running}

    def take(self, stream: BinaryIO, size: int) -> dict[str, Any]:
        """Keep the download the person gave, look inside it, and send it."""
        with self._lock:
            if self.running:
                raise UpdateRefused("A download is already being sent to the Secondary.")
            if not 0 < size <= PACKAGE_MAX:
                raise UpdateRefused("That file is not the size of a portable Ninaivu.")
            self.folder.mkdir(parents=True, exist_ok=True)
            package = self.folder / "package.tar.gz"
            try:
                digest = take_in(stream, size, package)
                info = look_inside(package)
            except (OSError, UpdateRefused) as exc:
                package.unlink(missing_ok=True)
                raise UpdateRefused(str(exc)) from exc
            if info["version"] != self.version:
                package.unlink(missing_ok=True)
                raise UpdateRefused(f"That is Ninaivu {info['version']}. Send the download of "
                                    f"Ninaivu {self.version}, the version this computer runs.")
            self.stage, self.message, self.to = "sending", "", info["version"]
            self.sent, self.size = 0, size
            self._thread = threading.Thread(target=self._send, args=(package, digest, size),
                                            name="mugil-update-send", daemon=True)
            self._thread.start()
            return self.status()

    def _send(self, package: Path, digest: str, size: int) -> None:
        try:
            with open(package, "rb") as handle:
                self.partner().request(
                    "PUT", "/api/mugil/update", body=_Counting(handle, self), length=size,
                    digest=digest, headers={"Content-Type": "application/octet-stream"})
            self.stage = "sent"
        except Exception as exc:                         # noqa: BLE001 - said on the page
            log.warning("the update could not be sent to the Secondary: %s", exc)
            self.stage, self.message = "failed", str(exc)
        finally:
            package.unlink(missing_ok=True)


__all__ = ["RECORD", "UpdateRefused", "Updater", "Sender", "Service", "Process", "check",
           "look_inside", "unpack", "finish", "watch", "record", "note", "arch",
           "helper_command", "start_helper", "unit_for", "folder_environment"]


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(main())
