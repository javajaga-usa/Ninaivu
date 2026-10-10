#!/usr/bin/env python3
"""Ninaivu launcher — one command, any platform.

    python start.py                 # first run: sets up, then opens the apps
    python start.py ~/Pictures      # …and opens that folder as the library
    python start.py --ai light      # skip the ~2 GB AI download, keep basic tagging
    python start.py --ai off        # no AI layer at all
    python start.py --no-setup      # skip dependency checks (fast start)

Unrecognised options are passed straight through to `python -m ninaivu`.

What it does
------------
1. Checks the Python version.
2. Creates a virtual environment in ``.venv`` (unless one is already active).
3. Installs what is missing — nothing more.
4. Finds free ports (80 for the family app — the default HTTP port, so
   ``http://ninaivu.local`` needs no ``:80`` typed on the end; 3000 for the
   admin console), so a busy port never blocks a start.
5. Launches both and opens your browser.

Works on Windows, macOS and Linux with only the standard library.
"""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
from types import ModuleType
from typing import NoReturn
import threading
import time
import venv
import webbrowser
from pathlib import Path

#: The repository root — this file lives one folder down, in launcher/.
HERE = Path(__file__).resolve().parents[1]
VENV_DIR = HERE / ".venv"
#: The exact versions the installers ship and the tests ran with. Every pip
#: call below installs under it when a checkout has it, so a source install
#: gets the same set as a release instead of whatever PyPI published this
#: morning — the bounds in CORE and EXTRAS say what Ninaivu can run with, this
#: file says what it was tested with. Hash-locking (``--require-hashes``) is
#: the step after this one, and not taken here: it needs a generated lock
#: naming every platform's wheels, which this repository does not keep yet.
CONSTRAINTS = HERE / "requirements" / "constraints-tested.txt"
MIN_PYTHON = (3, 12)            # what pyproject.toml requires
# No token is shipped with Ninaivu. To authenticate against huggingface.co
# (for example when downloading an OpenCLIP model the first time), set the
# HF_TOKEN environment variable yourself before starting the launcher.

CORE = ["flask>=3.0", "pillow>=12.3", "numpy>=1.24"]
#: Everything in requirements.txt that is not core. Kept in step with that
#: file by a test — they drifted apart once, and the result was a fresh install
#: silently missing the second EXIF reader, the name on the network, and video
#: poster frames on any machine without ffmpeg.
EXTRAS = [
    "waitress>=3.0",              # a real thread pool; without it Ninaivu falls
                                  # back to Werkzeug's development server
    "watchdog>=3.0",              # live library watching
    "zeroconf>=0.130",            # answer as ninaivu.local on the home network
    "mutagen>=1.47",              # audio tags and durations
    "pillow-heif>=0.15",          # HEIC/HEIF (iPhone photos)
    "opencv-python-headless>=4.8,<5",  # video posters; 5.0 dropped the Haar cascades
    "rawpy>=0.19",                # develop RAW files with no usable preview
    "exifread>=3.0",              # a second EXIF reader for the Archive tab
    "comtypes>=1.4; sys_platform == 'win32'",  # read a phone over USB
    "cryptography>=42",           # --https without an openssl binary
    "psutil>=5.9,<8",             # the Server page's readings and process monitoring
]
AI = ["torch", "open_clip_torch"]

IS_WINDOWS = os.name == "nt"
ANSI = sys.stdout.isatty() and not IS_WINDOWS or os.environ.get("WT_SESSION")


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if ANSI else text


def say(message: str) -> None:
    print(f"  {paint('•', '36')} {message}")


def warn(message: str) -> None:
    print(f"  {paint('!', '33')} {message}")


def die(message: str) -> NoReturn:
    print(f"  {paint('x', '31')} {message}", file=sys.stderr)
    raise SystemExit(1)


