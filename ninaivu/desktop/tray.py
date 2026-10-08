"""Ninaivu in the system tray (Windows) or menu bar (macOS).

    python -m ninaivu.desktop.tray

One small icon, one menu: whether Ninaivu is running, Start, Stop, Restart,
Open the family app, Open the console, How to update, View the log,
Start at sign-in, Trust the HTTPS certificate, Quit. The tray's job is to be
there when the console is not. The Control Panel window
(:mod:`ninaivu.desktop.app`) is the fuller view beside it — readings, the
resource mode and the live log — and the two run together happily.

The tray needs ``pystray`` and ``Pillow`` (``requirements-desktop.txt``). The
logic is in :class:`Tray`, which knows nothing about pystray, so it is
tested without a display; :func:`main` puts a pystray icon on it. Every
action runs on a worker thread, because a start can take half a minute and
a menu must never freeze.

The server itself is started and stopped by :class:`desktop.control.Controller`,
exactly as the old panel did: never force-killed, always through the
authenticated stop.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

from .control import UPDATE_ADVICE, Controller

#: The tray's icon, from the same mark as the app's.
ICON = Path(__file__).resolve().parents[1] / "static" / "icons" / "icon-192.png"

#: How often the menu's first line ("Running" / "Stopped") is re-read.
POLL_SECONDS = 5.0


class Tray:
    """The menu and its actions, with no toolkit in sight.

    *notify(title, message)* shows a small notification; *ask(title,
    question)* returns True when the person agrees; *open_url(url)* opens a
    browser. All three default to something sensible and are replaced by the
    pystray adapter (and by the tests).
    """

    def __init__(self, controller: Controller | None = None, *,
                 notify: Callable[[str, str], None] | None = None,
                 ask: Callable[[str, str], bool] | None = None,
                 open_url: Callable[[str], Any] | None = None,
                 run_async: bool = True) -> None:
        self.controller = controller or Controller()
        self.notify = notify or (lambda title, message: print(f"{title}: {message}"))
        self.ask = ask or (lambda title, question: True)
        self.open_url = open_url or webbrowser.open
        self.run_async = run_async
        self.busy: str | None = None
        self.on_change: Callable[[], None] | None = None     # the adapter redraws the menu
        self._lock = threading.Lock()

    # -- state -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self.controller.record())

    def status_line(self) -> str:
        if self.busy:
            return f"{self.busy}…"
        return "Ninaivu is running" if self.running else "Ninaivu is stopped"

    def menu(self) -> list[dict[str, Any]]:
        """The menu as data: ``{"label", "action", "enabled", "checked"}``.
        A ``None`` action is a separator or a line of text."""
        from . import autostart

        running = self.running
        idle = self.busy is None
        items: list[dict[str, Any]] = [
            {"label": self.status_line(), "action": None, "enabled": False},
            {"label": "-", "action": None},
            {"label": "Open the family app", "action": self.open_family, "enabled": running},
            {"label": "Open the console", "action": self.open_console, "enabled": running},
            {"label": "-", "action": None},
            {"label": "Start", "action": self.start, "enabled": idle and not running},
            {"label": "Stop", "action": self.stop, "enabled": idle and running},
            {"label": "Restart", "action": self.restart, "enabled": idle and running},
            {"label": "-", "action": None},
            {"label": "How to update", "action": self.how_to_update, "enabled": True},
            {"label": "View the log", "action": self.view_log, "enabled": True},
            {"label": "Trust the HTTPS certificate", "action": self.trust_certificate, "enabled": idle},
        ]
        if autostart.supported():
            items.append({"label": "Start at sign-in", "action": self.toggle_autostart,
                          "enabled": idle, "checked": autostart.enabled()})
        items += [{"label": "-", "action": None},
                  {"label": "Quit the tray", "action": self.quit, "enabled": True}]
        return items

    # -- actions -------------------------------------------------------------

    def _run(self, label: str, work: Callable[[], str]) -> None:
        """*work* on a worker thread, the menu greyed meanwhile, its one-line
        answer as a notification. Errors are notifications too, never a crash
        in a tray icon nobody can see the traceback of."""
        with self._lock:
            if self.busy:
                self.notify("Ninaivu", f"{self.busy} — wait for it to finish.")
                return
            self.busy = label
        self._changed()

        def go() -> None:
            try:
                message = work()
            except Exception as exc:                       # noqa: BLE001 — shown, not raised
                message = str(exc) or exc.__class__.__name__
            finally:
                with self._lock:
                    self.busy = None
            self.notify("Ninaivu", message)
            self._changed()

        if self.run_async:
            threading.Thread(target=go, name="ninaivu-tray-action", daemon=True).start()
        else:
            go()

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:                              # noqa: BLE001
                pass

    def start(self) -> None:
        self._run("Starting", self.controller.start)

    def stop(self) -> None:
        self._run("Stopping", self.controller.stop)

    def restart(self) -> None:
        def both() -> str:
            self.controller.stop()
            return self.controller.start()
        self._run("Restarting", both)

    def open_family(self) -> None:
        self.open_url(self.controller.server_url(admin=False))

    def open_console(self) -> None:
        self.open_url(self.controller.server_url(admin=True))

    def view_log(self) -> None:
        path = self.controller.runtime / "server.log"
        if not path.is_file():
            self.notify("Ninaivu", "No log yet — Ninaivu has not been started from here.")
            return
        open_file(path)

    def how_to_update(self) -> None:
        """What to do with a newer installer. Ninaivu never asks the internet
        whether one is out: the household brings the installer itself."""
        from .. import __version__
        self.notify("Ninaivu", UPDATE_ADVICE.format(version=__version__))

    def toggle_autostart(self) -> None:
        from . import autostart

        def flip() -> str:
            if autostart.enabled():
                return autostart.disable()
            return autostart.enable(self.controller.root)
        self._run("Changing start at sign-in", flip)

    def trust_certificate(self) -> None:
        """Put Ninaivu's own certificate where this computer's browsers trust
        it — the same steps the old panel's button took, with the same asks."""
        from ..utils.tls import ca_certificate_path

        args = self.controller.settings.get("arguments", [])
        if any(arg == "--cert" or str(arg).startswith("--cert=") for arg in args):
            self.notify("HTTPS certificate", "This server uses a custom certificate; follow its "
                        "issuer's trust instructions instead.")
            return
        path = ca_certificate_path(self.controller.cfg.state_dir)
        if not path.is_file():
            self.notify("HTTPS certificate", "Start Ninaivu with HTTPS first to create its certificate.")
            return

        def trust() -> str:
            if sys.platform == "win32":
                from ..utils.tls import trust_ca_on_windows
                if not self.ask("HTTPS certificate",
                                "Add Ninaivu's certificate to Trusted Root Certification Authorities "
                                "for this Windows account, so browsers here open Ninaivu without a "
                                "warning? Windows will ask you to confirm. Only do this for your own Ninaivu."):
                    return "Left as it was."
                trusted, message = trust_ca_on_windows(path)
                if not trusted:
                    open_file(path)
                    message += (" In the certificate window choose Install Certificate → Current User → "
                                "Trusted Root Certification Authorities.")
                return message
            if sys.platform == "darwin":
                from ..utils.tls import trust_ca_on_mac
                if not self.ask("HTTPS certificate",
                                "Trust Ninaivu's certificate for websites on this Mac, so browsers open "
                                "Ninaivu without a warning? macOS will ask for your password."):
                    return "Left as it was."
                trusted, message = trust_ca_on_mac(path)
                if not trusted:
                    subprocess.run(["open", "-R", str(path)], check=False)
                    message += (" In Keychain Access, find \"Ninaivu local CA\" in the login keychain, "
                                "open Trust and set it to Always Trust.")
                return message
            self.open_url(path.as_uri())
            return "Install the certificate as a trusted root certificate authority."
        self._run("Trusting the certificate", trust)

    def quit(self) -> None:
        """Leave the tray. The server keeps running: it has its own process,
        and this icon was never what kept it up."""
        if self.busy and not self.ask("Ninaivu", f"{self.busy} — leave anyway?"):
            return
        self._quit()

    def _quit(self) -> None:                                # replaced by the adapter
        pass


