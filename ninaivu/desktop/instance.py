"""One Ninaivu on the desktop: opening it again shows the one already open.

Ninaivu is one program on the computer it runs on: the Control Panel's
window, with its icon in the tray (or the menu bar), which is the same
program with the window put away. Double-clicking Ninaivu while it is
already open, with the window hidden in the tray, must bring that window
back, not open a second panel beside it: two panels, each with its own
Start button and its own idea of what is under way, is the reasoning about
which control to press that Ninaivu is meant to spare a household.

So the first one opened holds a lock in its run-time folder (the
``.ninaivu-control`` beside the program, or the portable folder's
``data/control``) for as long as it runs, and listens on a port of this
computer's own (127.0.0.1) that it writes beside the lock with a token. A
second one that cannot take the lock tells the first to show itself, with
that token, and ends. A desktop shortcut to the family app or the console
(:mod:`desktop.shortcuts`) opens the program the same way with ``--open``,
and a second one passes that on instead: the first opens the page. The lock is the operating system's, so it goes when
the program does, however it ends: there is no stale file to clear by hand.

Each folder has its own: a newer portable copy unpacked beside an older one
is a different Ninaivu, and the newer one's panel opening the older one's
(``desktop/portable.py`` :func:`~ninaivu.desktop.portable.launch`) is how a
library is given back.

When the first does not answer — it is still drawing its window, or it has
hung — the second waits a few seconds and then opens anyway. Two panels is
how it was before this, and harmless (both read the same run file); a
double-click that did nothing at all would not be.

Standard library only, and nothing here touches Tk: :func:`claim` is called
before any window exists, and *on_show* is called on a thread of its own, so
the panel hands it to Tk's thread through its queue.
"""
from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Callable

#: Held for as long as the desktop program runs.
LOCK = 'desktop.lock'
#: Where the program holding the lock listens, and the token it expects.
ADDRESS = 'desktop.json'
#: What a second Ninaivu sends, and what it is answered.
SHOW, SHOWN = b'show', b'shown'
#: What a second Ninaivu opened by a shortcut sends instead: open that page.
OPEN = (b'family', b'console')
#: How long a second Ninaivu waits for the first to answer before opening anyway.
PATIENCE = 6.0


class Instance:
    """The lock and the listener of the Ninaivu that holds them."""

    def __init__(self, folder: Path, handle, listener: socket.socket, token: str) -> None:
        self.folder = folder
        self._handle = handle
        self._listener = listener
        self._token = token.encode('ascii')
        self._on_show: Callable[[], None] | None = None
        self._on_open: Callable[[str], None] | None = None
        self._closed = threading.Event()

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1]

    def serve(self, on_show: Callable[[], None], on_open: Callable[[str], None] | None = None) -> None:
        """Answer the next Ninaivu opened, on a thread of its own: *on_show*
        for each one that brings the right token, or *on_open* with
        ``'family'`` or ``'console'`` for one opened to show that page."""
        self._on_show = on_show
        self._on_open = on_open
        threading.Thread(target=self._accept, name='ninaivu-desktop-instance', daemon=True).start()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return                                     # closed
            with connection:
                try:
                    connection.settimeout(2.0)
                    said = _read_line(connection, 2)
                    if len(said) != 2 or not hmac.compare_digest(said[0], self._token):
                        continue
                    if said[1] != SHOW and said[1] not in OPEN:
                        continue
                    connection.sendall(SHOWN + b'\n')
                    if said[1] in OPEN and self._on_open is not None:
                        self._on_open(said[1].decode('ascii'))
                    elif self._on_show is not None:
                        self._on_show()
                except (OSError, ValueError):
                    continue

    def close(self) -> None:
        """Let the next Ninaivu opened be the one. Called when the program
        ends; the operating system does the same if it never is."""
        if self._closed.is_set():
            return
        self._closed.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        with contextlib.suppress(OSError):
            os.remove(self.folder / ADDRESS)
        with contextlib.suppress(OSError):
            _unlock(self._handle)
        with contextlib.suppress(OSError):
            self._handle.close()


def claim(folder: Path, *, patience: float = PATIENCE, sleep=time.sleep,
          say: bytes = SHOW) -> Instance | None:
    """This program as the Ninaivu for *folder*, or None when another
    already is and has been told to show its window (*say*: or to open
    the page named, one of :data:`OPEN`).

    When another holds the lock but does not answer within *patience*
    seconds, this one becomes a second panel rather than leave the person
    with nothing (the module's docstring says why); it then holds no lock,
    so it answers nobody either.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / LOCK, 'a+b')                    # noqa: SIM115 — held while the program runs
    if _lock(handle):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        token = secrets.token_hex(16)
        _write_address(folder, listener.getsockname()[1], token)
        return Instance(folder, handle, listener, token)
    handle.close()
    deadline = time.monotonic() + patience
    while True:
        if tell_to_show(folder, say=say):
            return None
        if time.monotonic() >= deadline:
            break
        sleep(0.25)
    # Not answering: open anyway, with a listener nobody is told about.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    return Instance(folder, _Unlocked(), listener, secrets.token_hex(16))


def tell_to_show(folder: Path, timeout: float = 2.0, say: bytes = SHOW) -> bool:
    """Ask the Ninaivu holding *folder*'s lock to show its window (or to
    open the page *say* names); whether it said it would."""
    try:
        told = json.loads((Path(folder) / ADDRESS).read_text(encoding='utf-8'))
        port, token = int(told['port']), str(told['token'])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout) as connection:
            connection.sendall(token.encode('ascii') + b'\n' + say + b'\n')
            return _read_line(connection, 1) == [SHOWN]
    except (OSError, ValueError):
        return False


def _read_line(connection: socket.socket, lines: int) -> list[bytes]:
    """Up to *lines* lines from *connection*, each at most 128 bytes."""
    data = b''
    while data.count(b'\n') < lines and len(data) < 128 * lines:
        chunk = connection.recv(256)
        if not chunk:
            break
        data += chunk
    return data.split(b'\n')[:lines] if data.count(b'\n') >= lines else []


def _write_address(folder: Path, port: int, token: str) -> None:
    """The port and token, readable by this account alone: whoever has the
    token can bring the window up, and nothing else."""
    target = folder / ADDRESS
    temporary = folder / (ADDRESS + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
        json.dump({'port': port, 'token': token, 'pid': os.getpid()}, out)
    os.replace(temporary, target)


class _Unlocked:
    """The lock of a second panel, which holds none."""

    def close(self) -> None:
        pass


def _lock(handle) -> bool:
    """Take the lock on *handle* without waiting; whether it was free."""
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    if isinstance(handle, _Unlocked):
        return
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
