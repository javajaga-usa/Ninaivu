"""Where Ninaivu says it is running, so something else can ask it to stop.

Ctrl+C works, and for somebody watching the window it is the right way to stop
Ninaivu. It is no use to everything else: a shortcut on the desktop, a scheduled
task at three in the morning, a batch file that wants to restart the server
after copying new photographs in. Those need a way to say *stop* to a process
they did not start and cannot see.

Killing it is not that way. Ninaivu is usually in the middle of something — a
scan a third of the way through a drive, an upload with a resumable session
open, an archive run copying a file that is not whole yet. None of that is
*lost* when the process dies, because all of it was built to survive exactly
that, but each one costs work to pick up again, and a killed process leaves
half-written temporary files behind for the next run to clean up.

So Ninaivu writes down where it is: one small file in the state directory
holding the process id, the two ports and a token. Anything that can read that
file can ask the server to stop properly — finish the file it is on, write down
where it got to, and close.

The token is the whole of the security, and it is enough. The file is written
readable only by the account Ninaivu runs as, and the request must come from
the machine itself. Being able to read a file in somebody's own state
directory already means being able to stop their programs.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = ["RUN_FILE", "write", "read", "clear", "is_running", "new_token", "in_container"]

#: The name of the file, inside the state directory.
RUN_FILE = "ninaivu.run"


def path_for(state_dir: str | Path) -> Path:
    return Path(state_dir) / RUN_FILE


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write(state_dir: str | Path, *, port: int, admin_port: int,
          host: str = "", token: str | None = None,
          version: str = "", scheme: str = "http") -> str:
    """Record this process, and return the token that may stop it.

    Written to a temporary name and renamed into place, so a reader never sees
    half a file, and locked down to this account where the platform allows it.
    """
    target = path_for(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = token or new_token()
    payload: dict[str, Any] = {
        "pid": os.getpid(),
        "port": int(port),
        "admin_port": int(admin_port),
        "host": host,
        "scheme": scheme,
        "token": token,
        "version": version,
        "started_at": time.time(),
    }
    partial = target.with_name(target.name + ".part")
    # Created owner-only rather than written and then narrowed: between a
    # write_text and the chmod after it, the token sat in a file anyone on the
    # machine could read, which is the one thing it must not. A leftover .part
    # is removed first, since O_CREAT keeps the permissions of a file that is
    # already there. Windows ignores the mode; the file is in the account's own
    # profile there.
    try:
        partial.unlink()
    except OSError:
        pass
    fd = os.open(partial, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2))
    os.replace(partial, target)
    return token


def read(state_dir: str | Path) -> dict[str, Any] | None:
    """What the file says, or ``None`` if there is not a usable one."""
    try:
        data = json.loads(path_for(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("pid"):
        return None
    return data


def clear(state_dir: str | Path, *, pid: int | None = None) -> None:
    """Remove the file. Never raises — this runs while Ninaivu is shutting down."""
    try:
        record = read(state_dir)
        if record and record.get('pid') != (os.getpid() if pid is None else pid):
            return
        path_for(state_dir).unlink(missing_ok=True)
    except OSError:
        pass


class AlreadyRunning(RuntimeError):
    """The state directory belongs to an active server."""


@contextmanager
def server_lock(state_dir):
    """OS-owned lock, automatically released even if the server crashes."""
    path = Path(state_dir) / 'ninaivu-server.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    locked = False
    try:
        if path.stat().st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise AlreadyRunning('Another Ninaivu server is already using this state directory.') from exc
        yield
    finally:
        if locked:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()



def in_container(environ=None, exists=os.path.exists) -> bool:
    """Whether this process runs inside a container (Docker, Podman).

    Two things differ there. The only way in is the published ports, so a
    server that binds 127.0.0.1 is one nobody can reach — and nobody can then
    reach the switch that would put it back. And the container's supervisor is
    what starts the server, so a restart must leave the starting to it.

    ``NINAIVU_IN_CONTAINER`` says so outright, either way; otherwise the marker
    file each runtime leaves at the root of the filesystem decides.
    """
    environ = os.environ if environ is None else environ
    told = str(environ.get("NINAIVU_IN_CONTAINER", "")).strip().lower()
    if told in {"1", "true", "yes", "on"}:
        return True
    if told in {"0", "false", "no", "off"}:
        return False
    return bool(exists("/.dockerenv") or exists("/run/.containerenv"))


def is_running(pid: int) -> bool:
    """Whether a process with this id exists.

    A run file left behind by a machine that lost power names a process that
    is not there, and treating that as "Ninaivu is running" is how a stop script
    reports failure for a server that stopped days ago.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":                      # pragma: no cover - Windows
        import subprocess

        try:
            found = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return True          # cannot tell: assume it is, and let the
        return str(pid) in (found.stdout or "")      # caller try to reach it
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # somebody else's, but it is there
    except OSError:
        return True
    return True
