"""Starting Ninaivu when somebody signs in to this computer.

Ninaivu ran only while somebody had started it. A Mac restarted by an update,
or after a power cut, came back without it, and the family app was down until
somebody opened Ninaivu. Turned on (the control panel's "Start Ninaivu when I
sign in", or ``--enable`` here), the computer starts it at sign-in:

* on a Mac, a LaunchAgent in ~/Library/LaunchAgents, which launchd runs when
  the user signs in (System Settings lists it under Login Items);
* on Windows, a value under HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.

Either runs this module with ``--start``, which starts Ninaivu the way the
control panel does, with the arguments and resource mode it last ran with.
It starts it once and does not keep it alive, so Stop in the panel stays
stopped. It first waits, a couple of minutes at most, for two things that
arrive a few seconds after sign-in: a network address, without which the name
ninaivu.local is never announced for the whole run, and the library drive.

    python -m ninaivu.desktop.autostart --enable | --disable | --status | --start
"""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

#: The LaunchAgent's name, and the Run value's on Windows.
LABEL = "local.ninaivu.start"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Ninaivu"
#: The portable build's own value, so that turning it on or off never touches
#: an installed Ninaivu's, and one is not reported as the other.
RUN_VALUE_PORTABLE = "Ninaivu (portable)"


def run_value() -> str:
    return RUN_VALUE_PORTABLE if os.environ.get("NINAIVU_PORTABLE") else RUN_VALUE


#: How long --start waits for the network and for the library drive, each.
PATIENCE = 120.0


def ninaivu_root() -> Path:
    from .control import ninaivu_root as where
    return where()


def supported(platform: str | None = None) -> bool:
    return (platform or sys.platform) in ("darwin", "win32")


