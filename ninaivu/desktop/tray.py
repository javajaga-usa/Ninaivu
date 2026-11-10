"""Ninaivu's icon in the system tray (Windows, Linux) or menu bar (macOS).

The tray is the Control Panel minimised, not a program of its own. The one
desktop program (:mod:`ninaivu.desktop.app`) opens the window and puts the
icon beside it; closing the window puts it away into the icon, and the icon
brings it back. One process, one :class:`~ninaivu.desktop.control.Controller`,
one Start button that knows what the other is doing.

    python -m ninaivu.desktop.app --tray      (the same, opened minimised)
    python -m ninaivu.desktop.tray            (says the same thing)

:class:`PanelTray` is the icon's menu when the panel holds it, as data and
with no toolkit in sight, so it is tested without a display: Show Ninaivu
(what clicking the icon does), Open the family app, Start or Stop, and Quit.
Everything else is in the window, one click away. Its actions are handed to
the panel's own queue, so they run on Tk's thread like the window's buttons.

:func:`attach` puts a pystray icon on it, arranged for each system, because
pystray and Tk each want an event loop and macOS lets only the main thread
run one:

* **macOS**: Tk's loop is the main thread's, and it is an NSApplication's
  loop underneath. The icon is made on that thread, joins that same
  NSApplication (``run_detached``), and is shown and updated only from Tk's
  thread; its menu's clicks arrive there too. Nothing of AppKit is touched
  from another thread.
* **Windows**: the icon runs its own message loop on a thread of its own
  (what ``run_detached`` does there). A Windows message loop belongs to the
  thread that made its window, so it and Tk's never meet.
* **Linux**: the X11 tray (XEmbed, pystray's ``xorg`` backend), on a thread
  of its own with its own connection to the display. The AppIndicator and
  GTK backends need a GLib loop, which Tk does not run, and the portable
  build's Python has no GTK bindings anyway. That backend has no menu: a
  click shows the window, where everything is. A desktop with no XEmbed tray
  (GNOME without an extension) shows no icon; :attr:`TrayIcon.shown` is then
  False and closing the window closes it, as before, with Ninaivu still
  running, and opening Ninaivu again brings it back.

Without Tk (a Python built without it) there is no window to minimise, and
:func:`run_alone` runs the older, fuller menu (:class:`Tray`) as the whole
program, so the person is not left with nothing.

The server itself is started and stopped by :class:`desktop.control.Controller`:
never force-killed, always through the authenticated stop. Quitting leaves it
running; the icon was never what kept it up.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

from .control import UPDATE_ADVICE, Controller

#: The tray's icon, from the same mark as the app's.
ICON = Path(__file__).resolve().parents[1] / "static" / "icons" / "panel" / "icon-192.png"

#: How often the menu's first line ("Running" / "Stopped") is re-read.
POLL_SECONDS = 5.0

log = logging.getLogger(__name__)


class PanelTray:
    """The icon's menu when the Control Panel holds it: the panel minimised.

    *host* is the panel. It has ``post(callable)``, which runs the callable
    on Tk's thread, and the panel's own ``show_window``, ``start``, ``stop``,
    ``open_family`` and ``quit``. The menu reads only what :meth:`update`
    was last told, never the disk or the server: on Windows it is built on
    the icon's thread, and a reading there could freeze the icon.
    """

    def __init__(self, host: Any) -> None:
        self.host = host
        self.running = False
        self.busy: str | None = None

    def update(self, running: bool, busy: str | None) -> bool:
        """What the panel now shows; whether the menu changes with it."""
        changed = (bool(running), busy or None) != (self.running, self.busy)
        self.running, self.busy = bool(running), busy or None
        return changed

    def status_line(self) -> str:
        if self.busy:
            return f"{self.busy}…"
        return "Ninaivu is running" if self.running else "Ninaivu is stopped"

    def _on_tk(self, name: str) -> Callable[[], None]:
        """The panel's *name*, to be run on Tk's thread when clicked."""
        return lambda: self.host.post(getattr(self.host, name))

    def menu(self) -> list[dict[str, Any]]:
        """The menu as data: ``{"label", "action", "enabled", "default"}``.
        A ``None`` action is a separator or a line of text. Start and Stop
        are never both there: only the one that can be pressed."""
        idle = self.busy is None
        items: list[dict[str, Any]] = [
            {"label": self.status_line(), "action": None, "enabled": False},
            {"label": "-", "action": None},
            {"label": "Show Ninaivu", "action": self._on_tk("show_window"), "enabled": True,
             "default": True},
            {"label": "Open the family app", "action": self._on_tk("open_family"), "enabled": idle},
            {"label": "-", "action": None},
        ]
        if self.running:
            items.append({"label": "Stop Ninaivu", "action": self._on_tk("stop"), "enabled": idle})
        else:
            items.append({"label": "Start Ninaivu", "action": self._on_tk("start"), "enabled": idle})
        items += [{"label": "-", "action": None},
                  {"label": "Quit (Ninaivu keeps running)" if self.running else "Quit",
                   "action": self._on_tk("quit"), "enabled": True}]
        return items


class TrayIcon:
    """The pystray icon :func:`attach` made, and what the panel does with it.

    :meth:`refresh` and :meth:`close` are called on Tk's thread only.
    """

    def __init__(self, icon: Any, menu: PanelTray, *, shown: bool, platform: str) -> None:
        self.icon = icon
        self.menu = menu
        #: Whether there is an icon to find the window in again. Closing the
        #: window minimises it there only when there is.
        self.shown = shown
        self.platform = platform
        self.told_once = False

    def refresh(self, running: bool, busy: str | None) -> None:
        if self.menu.update(running, busy):
            try:
                self.icon.update_menu()
            except Exception:                              # noqa: BLE001 — a menu, not the window
                log.debug("could not update the tray menu", exc_info=True)

    def notify(self, message: str) -> None:
        """A small notification, where the icon has them; never waited for
        (on a Mac it is an osascript)."""
        if not getattr(self.icon, "HAS_NOTIFICATION", False):
            return

        def say() -> None:
            try:
                self.icon.notify(message, "Ninaivu")
            except Exception:                              # noqa: BLE001
                log.debug("could not show a notification", exc_info=True)
        threading.Thread(target=say, name="ninaivu-tray-notify", daemon=True).start()

    def close(self) -> None:
        """Take the icon away. The server is not touched."""
        try:
            if self.platform == "darwin":
                self.icon.visible = False
            self.icon.stop()
        except Exception:                                  # noqa: BLE001
            log.debug("could not take the tray icon away", exc_info=True)


def pystray_items(pystray: Any, menu: PanelTray) -> tuple:
    """*menu* as pystray's items, read afresh each time pystray asks."""
    entries = []
    for item in menu.menu():
        if item["label"] == "-":
            entries.append(pystray.Menu.SEPARATOR)
            continue
        action = item["action"]
        entries.append(pystray.MenuItem(
            item["label"], (lambda act: (lambda icon_, item_: act()))(action) if action else None,
            enabled=item.get("enabled", True), default=item.get("default", False)))
    return tuple(entries)