def use_utf8_output() -> None:
    """Stop the launcher dying of its own progress messages.

    Everything printed here uses a bullet or an ellipsis, and `python -m
    ninaivu` prints arrows and em dashes. On Windows those only survive while
    stdout is an interactive console; once it is a pipe or a file the encoding
    falls back to the locale's — cp1252 on most machines — and printing one of
    them raises :class:`UnicodeEncodeError`. So anyone who runs
    ``start.cmd > log.txt``, or starts Ninaivu from a supervisor, loses it to
    the setup chatter rather than to anything real.

    Kept as its own copy rather than imported from :mod:`ninaivu`: this file has
    to run on a machine where Ninaivu is not installed yet, which is the whole
    point of it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def ensure_venv() -> Path:
    """Return the interpreter to run the app with, creating a venv if needed."""
    if in_virtualenv() or os.environ.get("NINAIVU_NO_VENV"):
        return Path(sys.executable)

    python = venv_python(VENV_DIR)
    if not python.exists():
        say(f"Creating a virtual environment in {VENV_DIR.name}…")
        try:
            venv.EnvBuilder(with_pip=True, upgrade_deps=False).create(VENV_DIR)
        except Exception as exc:  # noqa: BLE001
            warn(f"Could not create a virtual environment ({exc}).")
            warn("Falling back to the current interpreter.")
            return Path(sys.executable)
    return python


def load_resources() -> ModuleType:
    """``ninaivu/utils/resources.py``, loaded by path rather than imported.

    Importing it as ``ninaivu.utils.resources`` runs ``ninaivu/__init__.py``
    first, and that imports Flask. On a first run the launcher is still the
    system python3 — the ``.venv`` that has Flask is only just being made — so
    the import failed and the saved resource mode was dropped: no
    ``--workers``, none of the mode's thread limits, on exactly the launch
    that a fresh machine sees. The file itself needs nothing but :mod:`os`.
    """
    spec = importlib.util.spec_from_file_location(
        "_ninaivu_launcher_resources", HERE / "ninaivu" / "utils" / "resources.py")
    if spec is None or spec.loader is None:
        raise ImportError("ninaivu/utils/resources.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installed(python: Path, module: str) -> bool:
    return subprocess.run(
        [str(python), "-c", f"import {module}"],
        capture_output=True, check=False,
    ).returncode == 0


def pip_install(python: Path, packages: list[str], label: str,
                extra_args: list[str] | None = None, constrained: bool = True) -> bool:
    say(f"Installing {label}…")
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check",
               "--quiet", *(extra_args or []), *packages]
    if constrained and CONSTRAINTS.is_file():
        command += ["-c", str(CONSTRAINTS)]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        warn(f"Could not install {label}. The app may run with reduced features.")
        return False
    return True


#: Which module name proves a package is installed. Only the ones where the
#: two differ need an entry; everything else is the package name with hyphens
#: turned into underscores.
MODULE_FOR = {
    "flask": "flask", "pillow": "PIL", "numpy": "numpy", "watchdog": "watchdog",
    "mutagen": "mutagen", "pillow-heif": "pillow_heif", "torch": "torch",
    "open_clip_torch": "open_clip", "zeroconf": "zeroconf",
    "opencv-python-headless": "cv2", "exifread": "exifread",
    "rawpy": "rawpy",
}


def missing(python: Path, packages: list[str]) -> list[str]:
    out = []
    for spec in packages:
        name = spec.split(">=")[0].split("==")[0].strip()
        module = MODULE_FOR.get(name, name.replace("-", "_"))
        if not installed(python, module):
            out.append(spec)
    return out


def setup(python: Path, want_ai: bool) -> None:
    if python != Path(sys.executable) or in_virtualenv():
        subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", "--quiet",
             "--disable-pip-version-check", "pip"],
            check=False, capture_output=True,
        )

    if gaps := missing(python, CORE):
        if not pip_install(python, gaps, "core dependencies"):
            die("Ninaivu needs Flask, Pillow and NumPy to run.")
    else:
        say("Core dependencies already present.")

    if gaps := missing(python, EXTRAS):
        pip_install(python, gaps,
                    "optional extras (watcher, name on the network, "
                    "audio tags, HEIC, video posters, EXIF)")

    refresh_if_changed(python)

    if want_ai and (gaps := missing(python, AI)):
        say("Installing PyTorch + OpenCLIP — this is a large download (~2 GB).")
        # Each package from the index that actually carries it: the PyTorch
        # CPU index hosts torch and nothing else, so asking it for OpenCLIP
        # always fails — and the all-PyPI fallback then pulls the CUDA build
        # of torch, several gigabytes heavier than the CPU one.
        torch_gaps = [g for g in gaps
                      if g.split(">=")[0].strip().startswith("torch")]
        other_gaps = [g for g in gaps if g not in torch_gaps]
        # Not under the constraints, as the Dockerfile's torch line is not:
        # the CPU index is the only index for this call, and it serves
        # torch's own dependencies at its own versions, not necessarily the
        # ones pinned for PyPI — a pin it cannot meet fails the whole
        # install and lands on the fallback below, the CUDA build. The
        # constraints do not pin torch itself (constraints-tested.txt leaves
        # the graphics-card builds out), and OpenCLIP is installed under them.
        if torch_gaps and not pip_install(
                python, torch_gaps, "PyTorch (CPU build)",
                ["--index-url", "https://download.pytorch.org/whl/cpu"],
                constrained=False):
            pip_install(python, torch_gaps, "PyTorch (default index)")
        if other_gaps:
            pip_install(python, other_gaps, "OpenCLIP")


def requirements_stamp() -> str:
    """What the launcher asks for, as one fingerprint: every bound in it, and
    the constraints file it installs under — a raised pin is as much a change
    to what is asked for as a moved bound, and has to reach the .venv the
    same way. A checkout without the file fingerprints the list alone."""
    try:
        pins = CONSTRAINTS.read_bytes()
    except OSError:
        pins = b""
    digest = hashlib.sha256("\n".join([*CORE, *EXTRAS]).encode("utf-8"))
    digest.update(b"\n--\n" + pins)
    return digest.hexdigest()


def refresh_if_changed(python: Path) -> None:
    """Bring Ninaivu's own .venv up to what this version asks for.

    ``missing`` only asks whether each module imports, so a bound that moved
    (a version too old, a version known to break) never reached a .venv made
    by an earlier Ninaivu. The fingerprint of the list is kept in the .venv,
    and when it differs everything is asked for again: pip upgrades what no
    longer fits and leaves the rest. If that fails offline, Ninaivu starts
    with what is there and tries again next time; if one optional package
    has no build for this computer, the rest are brought up one at a time
    and the list is not asked for again. From Ninaivu Lite. Only Ninaivu's
    own .venv: a Python somebody else manages is theirs.
    """
    if python != venv_python(VENV_DIR):
        return
    stamp = VENV_DIR / ".ninaivu-requirements"
    wanted = requirements_stamp()
    try:
        if stamp.read_text(encoding="utf-8").strip() == wanted:
            return
    except OSError:
        pass
    done = pip_install(python, [*CORE, *EXTRAS], "updates to what Ninaivu needs")
    if not done and pip_install(python, CORE, "updates to the core dependencies"):
        # The core is reachable, so the index is: what failed is an optional
        # package this computer cannot have, which asking again will not change.
        for spec in EXTRAS:
            pip_install(python, [spec], spec.split(";")[0].strip())
        done = True
    if done:
        try:
            stamp.write_text(wanted, encoding="utf-8")
        except OSError:
            pass
    else:
        warn("Starting with what is already installed; this is tried again next time.")


# ---------------------------------------------------------------------------
# Port selection
# ---------------------------------------------------------------------------

def port_free(host: str, port: int) -> bool | None:
    """True if nothing holds *port*, False if something does, and None when this
    user may not bind it on *host* at all — which is not the same as taken."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif not IS_WINDOWS:
            # As the server binds (see port_is_free in ninaivu/__main__.py): a
            # listener still refuses it, a restart's closed connections do not.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except PermissionError:
            return None
        except OSError:
            return False