def open_file(path: Path) -> None:
    """Open *path* with whatever this computer opens it with."""
    if sys.platform == "win32":
        import os
        os.startfile(str(path))                             # noqa: S606 — the person's own file
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


# -- the pystray adapter --------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print("The tray needs pystray and Pillow: pip install -r requirements/requirements-desktop.txt",
              file=sys.stderr)
        return 2

    tray = Tray(notify=lambda title, message: icon.notify(message, title),
                ask=_ask_native)

    def build_menu():
        entries = []
        for item in tray.menu():
            if item["label"] == "-":
                entries.append(pystray.Menu.SEPARATOR)
                continue
            action = item["action"]
            entries.append(pystray.MenuItem(
                item["label"],
                (lambda act: (lambda icon_, item_: act()))(action) if action else None,
                enabled=item.get("enabled", True),
                checked=(lambda checked: (lambda item_: checked))(item["checked"]) if "checked" in item else None,
            ))
        return pystray.Menu(*entries)

    icon = pystray.Icon("ninaivu", Image.open(ICON), "Ninaivu", menu=build_menu())
    tray.on_change = lambda: setattr(icon, "menu", build_menu())
    tray._quit = icon.stop

    def poll() -> None:
        while icon.visible:
            tray._changed()
            threading.Event().wait(POLL_SECONDS)

    def ready(icon_) -> None:
        icon_.visible = True
        threading.Thread(target=poll, name="ninaivu-tray-poll", daemon=True).start()

    icon.run(setup=ready)
    return 0


def _ask_native(title: str, question: str) -> bool:
    """A yes/no question with what the platform has, no toolkit of our own."""
    try:
        if sys.platform == "darwin":
            script = (f'display dialog "{question}" with title "{title}" '
                      'buttons {"No", "Yes"} default button "Yes"')
            done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
            return "Yes" in done.stdout
        if sys.platform == "win32":
            import ctypes
            MB_YESNO, IDYES = 0x4, 6
            return ctypes.windll.user32.MessageBoxW(0, question, title, MB_YESNO | 0x40) == IDYES
        done = subprocess.run(["zenity", "--question", f"--title={title}", f"--text={question}"],
                              check=False)
        return done.returncode == 0
    except Exception:                                      # noqa: BLE001 — no dialog: assume no
        return False


if __name__ == "__main__":
    sys.exit(main())