def agent_path(home: Path | None = None) -> Path:
    return Path(home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def command(root: Path, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    # The portable build has no environment of its own to start from at
    # sign-in: its folders are told to it by Ninaivu.exe. Starting through
    # that, the sign-in start uses the same index, models and logs as the
    # panel did; started bare it would have used ~/.ninaivu and a root of
    # app\pkgs, and looked like a second, empty Ninaivu.
    if platform == "win32" and os.environ.get("NINAIVU_PORTABLE"):
        launcher = Path(os.environ.get("NINAIVU_HOME", "")).parent / "Ninaivu.exe"
        if launcher.is_file():
            return [str(launcher), "--autostart"]
    # pythonw on Windows: python.exe would open a console window at sign-in.
    from .control import python_for_server
    python = python_for_server(root, platform)
    if platform == "win32" and python.name.lower() == "python.exe":
        python = python.with_name("pythonw.exe")
    return [str(python), "-m", "ninaivu.desktop.autostart", "--start"]


def launch_agent(root: Path) -> dict:
    """The LaunchAgent. No KeepAlive: launchd would start Ninaivu again the
    moment somebody stopped it."""
    from .control import control_dir
    # launchd makes and appends to this log itself, at its own permissions:
    # tidy_log() keeps it owner-only and bounded each time the agent starts.
    log = str(control_dir(root) / "autostart.log")
    agent = {
        "Label": LABEL,
        "ProgramArguments": command(root, "darwin"),
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    # launchd starts the agent with an empty environment. The app's launcher
    # says where its files and its Python are; without the same two here,
    # the sign-in start looked for a .venv inside the signed app bundle.
    carried = {name: os.environ[name] for name in ("NINAIVU_HOME", "NINAIVU_PYTHON")
               if os.environ.get(name)}
    if carried:
        agent["EnvironmentVariables"] = carried
    return agent


def _run_key(write: bool = False):
    import winreg                                     # noqa: PLC0415 - Windows only
    access = winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE if write else winreg.KEY_QUERY_VALUE
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def enabled(home: Path | None = None, platform: str | None = None) -> bool:
    platform = platform or sys.platform
    if platform == "darwin":
        return agent_path(home).is_file()
    if platform == "win32":
        try:
            import winreg                             # noqa: PLC0415
            with _run_key() as key:
                winreg.QueryValueEx(key, run_value())
            return True
        except OSError:
            return False
    return False


def enable(root: Path | None = None, home: Path | None = None,
           platform: str | None = None) -> str:
    root = Path(root or ninaivu_root())
    platform = platform or sys.platform
    if platform == "darwin":
        path = agent_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        from .control import control_dir
        control_dir(root).mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(plistlib.dumps(launch_agent(root)))
        temporary.replace(path)
        return "Ninaivu will start when you sign in to this Mac."
    if platform == "win32":
        import winreg                                 # noqa: PLC0415
        with _run_key(write=True) as key:
            winreg.SetValueEx(key, run_value(), 0, winreg.REG_SZ,
                              subprocess.list2cmdline(command(root, platform)))
        return "Ninaivu will start when you sign in to Windows."
    raise RuntimeError("Starting at sign-in is set up here on macOS and Windows. "
                       "On Linux, the installer sets up a systemd service; from a "
                       "checkout, use installers/systemd/ninaivu.service.")


def disable(home: Path | None = None, platform: str | None = None) -> str:
    platform = platform or sys.platform
    if platform == "darwin":
        agent_path(home).unlink(missing_ok=True)
    elif platform == "win32":
        import winreg                                 # noqa: PLC0415
        try:
            with _run_key(write=True) as key:
                winreg.DeleteValue(key, run_value())
        except FileNotFoundError:
            pass
    return "Ninaivu will not start by itself when you sign in."


def wait_for(ready: Callable[[], bool], patience: float = PATIENCE, step: float = 2.0,
             clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep) -> bool:
    """Ask *ready* until it says yes or *patience* runs out."""
    deadline = clock() + patience
    while True:
        try:
            if ready():
                return True
        except Exception:                             # noqa: BLE001 - not ready yet
            pass
        if clock() >= deadline:
            return False
        sleep(step)


def say(message: str) -> None:
    print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}', flush=True)


def start(controller=None, addresses=None, libraries_here=None,
          wait: Callable[..., bool] = wait_for) -> str:
    """Start Ninaivu as the control panel would, once the computer is ready."""
    if addresses is None:
        from ..utils.tls import lan_addresses          # noqa: PLC0415
        addresses = lan_addresses
    if libraries_here is None:
        from ..server.config import Config             # noqa: PLC0415
        from ..storage import roots as roots_kit       # noqa: PLC0415
        cfg = Config.load()

        def libraries_here():
            return all(roots_kit.available(root) for root in cfg.roots)
    if not wait(lambda: bool(addresses())):
        say("no network address after two minutes; starting anyway, reachable "
            "on this computer, and by name once Ninaivu is restarted")
    if not wait(libraries_here):
        say("a library folder is still not there; starting anyway, and the "
            "photographs on it appear when it is")
    if controller is None:
        from .control import Controller                # noqa: PLC0415
        controller = Controller()
    return controller.start()


def tidy_log(root: Path) -> None:
    """The sign-in log, owner-only and under the size the other logs keep to.

    launchd holds it open for this very process, appending: so it is emptied
    in place when it has grown, never moved — a moved file would go on
    growing under its new name.
    """
    from .control import control_dir, private_folder, trim_log
    try:
        log = private_folder(control_dir(root)) / "autostart.log"
    except OSError:
        return
    trim_log(log, rotate=False)
    if os.name == "posix" and log.exists():
        try:
            os.chmod(log, 0o600)
        except OSError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    for name in ("start", "enable", "disable", "status"):
        action.add_argument(f"--{name}", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        print("on" if enabled() else "off")
    elif args.enable:
        print(enable())
    elif args.disable:
        print(disable())
    else:
        os.chdir(ninaivu_root())
        tidy_log(ninaivu_root())
        say("signed in: starting Ninaivu")
        try:
            say(start())
        except Exception:                             # noqa: BLE001
            import traceback                          # noqa: PLC0415
            say("could not start Ninaivu\n" + traceback.format_exc())
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