#: Where the family app goes when this user may not use port 80 or 443 at
#: all (Linux without root): the same few ports every time, as the server's
#: own UNPRIVILEGED_FALLBACK, never a random one. Each start used to land on
#: a port picked by the system, and the address saved on every phone broke.
UNPRIVILEGED_FALLBACK = {443: (8443, 8080, 8081, 5000), 80: (8080, 8081, 8443, 5000)}


def find_port(host: str, preferred: int, tries: int = 40) -> int:
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host

    def allowed(candidate: int) -> bool | None:
        free = port_free(probe, candidate)
        if free is None and probe != host:
            free = port_free(host, candidate)
        return free

    if preferred < 1024 and allowed(preferred) is None:
        for candidate in UNPRIVILEGED_FALLBACK.get(preferred, UNPRIVILEGED_FALLBACK[80]):
            if allowed(candidate):
                return candidate
    for offset in range(tries):
        candidate = preferred + offset
        if candidate > 65535:
            break
        free = port_free(probe, candidate)
        # macOS lets an ordinary user bind a port below 1024 on every address
        # but not on 127.0.0.1 alone. Asked only there, 443 and the 39 ports
        # after it all read as busy, and the family app landed on a random
        # port — so https://ninaivu.local, the address every phone had from
        # Windows, answered nothing at all. Ask where the server will listen.
        if free is None and probe != host:
            free = port_free(host, candidate)
        if free:
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((probe, 0))
        return int(sock.getsockname()[1])


