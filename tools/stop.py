#!/usr/bin/env python3
"""Ask a running Ninaivu to stop, properly.

``stop.bat`` and ``stop.sh`` are two lines each; this is the part that does
the work, so both platforms behave the same and there is one place to read.

The order matters, and it is the whole of the design:

1. **Ask.** Read the run file Ninaivu wrote in its state directory, and post
   its token to the shutdown endpoint on the console port. Ninaivu then stops
   the way it would for Ctrl+C — the scan finishes the file it is on and
   checkpoints, the upload pauses keeping its resumable session, the archive
   run records where it got to — and closes.
2. **Wait.** Watch the process until it is gone.
3. **Only then, end it.** If asking did not work, or the server never went
   away, send it a normal terminate. Force is the last resort and it is
   announced, because forcing a process holding a database open is exactly
   what everything above exists to avoid.

Exit codes: 0 stopped (or was not running), 1 could not stop it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

#: How long to wait for an asked-for stop before deciding it is not coming.
POLITE_WAIT = 45.0
#: …and how long after a terminate before forcing it.
TERMINATE_WAIT = 15.0


def say(message: str) -> None:
    print(f"  • {message}")


def warn(message: str) -> None:
    print(f"  ! {message}")


def _load_ask_to_stop():
    """``ninaivu/server/stop.py``, read by path: importing it through the
    package would import Flask, and stopping must work without it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ninaivu_stop", HERE / "ninaivu" / "server" / "stop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ask_to_stop


ask_to_stop = _load_ask_to_stop()


