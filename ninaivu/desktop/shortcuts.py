"""Ninaivu's three faces on the Desktop: the family app, the console and the
Control Panel, each with its own icon.

The first time the Control Panel opens on a computer it puts three
shortcuts on the Desktop:

* **Ninaivu** — the family app in the browser;
* **Ninaivu Console** — the console in the browser;
* **Ninaivu Control Panel** — the Control Panel.

Each opens the one desktop program (``Ninaivu.exe``, ``Ninaivu.app``, the
Linux folder's ``ninaivu``, or a checkout's Control Panel), the first two
with ``--open family`` or ``--open console``: that starts Ninaivu when it is
stopped and opens the page once it answers, with the panel put away in the
tray. When Ninaivu is open already, the new one hands the page to it and
ends (:mod:`desktop.instance`). So a shortcut never opens a page that does
not load, and never needs to know the port.

On a Mac each is a small app of its own (a ``.app`` folder holding a
one-line script and the icon), on Windows a ``.lnk``, on Linux a trusted
``.desktop`` file.

**Only once.** A record in the run-time folder says what was placed. A
shortcut the person deletes is not put back; one still there is pointed at
this copy of Ninaivu each time the panel opens, so a newer portable copy
that brought the library across (the record comes with it) takes the
shortcuts over, as it takes over the sign-in start. A file already on the
Desktop under one of the names that Ninaivu did not put there is left alone.

    python -m ninaivu.desktop.shortcuts --add | --remove | --status
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: In the run-time folder: what was placed, where, and what it opened.
RECORD = "desktop-shortcuts.json"
#: The icons a shortcut points at, made from the package's PNGs.
ICON_FOLDER = "shortcut-icons"
#: Inside a Mac shortcut, so one is never mistaken for an app of the person's.
MAC_MARK = "ninaivu-shortcut.json"
#: In a Linux shortcut, for the same reason.
LINUX_MARK = "X-Ninaivu-Shortcut"

STATIC = Path(__file__).resolve().parents[1] / "static"


@dataclass(frozen=True)
class Face:
    key: str             # 'family', 'console', 'panel'
    name: str            # the shortcut's name on the Desktop
    comment: str
    args: tuple[str, ...]
    icon: Path           # the 512-pixel PNG it is drawn from


FACES = (
    Face("family", "Ninaivu", "Your family's photographs", ("--open", "family"),
         STATIC / "icons" / "icon-512.png"),
    Face("console", "Ninaivu Console", "Ninaivu's settings, for whoever looks after it",
         ("--open", "console"), STATIC / "icons" / "admin" / "icon-512.png"),
    Face("panel", "Ninaivu Control Panel", "Start and stop Ninaivu", (),
         STATIC / "icons" / "panel" / "icon-512.png"),
)


@dataclass(frozen=True)
class Program:
    """What a shortcut runs: the Control Panel, and the folder it runs in.
    *bundle* is the Mac app, opened with ``open``."""
    argv: tuple[str, ...]
    workdir: Path
    bundle: Path | None = None


def _system(platform: str | None = None) -> str:
    platform = platform or sys.platform
    return "win32" if platform.startswith("win") else "darwin" if platform == "darwin" else "linux"


def _app_bundle(path: str) -> Path | None:
    """The ``.app`` folder *path* is inside, if any."""
    for parent in Path(path).parents:
        if parent.suffix == ".app":
            return parent
    return None


def program(environ=None, platform: str | None = None, root: Path | None = None) -> Program:
    """The Control Panel as this copy of Ninaivu was opened: by its starter
    where it has one, by its Python and the checkout's own double-click
    otherwise."""
    environ = os.environ if environ is None else environ
    system = _system(platform)
    if root is None:
        from .control import ninaivu_root
        root = ninaivu_root()
    launcher = environ.get("NINAIVU_LAUNCHER", "").strip()
    python = environ.get("NINAIVU_PYTHON", "").strip() or sys.executable
    if system == "darwin":
        for told in (launcher, python):
            bundle = _app_bundle(told) if told else None
            if bundle is not None:
                return Program((), bundle.parent, bundle)
    elif system == "win32":
        if launcher and Path(launcher).suffix.lower() == ".exe":
            return Program((launcher,), Path(launcher).parent)
        if python.lower().endswith("python.exe"):     # pythonw: no console window
            python = python[:-len("python.exe")] + "pythonw.exe"
        return Program((python, str(root / "launcher" / "ninaivu_control.pyw")), root)
    else:
        from .linux_menu import PANEL_SCRIPT, portable_script
        portable = environ.get("NINAIVU_PORTABLE_ROOT", "").strip()
        if portable and (Path(portable) / "app").is_dir():
            # By way of sh, as the menu entry does (linux_menu.launchers).
            return Program(("sh", str(portable_script(Path(portable)))), Path(portable))
        script = Path(launcher) if launcher else root / PANEL_SCRIPT
        if script.is_file():
            return Program(("sh", str(script)), root)
    return Program((python, str(root / "launcher" / "ninaivu_control.pyw")), root)


def desktop_folder(environ=None, platform: str | None = None) -> Path | None:
    """The Desktop, as this system names it; None when there is none."""
    environ = os.environ if environ is None else environ
    system = _system(platform)
    if system == "linux":
        from .linux_menu import desktop_folder as linux_desktop
        return linux_desktop(environ)
    if system == "win32" and platform is None:
        found = _windows_desktop()
        if found is not None:
            return found
    folder = Path(environ.get("USERPROFILE" if system == "win32" else "HOME", Path.home())) / "Desktop"
    return folder if folder.is_dir() else None


def _windows_desktop() -> Path | None:
    """The Desktop Windows shows, which OneDrive may have moved."""
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY: the folder, not the virtual Desktop.
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0 and buffer.value:
            return Path(buffer.value)
    except (AttributeError, OSError):
        pass
    return None


def shortcut_path(desktop: Path, face: Face, platform: str | None = None) -> Path:
    system = _system(platform)
    if system == "darwin":
        return desktop / f"{face.name}.app"
    if system == "win32":
        return desktop / f"{face.name}.lnk"
    return desktop / f"local.ninaivu.{face.key}.desktop"


def describe(face: Face, prog: Program, platform: str | None = None) -> dict:
    """What a shortcut opens, as recorded: a change means it is written again."""
    system = _system(platform)
    if system == "darwin" and prog.bundle is not None:
        return {"bundle": str(prog.bundle), "args": list(face.args)}
    return {"argv": [*prog.argv, *face.args], "workdir": str(prog.workdir)}


# -- each system's shortcut ----------------------------------------------------------

def mac_script(face: Face, prog: Program) -> str:
    if prog.bundle is not None:
        # -n: a second Ninaivu, which tells the open one what to show and ends;
        # without it, a Ninaivu already open would only come to the front.
        command = ["/usr/bin/open", "-n", "-a", str(prog.bundle)]
        if face.args:
            command += ["--args", *face.args]
    else:
        command = [*prog.argv, *face.args]
    return (f"#!/bin/sh\n# {face.name}: put on the Desktop by Ninaivu.\n"
            f"cd {shlex.quote(str(prog.workdir))} || exit 1\n"
            f"exec {' '.join(shlex.quote(part) for part in command)}\n")


def write_mac(target: Path, face: Face, prog: Program, icon: Path) -> None:
    contents = target / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleName": face.name,
        "CFBundleDisplayName": face.name,
        "CFBundleIdentifier": f"local.ninaivu.shortcut.{face.key}",
        "CFBundleExecutable": "open-ninaivu",
        "CFBundleIconFile": "icon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        # It only hands over to Ninaivu.app and ends: no Dock icon of its own.
        "LSUIElement": True,
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    script = contents / "MacOS" / "open-ninaivu"
    script.write_text(mac_script(face, prog), encoding="utf-8")
    script.chmod(0o755)
    (contents / "Resources" / "icon.icns").write_bytes(icon.read_bytes())
    (contents / "Resources" / MAC_MARK).write_text(json.dumps({"face": face.key}), encoding="utf-8")
    os.utime(target)                     # so Finder looks at the icon again


def linux_entry(face: Face, prog: Program, icon: Path) -> str:
    from .linux_menu import entry
    text = entry(face.name, face.comment, [*prog.argv, *face.args], workdir=prog.workdir)
    text = text.replace("Icon=ninaivu", f"Icon={icon}")
    return text.rstrip("\n") + f"\n{LINUX_MARK}={face.key}\n"


def write_linux(target: Path, face: Face, prog: Program, icon: Path) -> None:
    from .linux_menu import trust
    target.write_text(linux_entry(face, prog, icon), encoding="utf-8")
    trust(target)


def _ps(text: str) -> str:
    """A PowerShell string literal."""
    return "'" + str(text).replace("'", "''") + "'"


def windows_script(made: list[tuple[Path, Face, Program, Path]]) -> str:
    """One PowerShell run that writes every .lnk: WScript.Shell is how a
    shortcut is made without a library of its own."""
    lines = ["$ErrorActionPreference = 'Stop'", "$shell = New-Object -ComObject WScript.Shell"]
    for target, face, prog, icon in made:
        argv = [*prog.argv, *face.args]
        arguments = subprocess.list2cmdline(argv[1:])
        lines += [f"$link = $shell.CreateShortcut({_ps(target)})",
                  f"$link.TargetPath = {_ps(argv[0])}",
                  f"$link.Arguments = {_ps(arguments)}",
                  f"$link.WorkingDirectory = {_ps(prog.workdir)}",
                  f"$link.IconLocation = {_ps(f'{icon},0')}",
                  f"$link.Description = {_ps(face.comment)}",
                  "$link.Save()"]
    return "\n".join(lines) + "\n"


def write_windows(made, run: Callable = subprocess.run) -> None:
    if not made:
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", "-"], input=windows_script(made), capture_output=True, text=True,
               timeout=60, creationflags=flags)
    if done.returncode != 0:
        raise OSError((done.stderr or done.stdout or "PowerShell could not make the shortcuts").strip())


def make_icon(face: Face, folder: Path, platform: str | None = None) -> Path:
    """The face's icon as this system wants it, in *folder*."""
    system = _system(platform)
    if system == "linux":
        return face.icon                 # the PNG itself, by its path
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    image = Image.open(face.icon).convert("RGBA")
    if system == "darwin":
        target = folder / f"{face.key}.icns"
        image.save(target, "ICNS")
    else:
        target = folder / f"{face.key}.ico"
        image.save(target, "ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128),
                                         (256, 256)])
    return target