def attach(host: Any, platform: str | None = None) -> TrayIcon | None:
    """Put Ninaivu's icon in the tray for *host*, the panel, arranged for
    this system (the module's docstring says how and why); None when this
    computer has no tray Ninaivu can use. Called on Tk's thread, which on a
    Mac is the main thread, after the window exists."""
    platform = platform or sys.platform
    if platform.startswith("linux"):
        # Set before pystray is imported: it picks its backend then.
        os.environ.setdefault("PYSTRAY_BACKEND", "xorg")
    try:
        import pystray
        from PIL import Image
    except Exception:                                      # noqa: BLE001 — ImportError, or no display
        log.info("no tray icon: pystray cannot run here", exc_info=True)
        return None
    menu = PanelTray(host)
    try:
        icon = pystray.Icon("ninaivu", Image.open(ICON), "Ninaivu",
                            menu=pystray.Menu(lambda: pystray_items(pystray, menu)))
        if platform == "darwin":
            # Joins the NSApplication Tk's loop already runs. The setup does
            # nothing: shown here, on the main thread, and not on pystray's
            # setup thread, which would touch AppKit from another thread.
            icon.run_detached(setup=lambda _icon: None)
            icon.visible = True
            shown = True
        else:
            # Its own loop on its own thread, a daemon so a Ninaivu that
            # ends without Quit is not kept alive by it (pystray's own
            # run_detached makes one that is not, and on X11 starts two
            # setup threads, one of which waits for ever).
            threading.Thread(target=icon.run, name="ninaivu-tray", daemon=True).start()
            shown = platform == "win32" or x_tray_present(icon)
    except Exception:                                      # noqa: BLE001 — the window still works
        log.warning("could not put Ninaivu in the tray", exc_info=True)
        return None
    return TrayIcon(icon, menu, shown=shown, platform=platform)


