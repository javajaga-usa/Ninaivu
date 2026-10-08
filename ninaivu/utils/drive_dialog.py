"""On a Mac, a drive or phone plugged in is asked about on the Mac itself.

The console asks too (``static/js/drives.js``), but only while it is open in
a browser. Somebody who plugs a memory card into the Ninaivu computer is
standing at it, and expects the computer to say something, as Windows'
AutoPlay does. So the server watches the same :class:`utils.drives.Watcher`
and, for each drive that still needs the question, shows a small macOS
window (``osascript``'s ``display dialog``, nothing to install):

    Import…                         opens the console's Import page with the
                                    drive as its source
    Not now                         asked again when it is plugged in again
    Don't ask again                 remembered, as in the console

For a phone or camera, which a Mac cannot read as files, the window says so
and offers to open Image Capture.

Whichever is answered first wins: the console's answer closes the window, and
the window's answer closes the console's pop-up. Without a screen to show it
on (Ninaivu started over SSH, or as a background service) ``osascript`` fails,
and the watcher stops quietly; the console still asks.
``NINAIVU_DRIVE_DIALOG=0`` turns it off.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from typing import Callable

from .drives import Drive, Watcher

log = logging.getLogger(__name__)

#: How often the drives are looked at. A dialog a few seconds after the card
#: goes in is soon enough, and ioreg/diskutil are not free.
POLL_SECONDS = 4.0
#: An unanswered window goes away by itself after this long.
GIVE_UP_SECONDS = 600

IMPORT = "Import…"
NOT_NOW = "Not now"
NEVER = "Don't ask again"
IMAGE_CAPTURE = "Open Image Capture"


def wanted() -> bool:
    return (sys.platform == "darwin" and os.environ.get("NINAIVU_DRIVE_DIALOG", "1") != "0"
            and "PYTEST_CURRENT_TEST" not in os.environ)


def _quoted(text: str) -> str:
    """A string for AppleScript: backslashes and double quotes escaped."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def script_for(drive: Drive) -> str:
    """The AppleScript that asks about *drive*."""
    name = _quoted(drive.label or drive.path)
    if drive.kind == "phone" and not drive.readable:
        message = (f"{name} was connected.\\n\\nA Mac does not let Ninaivu read a phone "
                   "or camera over the cable. Open Image Capture, import the photos into "
                   "a folder, then add that folder on Ninaivu's Import page.")
        buttons = f'{{"{NEVER}", "{NOT_NOW}", "{IMAGE_CAPTURE}"}}'
        default = IMAGE_CAPTURE
    else:
        what = "phone" if drive.kind == "phone" else "drive"
        message = (f"A {what} was connected: {name}\\n\\nImport its photos and videos "
                   "into Ninaivu? The console's Import page opens with it as the source; "
                   "nothing is copied until you press Start.")
        buttons = f'{{"{NEVER}", "{NOT_NOW}", "{IMPORT}"}}'
        default = IMPORT
    return (f'display dialog "{message}" with title "Ninaivu" buttons {buttons} '
            f'default button "{default}" cancel button "{NOT_NOW}" '
            f"giving up after {GIVE_UP_SECONDS}")


def import_url(console_url: str, drive: Drive) -> str:
    query = urllib.parse.urlencode({"drive": drive.path, "do": "import"})
    return f"{console_url.rstrip('/')}/?{query}#archive"


class DriveDialog:
    """Asks about each new drive in a window on the Mac, one at a time."""

    def __init__(self, watcher: Watcher, console_url: str, *,
                 holds_library: Callable[[Drive], bool] = lambda d: False,
                 run: Callable[..., subprocess.Popen] = subprocess.Popen,
                 open_url: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.watcher = watcher
        self.console_url = console_url
        self.holds_library = holds_library
        self.run = run
        self.open_url = open_url or self._open
        self.sleep = sleep
        self.stopped = threading.Event()
        #: Drives already shown a window this time they are plugged in.
        self.shown: set[str] = set()
        self.thread: threading.Thread | None = None

    @staticmethod
    def _open(target: str) -> None:
        subprocess.run(["open", target], check=False, timeout=15, capture_output=True)

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, name="ninaivu-drive-dialog",
                                           daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def loop(self) -> None:
        while not self.stopped.is_set():
            try:
                if not self.once():
                    return
            except Exception:  # noqa: BLE001 — a nicety; never take the server down
                log.exception("drive dialog")
            self.stopped.wait(POLL_SECONDS)

    def next_drive(self) -> Drive | None:
        present = {d.id for d in self.watcher.drives()}
        self.shown &= present                       # taken out: may be shown again
        for drive in self.watcher.pending():
            if drive.id not in self.shown and not self.holds_library(drive):
                return drive
        return None

    def once(self) -> bool:
        """Ask about one drive, if one needs it. False once there is no screen
        to ask on, which ends the loop."""
        drive = self.next_drive()
        if drive is None:
            return True
        self.shown.add(drive.id)
        answer = self.ask(drive)
        if answer is None:
            return False
        self.act(drive, answer)
        return True

    def ask(self, drive: Drive) -> str | None:
        """The button pressed; "" when the window went unanswered or was
        answered elsewhere; None when no window could be shown."""
        try:
            proc = self.run(["osascript", "-e", script_for(drive)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError:
            log.info("drive dialog: osascript is not available; the console still asks")
            return None
        while proc.poll() is None:
            if self.stopped.is_set() or drive.id not in {d.id for d in self.watcher.pending()}:
                proc.terminate()                    # answered in the console, or pulled out
                proc.wait()
                return ""
            self.sleep(1.0)
        out, err = proc.communicate()
        if proc.returncode != 0:
            # -128 is the Not now button (the cancel button); anything else is
            # no window server: Ninaivu started where nobody can see a window.
            if "-128" in (err or ""):
                return NOT_NOW
            log.info("drive dialog: no window could be shown (%s); the console still asks",
                     (err or "").strip()[:200])
            return None
        for name in (IMPORT, IMAGE_CAPTURE, NEVER, NOT_NOW):
            if f"button returned:{name}" in (out or ""):
                return name
        return ""                                   # gave up: nobody was there

    def act(self, drive: Drive, answer: str) -> None:
        if answer == NEVER:
            self.watcher.never_ask(drive)
            return
        if not answer:
            return                                  # the console may still ask
        self.watcher.answer(drive.id)
        if answer == IMPORT:
            self.open_url(import_url(self.console_url, drive))
        elif answer == IMAGE_CAPTURE:
            subprocess.run(["open", "-a", "Image Capture"], check=False, timeout=15,
                           capture_output=True)