# -- placing them --------------------------------------------------------------------

def _ours(path: Path, face: Face, record: dict, platform: str | None = None) -> bool:
    """Whether the file at *path* is a shortcut Ninaivu put there."""
    if str(path) not in record.get("placed", {}):
        return False
    system = _system(platform)
    if system == "darwin":
        return (path / "Contents" / "Resources" / MAC_MARK).is_file()
    if system == "linux":
        try:
            return f"{LINUX_MARK}={face.key}" in path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
    return path.is_file()


def read_record(control: Path) -> dict:
    try:
        record = json.loads((control / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _write_record(control: Path, record: dict) -> None:
    control.mkdir(parents=True, exist_ok=True)
    temporary = control / (RECORD + ".tmp")
    temporary.write_text(json.dumps(record, indent=1), encoding="utf-8")
    os.replace(temporary, control / RECORD)


def settle(control: Path, *, environ=None, platform: str | None = None, desktop: Path | None = None,
           prog: Program | None = None, force: bool = False, run: Callable = subprocess.run) -> list[str]:
    """Put the shortcuts on the Desktop the first time, and keep the ones
    still there pointing at this Ninaivu; the names of those it wrote.

    *force* (``--add``) puts back the ones that are missing too.
    """
    system = _system(platform)
    record = read_record(control)
    first = not record.get("done")
    desktop = desktop or desktop_folder(environ, platform)
    if desktop is None:
        if first:
            _write_record(control, {"done": True, "placed": {}})
        return []
    prog = prog or program(environ, platform)
    placed = dict(record.get("placed", {}))
    icons = control / ICON_FOLDER
    wanted = []
    for face in FACES:
        target = shortcut_path(desktop, face, platform)
        opens = describe(face, prog, platform)
        if _ours(target, face, record, platform):
            if placed.get(str(target)) == opens:
                continue                                   # already right
        elif target.exists() or target.is_symlink():
            continue                                       # somebody else's: left alone
        elif not (first or force):
            placed.pop(str(target), None)                  # deleted: not put back
            continue
        wanted.append((target, face, opens))
    written = []
    windows = []
    for target, face, opens in wanted:
        icon = make_icon(face, icons, platform)
        if system == "darwin":
            write_mac(target, face, prog, icon)
        elif system == "linux":
            write_linux(target, face, prog, icon)
        else:
            windows.append((target, face, prog, icon))
        placed[str(target)] = opens
        written.append(face.name)
    write_windows(windows, run)
    _write_record(control, {"done": True, "placed": placed})
    return written


def remove(control: Path, *, platform: str | None = None) -> list[str]:
    """Take away the shortcuts Ninaivu placed; the names of those removed."""
    import shutil
    record = read_record(control)
    gone = []
    for told in record.get("placed", {}):
        path = Path(told)
        face = next((f for f in FACES if shortcut_path(path.parent, f, platform).name == path.name), None)
        if face is None or not _ours(path, face, record, platform):
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        gone.append(face.name)
    _write_record(control, {"done": True, "placed": {}})
    return gone


def settle_quietly(control: Path) -> list[str]:
    """For the Control Panel: never stops it opening. ``NINAIVU_DESKTOP_SHORTCUTS=0``
    turns it off (the release checks, which open the panel on build machines)."""
    if os.environ.get("NINAIVU_DESKTOP_SHORTCUTS", "").strip() == "0":
        return []
    try:
        return settle(control)
    except Exception:                                      # noqa: BLE001 - logged, the panel opens
        import logging
        logging.getLogger(__name__).exception("could not put Ninaivu's shortcuts on the Desktop")
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ninaivu.desktop.shortcuts",
                                     description="Ninaivu's shortcuts on the Desktop.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true", help="put them on the Desktop (again)")
    group.add_argument("--remove", action="store_true", help="take away the ones Ninaivu put there")
    group.add_argument("--status", action="store_true", help="say which are there")
    options = parser.parse_args(argv)
    from . import portable
    from .control import control_dir, ninaivu_root
    portable.activate_environment()
    control = control_dir(ninaivu_root())
    if options.add:
        names = settle(control, force=True)
        print("On the Desktop: " + ", ".join(names) if names else "Nothing to add: they are there.")
    elif options.remove:
        names = remove(control)
        print("Removed: " + ", ".join(names) if names else "Ninaivu had put no shortcuts there.")
    else:
        placed = read_record(control).get("placed", {})
        there = [p for p in placed if Path(p).exists()]
        print("\n".join(there) if there else "None of Ninaivu's shortcuts are on the Desktop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