def _runfile():
    """The run-file reader, with or without the rest of Ninaivu installed.

    Stopping a server should not require the web framework it serves with.
    Importing ``ninaivu.server.runfile`` the ordinary way executes the
    ``ninaivu`` package first, which imports Flask — so running stop.bat with
    the system Python instead of the virtual environment failed on an import
    that has nothing to do with stopping anything.

    ``runfile`` itself depends on nothing outside the standard library, so
    when the package will not import it is loaded straight from its file.
    """
    try:
        from ninaivu.server import runfile  # noqa: PLC0415
        return runfile
    except Exception:  # noqa: BLE001 - missing Flask, half-installed venv
        import importlib.util  # noqa: PLC0415

        source = (Path(__file__).resolve().parent.parent
                  / "ninaivu" / "server" / "runfile.py")
        spec = importlib.util.spec_from_file_location("_ninaivu_runfile", source)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _state_dir(explicit: str | None) -> Path:
    """Where Ninaivu keeps its state, without needing Ninaivu to say so."""
    if explicit:
        return Path(explicit)
    try:
        from ninaivu.server.config import Config  # noqa: PLC0415
        return Config.load().state_dir
    except Exception:  # noqa: BLE001 - same fallback as above
        env = os.environ.get("NINAIVU_STATE_DIR")
        if env:
            return Path(env).expanduser().resolve()
        base = os.environ.get("XDG_DATA_HOME")
        if base:
            return Path(base).expanduser().resolve() / "ninaivu"
        return Path.home() / ".ninaivu"


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if port can be bound right now."""
    if not port or port <= 0:
        return True
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def lock_is_free(state_dir: Path) -> bool:
    """True if server_lock can be acquired."""
    runfile = _runfile()
    try:
        with runfile.server_lock(state_dir):
            return True
    except Exception:
        return False


def wait_for_shutdown(pid: int, ports: list[int], state_dir: Path,
                      timeout: float = 15.0) -> bool:
    """Wait for process to exit, ports to be free, and server lock to be released."""
    deadline = time.time() + timeout
    runfile = _runfile()
    while time.time() < deadline:
        running = runfile.is_running(pid) if pid > 0 else False
        ports_free = all(port_is_free(p) for p in ports if p > 0)
        lock_free = lock_is_free(state_dir)
        if not running and ports_free and lock_free:
            return True
        time.sleep(0.25)
    return (
        (not (runfile.is_running(pid) if pid > 0 else False))
        and all(port_is_free(p) for p in ports if p > 0)
        and lock_is_free(state_dir)
    )


def gone(pid: int, deadline: float) -> bool:
    runfile = _runfile()

    while time.time() < deadline:
        if not runfile.is_running(pid):
            return True
        time.sleep(0.3)
    return not runfile.is_running(pid)


def end_it(pid: int, force: bool = False) -> None:
    if sys.platform == "win32":
        command = ["taskkill", "/PID", str(pid)]
        if force:
            command.append("/F")
        subprocess.run(command, capture_output=True, check=False)
        return
    import signal

    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop a running Ninaivu.")
    parser.add_argument("--state-dir", default=None,
                        help="Where Ninaivu keeps its state (it finds this itself)")
    parser.add_argument("--wait", action="store_true",
                        help="Also wait for the ports and lock to be free again")
    parser.add_argument("--force", action="store_true",
                        help="Skip asking nicely and end the process")
    parser.add_argument("--https", action="store_true",
                        help="Communicate with the shutdown endpoint over HTTPS")
    parser.add_argument("--cert", default=None, metavar="FILE",
                        help="Certificate file (implies --https)")
    parser.add_argument("--key", default=None, metavar="FILE",
                        help="Private key for --cert")
    parser.add_argument("--scheme", choices=["http", "https"], default=None,
                        help="Force HTTP or HTTPS scheme for the shutdown request")
    parser.add_argument("--port", type=int, default=None,
                        help="Family app port")
    parser.add_argument("--admin-port", type=int, default=None,
                        help="Admin console port")
    parser.add_argument("--token", default=None,
                        help="Shutdown token")
    args, _ = parser.parse_known_args()

    runfile = _runfile()
    state_dir = _state_dir(args.state_dir)
    record = runfile.read(state_dir)
    if record is None:
        if args.token and (args.admin_port or args.port):
            scheme = "https" if (args.https or args.cert) else (args.scheme or "http")
            port = args.admin_port or args.port or 3000
            if ask_to_stop(port, args.token, scheme=scheme):
                say("Stopped.")
                return 0
        say("Ninaivu does not appear to be running.")
        return 0

    pid = int(record["pid"])
    if not runfile.is_running(pid):
        say("Ninaivu is not running — clearing the file it left behind.")
        runfile.clear(state_dir, pid=pid)
        return 0

    ports_to_watch = [int(p) for p in (record.get("port"), record.get("admin_port"), args.port, args.admin_port) if p]
    scheme = "https" if (args.https or args.cert) else (args.scheme or record.get("scheme", "http"))

    if not args.force:
        say(f"Asking Ninaivu (pid {pid}) to stop…")
        admin_port = int(args.admin_port or record.get("admin_port") or 0)
        token = args.token or record.get("token", "")
        if ask_to_stop(admin_port, token, scheme=scheme):
            if gone(pid, time.time() + POLITE_WAIT):
                say("Stopped.")
                runfile.clear(state_dir, pid=pid)
                if args.wait:
                    wait_for_shutdown(pid, ports_to_watch, state_dir)
                return 0
            warn("It accepted, but is still going. Something it was doing is "
                 "taking longer than expected.")
        else:
            warn("It did not answer. Falling back to ending the process.")

    say("Ending the process.")
    end_it(pid)
    if gone(pid, time.time() + TERMINATE_WAIT):
        say("Stopped.")
        runfile.clear(state_dir, pid=pid)
        if args.wait:
            wait_for_shutdown(pid, ports_to_watch, state_dir)
        return 0

    warn("Forcing it. If a scan or an archive run was going, it will pick up "
         "where it left off next time.")
    end_it(pid, force=True)
    if gone(pid, time.time() + 10):
        say("Stopped.")
        runfile.clear(state_dir, pid=pid)
        if args.wait:
            wait_for_shutdown(pid, ports_to_watch, state_dir)
        return 0

    warn(f"Could not stop process {pid}.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