def format_url(display: str, port: int, scheme: str = "http") -> str:
    """A URL with no port shown when *port* is that scheme's default.

    This is the whole point of defaulting the family app to port 80: a URL
    built from an mDNS name and this function reads exactly as typed —
    ``http://ninaivu.local`` — with nothing appended that the person didn't
    ask to see. Any other port still gets shown, same as always.
    """
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{display}{suffix}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description="Set up and start Ninaivu on any platform.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("folder", nargs="?", help="Library folder to open")
    parser.add_argument("--port", type=int, default=None,
                        help="Family app port (default 443 with HTTPS, otherwise 80; falls back if busy)")
    parser.add_argument("--admin-port", type=int, default=3000,
                        help="Admin console port (default 3000; falls back if busy)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Address to serve on (default: the whole home network)")
    parser.add_argument("--local-only", action="store_true",
                        help="Serve to this machine only")
    # Doubles as the server's --ai flag. Not given, nothing large is
    # installed and the server decides by itself (Config.ai_engine "auto"):
    # the light engine until the image model is added — from the first-day
    # walk-through or Home & extensions → Extras — and the image model after that on a
    # machine that can run it. It used to default to "auto" here, which
    # installed PyTorch and fetched the model on the first start, about 2 GB
    # that nobody had asked for. `--ai auto` or `--ai clip` still does that.
    parser.add_argument("--ai", nargs="?", const="auto", default=None,
                        choices=["auto", "clip", "light", "off"],
                        help="AI tier. Not given: decided by the server, and "
                             "nothing large is installed. 'auto'/'clip' install "
                             "PyTorch + OpenCLIP now (~2 GB)")
    parser.add_argument("--setup-only", action="store_true",
                        help="Install dependencies and exit, without starting")
    parser.add_argument("--no-setup", action="store_true",
                        help="Skip dependency checks")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--rescan", action="store_true",
                        help="Force a full re-index on start")
    parser.add_argument("--https", dest="https", action="store_true", default=True,
                        help="Serve over HTTPS, creating a local CA and "
                             "certificate the first time (default: on; see "
                             "the console for how to install it on other devices)")
    parser.add_argument("--http", "--no-https", dest="https", action="store_false",
                        help="Serve over plain HTTP instead of HTTPS")
    parser.add_argument("--cert", default=None, metavar="FILE",
                        help="Use this certificate (implies --https)")
    parser.add_argument("--key", default=None, metavar="FILE",
                        help="Private key for --cert")
    args, passthrough = parser.parse_known_args()
    if args.port is None:
        args.port = 443 if (args.https or args.cert) else 80

    if sys.version_info < MIN_PYTHON:
        die(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
            f"(found {platform.python_version()}).")

    print()
    print(f"  {paint('Ninaivu', '1;36')}  ·  {platform.system()} "
          f"· Python {platform.python_version()}")
    print()

    want_ai = args.ai in ("auto", "clip")
    python = Path(sys.executable) if args.no_setup else ensure_venv()
    if not args.no_setup:
        setup(python, want_ai)

    if args.setup_only:
        say("Everything is installed. Ninaivu is ready to start.")
        return 0

    if args.local_only:
        args.host = "127.0.0.1"

    port = find_port(args.host, args.port)
    if port != args.port and args.port < 1024 and port_free(args.host, args.port) is None:
        say(f"This account may not use port {args.port}, so the family app is on {port}.")
    elif port != args.port:
        warn(f"Port {args.port} is busy — using {port} for the family app.")
        if args.port == 80 and IS_WINDOWS:
            # The single most common reason 80 is taken on a Windows machine
            # that didn't ask for a web server: IIS ships disabled but
            # installed on many editions, and a stray "World Wide Web
            # Publishing Service" holds the port even though nothing is
            # actually served from it.
            warn('(Often IIS — the "World Wide Web Publishing Service" — or '
                 "Docker Desktop/Skype. Free up port 80 to get the no-port "
                 "address back.)")
        elif args.port == 443 and IS_WINDOWS:
            warn('(Often IIS, VMware Workstation, or Docker Desktop holding port 443. '
                 "Free up port 443 to get the standard HTTPS port back.)")
    admin_port = find_port(args.host, args.admin_port)
    if admin_port == port:
        admin_port = find_port(args.host, port + 1)
    if admin_port != args.admin_port:
        warn(f"Port {args.admin_port} is busy — using {admin_port} for the console.")

    command = [str(python), "-m", "ninaivu", "--host", args.host,
               "--port", str(port), "--admin-port", str(admin_port)]
    if args.folder:
        folder = Path(args.folder).expanduser()
        if not folder.is_dir():
            die(f"{folder} is not a folder.")
        # --root rather than the positional, so it can never collide with a
        # stray argument in the passthrough list.
        command += ["--root", str(folder.resolve())]
    if args.ai:
        command += ["--ai", args.ai]
    if args.rescan:
        command.append("--rescan")
    if args.cert or args.key:
        if not (args.cert and args.key):
            die("--cert and --key must be given together.")
        command += ["--cert", args.cert, "--key", args.key]
    elif args.https:
        command.append("--https")

    # Synchronize with desktop control center resource mode if present
    settings_file = HERE / ".ninaivu-control" / "settings.json"
    saved_mode = "standard"
    if settings_file.is_file():
        try:
            saved_mode = json.loads(settings_file.read_text(encoding="utf-8")).get("mode", "standard")
        except (OSError, ValueError):
            pass

    mode_env = {}
    try:
        resources = load_resources()
        if not any(arg == "--workers" or str(arg).startswith("--workers=") for arg in passthrough):
            command += ["--workers", str(resources.budget(saved_mode)["workers"])]
        mode_env = resources.environment(saved_mode)
    except Exception as exc:
        warn(f"Could not apply the saved resource mode ({saved_mode}): {exc}. "
             "Starting with the default settings.")

    command += passthrough

    scheme = "https" if (args.https or args.cert) else "http"
    display = 'localhost' if args.host in ('0.0.0.0', '127.0.0.1') else args.host
    url = format_url(display, port, scheme)
    admin_url = format_url(display, admin_port, scheme)
    if not args.no_browser:
        def open_when_ready() -> None:
            probe = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
            deadline = time.time() + 45
            while time.time() < deadline:
                if not port_free(probe, port):  # server is listening
                    webbrowser.open(url)
                    return
                time.sleep(0.4)

        threading.Thread(target=open_when_ready, daemon=True).start()

    say(f"Family app     {paint(url, '1;32')}")
    say(f"Admin console  {paint(admin_url, '1;35')}")
    print("    (Ctrl+C to stop)")
    print()

    # The child inherits this process's stdout, so if that is a pipe it gets
    # the locale encoding too. `python -m ninaivu` retunes its own streams, but
    # saying so in the environment covers the interpreter's own errors, which
    # are written before any of Ninaivu's code runs.
    env = {**os.environ, **mode_env, "PYTHONPATH": str(HERE), "PYTHONUNBUFFERED": "1",
           "PYTHONIOENCODING": os.environ.get("PYTHONIOENCODING", "utf-8")}

    runtime_dir = HERE / ".ninaivu-control"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "server.log"

    # Only a server that could not be started at all falls back to running
    # it plainly. Once one is running, whatever goes wrong with relaying its
    # output is not a reason to start another: that fallback used to catch a
    # failed write to this window or the log (OSError), start a second server
    # beside the first, and have it refused the state folder.
    log_file = None
    try:
        log_file = log_path.open("a", encoding="utf-8", errors="replace")
        log_file.write(f"\n--- Ninaivu launcher starting ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---\n")
        log_file.write(f"Family app     {url}\n")
        log_file.write(f"Admin console  {admin_url}\n\n")
        log_file.flush()
        proc = subprocess.Popen(
            command, cwd=str(HERE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except KeyboardInterrupt:
        if log_file is not None:
            log_file.close()
        print("\n  Stopped.")
        return 0
    except (OSError, ValueError):
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass
        try:
            return subprocess.call(command, cwd=str(HERE), env=env)
        except KeyboardInterrupt:
            print("\n  Stopped.")
            return 0

    with log_file:
        return follow(proc, (sys.stdout, log_file))


def relay(lines, sinks) -> None:
    """Copy each line to every sink still taking them.

    A sink that fails — this window closed, a pipe gone, the disk with the log
    full — is dropped and the rest carry on. Every line is still read, so the
    server never blocks on a full pipe nobody is emptying.
    """
    live = list(sinks)
    for line in lines:
        for sink in tuple(live):
            try:
                sink.write(line)
                sink.flush()
            except (OSError, ValueError):
                live.remove(sink)


def follow(proc, sinks) -> int:
    """Relay the server's output until it ends, and return its exit code.

    Ctrl+C reaches the server too (same console), which then stops properly
    and says so; the first one keeps relaying its goodbye, a second stops
    waiting on it. Nothing here starts another server.
    """
    interrupted = False
    while True:
        try:
            relay(proc.stdout, sinks)
            break
        except KeyboardInterrupt:
            if interrupted:
                break
            interrupted = True
        except (OSError, ValueError):
            # The pipe itself failed. The server is still running; wait for it.
            break
    try:
        return proc.wait(timeout=5 if interrupted else None)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        try:
            proc.terminate()
            return proc.wait(timeout=3)
        except Exception:                                   # noqa: BLE001
            return 0

if __name__ == "__main__":
    raise SystemExit(main())
