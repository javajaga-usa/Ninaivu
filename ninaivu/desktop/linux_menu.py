"""Ninaivu in a Linux desktop's application menu and on its Desktop.

A Mac has Ninaivu.app and Windows has Ninaivu.exe: a double-click. On Linux a
file manager opens a shell script in a text editor rather than running it, so
the portable folder and a checkout each carry a ``Ninaivu.desktop`` beside
the program (installers/linux/portable/Ninaivu.desktop and the one at the
root of the repository), which opens it from where it is. GNOME asks once to
*Allow Launching* it and Xfce (MX Linux) to *Mark Executable*.

This writes the same launcher with the folder's own paths into the
application menu, ``~/.local/share/applications``, and onto the Desktop,
marked as trusted so it opens with one click, with Ninaivu's icon:

    ./ninaivu --add-to-menu              # the portable folder
    ./ninaivu --remove-from-menu
    python3 ninaivu/desktop/linux_menu.py --add       # a checkout
    python3 ninaivu/desktop/linux_menu.py --remove

From a portable folder there is one entry, *Ninaivu*, which opens the
Control Panel. From a checkout there are two, as "Setup Ninaivu.command" makes
on a Mac: *Ninaivu*, which sets the checkout up the first time and starts the
library in a terminal (launcher/start.sh), and *Ninaivu Control Panel*, with
the .venv that made. Moving the folder needs ``--add-to-menu`` again.

Standard library only: a checkout runs it with the system's Python, before
anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

#: The file names it writes, so that removing them never touches another
#: program's entries.
PORTABLE_ID = "local.ninaivu.portable"
CHECKOUT_ID = "local.ninaivu.checkout"
PANEL_ID = "local.ninaivu.control-panel"
ICON_NAME = "ninaivu"

HERE = Path(__file__).resolve().parent


def data_home(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("XDG_DATA_HOME") or Path(environ.get("HOME", Path.home())) / ".local" / "share")


def desktop_folder(environ=None) -> Path | None:
    """The Desktop, as ``xdg-user-dir`` names it (it is translated: "Bureau",
    "Schreibtisch"), or ~/Desktop; None when there is none."""
    environ = os.environ if environ is None else environ
    found = ""
    if shutil.which("xdg-user-dir"):
        try:
            found = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True,
                                   timeout=10, env=dict(environ)).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            found = ""
    home = Path(environ.get("HOME", Path.home()))
    folder = Path(found) if found else home / "Desktop"
    # xdg-user-dir answers the home folder itself when no Desktop is set.
    if folder == home or not folder.is_dir():
        return None
    return folder


def quote_exec(argument: str) -> str:
    """One argument of an Exec line, as the Desktop Entry spec quotes it:
    in double quotes, with " ` $ and \\ escaped, and then the backslashes
    doubled again for the key file itself."""
    if argument and not any(c in argument for c in ' \t\n"\'\\><~|&;$*?#()`%'):
        return argument
    inner = "".join("\\" + c if c in '"`$\\' else c for c in argument.replace("%", "%%"))
    return ('"' + inner + '"').replace("\\", "\\\\")


def value(text: str) -> str:
    """A key file's string value: a backslash and the control characters
    written as escapes."""
    return (str(text).replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\t", "\\t").replace("\r", "\\r"))


def entry(name: str, comment: str, argv: list[str], *, workdir: Path,
          terminal: bool = False) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={name}",
        f"Comment={comment}",
        "Exec=" + " ".join(quote_exec(a) for a in argv),
        f"Path={value(workdir)}",
        f"Icon={ICON_NAME}",
        f"Terminal={'true' if terminal else 'false'}",
        "Categories=Graphics;Photography;",
        "StartupNotify=true",
        "",
    ])


def icon_source(root: Path) -> Path | None:
    """Ninaivu's icon: in the package (a portable folder's site-packages, or
    a checkout)."""
    for candidate in (HERE.parent / "static" / "icons" / "icon-512.png",
                      root / "ninaivu" / "static" / "icons" / "icon-512.png"):
        if candidate.is_file():
            return candidate
    return None


def launchers(root: Path) -> list[tuple[str, str]]:
    """(file id, text) of each launcher for the folder at *root*."""
    if (root / "ninaivu").is_file() and (root / "app").is_dir():
        return [(PORTABLE_ID, entry(
            "Ninaivu", "Your family's photographs: the Ninaivu Control Panel",
            # By way of sh, whose path has nothing to quote: GLib looks for the
            # program before it reads %% back as %, and a folder with a % in
            # its name ("Photos 100%") was "not found".
            ["sh", str(root / "ninaivu")], workdir=root))]
    if (root / "launcher" / "start.sh").is_file():
        venv_python = root / ".venv" / "bin" / "python"
        return [
            (CHECKOUT_ID, entry(
                "Ninaivu", "Your family's photographs: set up and start the library",
                ["sh", str(root / "launcher" / "start.sh")], workdir=root, terminal=True)),
            (PANEL_ID, entry(
                "Ninaivu Control Panel", "Start and stop Ninaivu",
                [str(venv_python), str(root / "ninaivu_control.pyw")], workdir=root)),
        ]
    raise SystemExit(f"{root} is neither a portable Ninaivu folder nor a checkout")


def trust(path: Path) -> None:
    """Executable, and marked as trusted the way GNOME (``metadata::trusted``)
    and Xfce (``metadata::xfce-exe-checksum``) ask, so a double-click opens
    it. Where gio is missing the desktop asks once instead."""
    path.chmod(0o755)
    if not shutil.which("gio"):
        return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for args in (["gio", "set", str(path), "metadata::trusted", "true"],
                 ["gio", "set", "-t", "string", str(path), "metadata::xfce-exe-checksum", digest]):
        try:
            subprocess.run(args, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


def add(root: Path, environ=None, out=print) -> int:
    environ = os.environ if environ is None else environ
    menu = data_home(environ) / "applications"
    menu.mkdir(parents=True, exist_ok=True)
    icon = icon_source(root)
    if icon is not None:
        icons = data_home(environ) / "icons" / "hicolor" / "512x512" / "apps"
        icons.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(icon, icons / f"{ICON_NAME}.png")
    desktop = desktop_folder(environ)
    for ident, text in launchers(root):
        target = menu / f"{ident}.desktop"
        target.write_text(text, encoding="utf-8")
        target.chmod(0o755)
        out(f"In the application menu: {target}")
        if desktop is not None:
            shortcut = desktop / f"{ident}.desktop"
            shortcut.write_text(text, encoding="utf-8")
            trust(shortcut)
            out(f"On the Desktop: {shortcut}")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(menu)], capture_output=True)
    return 0


def remove(environ=None, out=print) -> int:
    environ = os.environ if environ is None else environ
    places = [data_home(environ) / "applications"]
    desktop = desktop_folder(environ)
    if desktop is not None:
        places.append(desktop)
    for place in places:
        for ident in (PORTABLE_ID, CHECKOUT_ID, PANEL_ID):
            target = place / f"{ident}.desktop"
            if target.exists():
                target.unlink()
                out(f"Removed {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--add", action="store_true", help="put Ninaivu in the menu and on the Desktop")
    which.add_argument("--remove", action="store_true", help="take it out again")
    parser.add_argument("--root", type=Path, default=None,
                        help="the portable folder or checkout (default: the one this is in)")
    args = parser.parse_args(argv)
    if args.remove:
        return remove()
    root = args.root or Path(os.environ.get("NINAIVU_PORTABLE_ROOT") or HERE.parent.parent)
    return add(root.resolve())


if __name__ == "__main__":
    sys.exit(main())