def x_tray_present(icon: Any) -> bool:
    """Whether this X display has a system tray to dock in (an owner of
    ``_NET_SYSTEM_TRAY_S<screen>``). GNOME has none without an extension;
    Cinnamon, MATE, Xfce and KDE do."""
    try:
        import Xlib.X
        return icon._display.get_selection_owner(icon._net_system_tray_sx) != Xlib.X.NONE   # noqa: SLF001
    except Exception:                                      # noqa: BLE001 — not sure: no tray
        return False


class Tray:
    """The fuller menu and its actions, with no toolkit in sight: the whole
    program where there is no Tk (:func:`run_alone`), and the steps of
    trusting the certificate, which the panel's button runs too.

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
        ]
        if self.serves_https():
            # Only while Ninaivu runs on HTTPS: on HTTP there is nothing to trust.
            items.append({"label": "Trust the HTTPS certificate",
                          "action": self.trust_certificate, "enabled": idle})
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
        self.open_url(self.controller.local_url(admin=False))

    def open_console(self) -> None:
        self.open_url(self.controller.local_url(admin=True))

    def view_log(self) -> None:
        path = self.controller.logs / "server.log"
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

    def serves_https(self) -> bool:
        """Running on HTTPS with Ninaivu's own certificate."""
        try:
            record = self.controller.record()
        except Exception:                                    # noqa: BLE001
            return False
        args = self.controller.settings.get("arguments", [])
        custom = any(a == "--cert" or str(a).startswith("--cert=") for a in args)
        return bool(record) and record.get("scheme") == "https" and not custom

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
                    subprocess.Popen(["open", "-R", str(path)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    else:
        # Started, not waited for: xdg-open waits for the program it opens
        # with on some desktops (an editor, a terminal), and the tray's menu
        # waited with it.
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


# -- without Tk: the menu as the whole program ---------------------------

def main(argv: list[str] | None = None) -> int:
    """``python -m ninaivu.desktop.tray``: Ninaivu opened minimised to its
    icon, the one desktop program (:func:`ninaivu.desktop.app.main`)."""
    from . import app
    if app.tk is None:
        return run_alone()
    return app.main(["--tray", *(sys.argv[1:] if argv is None else argv)])


def run_alone() -> int:
    """The tray's fuller menu (:class:`Tray`) as the whole program, for a
    Python without Tk: there is no window to minimise, and the person still
    has Start, Stop and the rest from the icon."""
    try:
        import pystray
        from PIL import Image
    except Exception:                                      # noqa: BLE001 — ImportError, or no display
        print("The tray needs pystray and Pillow: pip install -r requirements/requirements-desktop.txt",
              file=sys.stderr)
        return 2

    from .portable import settle_quietly
    settle_quietly()
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
