"""``ninaivu mugil``: set a computer up for Mugil, and say how it stands.

    ninaivu mugil setup [--dry-run]   start Ninaivu with the computer, on any OS
    ninaivu mugil status [--json]     the Mugil checklist, read from real state

The copies, checks, off-site copy, restore tests and nightly wake all run
inside Ninaivu on their own schedules, so what a computer needs is for
Ninaivu to be running: started when the computer starts, and started again if
it stops. ``setup`` arranges that the way each system does it:

* **macOS**: a LaunchAgent, the same as *Start Ninaivu when I sign in* in
  the Control Panel; and it says whether *Wake for network access* is on, so
  the Secondary can wake the Mac at night (mugil/wake.py).
* **Windows**: the same sign-in start the Control Panel sets.
* **Linux**: a systemd user service, ``~/.config/systemd/user/ninaivu.service``,
  restarted when it stops, with the folders this copy of Ninaivu uses. It is
  enabled for the next start, not started now (a Ninaivu already running
  would be started twice). For it to start at boot before anybody signs in,
  ``loginctl enable-linger`` is needed once, which it prints.

Nothing in the household's settings is changed. ``status`` prints the same
checklist the Mugil page shows.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .replica import use_utf8_output

#: The environment a service started without a terminal still needs, so it
#: finds the same index, models and logs as the copy that set it up.
CARRIED = ("NINAIVU_STATE_DIR", "NINAIVU_AI_MODELS_DIR", "NINAIVU_LOG_DIR", "NINAIVU_HOME",
           "NINAIVU_PORTABLE", "NINAIVU_PORTABLE_ROOT", "NINAIVU_PROGRAM_DIR")


def unit_path(home: Path | None = None) -> Path:
    return Path(home or Path.home()) / ".config" / "systemd" / "user" / "ninaivu.service"


def start_command(environ=None, executable: str | None = None) -> list[str]:
    """How this copy of Ninaivu's server is started: the portable folder's own
    launcher when it is one, else this Python."""
    environ = os.environ if environ is None else environ
    portable = environ.get("NINAIVU_PORTABLE_ROOT")
    if environ.get("NINAIVU_PORTABLE") and portable and (Path(portable) / "ninaivu").is_file():
        return [str(Path(portable) / "ninaivu"), "--server"]
    return [executable or sys.executable, "-m", "ninaivu"]


def unit_text(command: list[str], environ=None) -> str:
    environ = os.environ if environ is None else environ
    lines = ["[Unit]", "Description=Ninaivu, the family photo library (Mugil)",
             "After=network-online.target local-fs.target", "Wants=network-online.target", "",
             "[Service]", "Type=simple", f"ExecStart={shlex.join(command)}"]
    for name in CARRIED:
        if environ.get(name):
            lines.append(f"Environment={name}={environ[name]}")
    lines += ["Restart=always", "RestartSec=10s",
              "# Ninaivu's own orderly stop takes up to 20 s; leave room for it.",
              "TimeoutStopSec=45s", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def setup_linux(*, dry_run: bool, home: Path | None = None, environ=None,
                runner=subprocess.run, out=print) -> int:
    path = unit_path(home)
    text = unit_text(start_command(environ), environ)
    if dry_run:
        out(f"Would write {path}:")
        out(text)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        out(f"{path} is already as it should be.")
    else:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        out(f"Wrote {path}.")
    if not shutil.which("systemctl"):
        out("systemctl was not found: this system does not use systemd. Start Ninaivu "
            "with whatever starts programs at boot here.")
        return 1
    for step in (["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", "ninaivu.service"]):
        done = runner(step, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            out(f"{shlex.join(step)} did not work: {(done.stderr or done.stdout).strip()}")
            return 1
    out("Ninaivu will start with this computer's next start, and again if it stops.")
    user = (environ or os.environ).get("USER") or "$USER"
    out(f"For it to start at boot before anybody signs in, run once:  sudo loginctl enable-linger {user}")
    out("To start it now instead of the copy running in a terminal, stop that one and run:  "
        "systemctl --user start ninaivu")
    return 0


def setup_desktop(*, dry_run: bool, platform: str, out=print) -> int:
    from ..desktop import autostart                               # noqa: PLC0415
    if autostart.enabled(platform=platform):
        out("Ninaivu already starts when you sign in.")
    elif dry_run:
        out("Would set Ninaivu to start when you sign in.")
    else:
        out(autostart.enable(platform=platform))
    if platform == "darwin":
        from ..mugil.checklist import network_wake                # noqa: PLC0415
        womp = network_wake(platform=platform)
        if womp is False:
            out("Wake for network access is off, so the Secondary cannot wake this Mac at night. "
                "Turn it on in System Settings, Energy, or run once:  sudo pmset -a womp 1")
        elif womp:
            out("Wake for network access is on: the Secondary can wake this Mac at night.")
    return 0


def setup(*, dry_run: bool = False, platform: str | None = None, out=print, **kwargs) -> int:
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return setup_linux(dry_run=dry_run, out=out, **kwargs)
    if platform in ("darwin", "win32"):
        return setup_desktop(dry_run=dry_run, platform=platform, out=out)
    out(f"Starting Ninaivu with the computer is not set up here on {platform}.")
    return 1


MARKS = {"done": "[x]", "todo": "[ ]", "problem": "[!]", "off": "[-]"}


def status(cfg, *, as_json: bool = False, out=print) -> int:
    import sqlite3                                                # noqa: PLC0415
    from types import SimpleNamespace                             # noqa: PLC0415
    from ..mugil import checklist                                 # noqa: PLC0415
    path = Path(cfg.db_path)
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) if path.exists() else \
        sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        try:
            answer = checklist.checklist(cfg, conn, SimpleNamespace())
        except sqlite3.OperationalError:
            answer = {"role": cfg.mugil_role, "items": [], "done": 0, "of": 0}
    finally:
        conn.close()
    if as_json:
        out(json.dumps(answer, indent=2))
        return 0
    if not answer["items"]:
        out("This computer is the only one: Mugil with two computers is not set up.")
        return 0
    out(f"Mugil checklist ({answer['role']}): {answer['done']} of {answer['of']} in place.")
    for item in answer["items"]:
        facts = {k: v for k, v in item.items() if k not in ("id", "status")}
        said = ", ".join(f"{k} {v}" for k, v in facts.items() if v not in (None, ""))
        out(f"  {MARKS.get(item['status'], '[?]')} {item['id']}" + (f"  ({said})" if said else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(prog="ninaivu mugil", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="what", required=True)
    one = sub.add_parser("setup", help="start Ninaivu with this computer")
    one.add_argument("--dry-run", action="store_true", help="say what would be done, do nothing")
    two = sub.add_parser("status", help="the Mugil checklist")
    two.add_argument("--json", action="store_true")
    two.add_argument("--state-dir", default=None, help="Ninaivu's state folder")
    args = parser.parse_args(argv)
    if args.what == "setup":
        return setup(dry_run=args.dry_run)
    from ..server.config import Config                           # noqa: PLC0415
    if args.state_dir:
        os.environ["NINAIVU_STATE_DIR"] = str(Path(args.state_dir).expanduser())
    return status(Config.load(), as_json=args.json)


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
