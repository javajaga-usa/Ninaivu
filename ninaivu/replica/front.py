"""The Pi's family app: one address, read-only, and the mini when it is awake.

Installed on the family app only when this process is a replica
(``--replica-of``). Three rules, each a ``before_request`` after the app's
own (host names, sign-in, the screen lock):

**Send people to the mini while it is awake.** The family bookmarks the Pi.
A page they open there (``/``, a share link, a "who is this?" link) is sent
to the same page on the mini whenever the mini answered its last health
check, so they get the real thing with everything it can do. The check runs
on a thread of its own every :data:`PROBE_EVERY` seconds and never holds a
request up. ``?pi=1`` stays on the Pi, for that browser, until ``?pi=0``.

**Change nothing.** Every request that would change something is refused
with 503 and a sentence saying why, except signing in and out, the screen
lock, and asking the mini to wake. The library itself is read-only to the
Pi's kernel as well (the systemd unit mounts it so); this is the rule for
the index, whose copy here is replaced by the next hand-over anyway: a
caption typed on the Pi would be lost without anybody being told.

**Wake the mini when somebody needs it.** With the mini's network address
given (``--replica-wake``), a refused change, or ``POST /api/replica/wake``,
sends it a Wake-on-LAN packet, at most once a minute. The mini needs "Wake
for network access" on in its Energy settings.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from flask import Blueprint, Flask, jsonify, redirect, request

from ..server import auth
from ..words import refusal

log = logging.getLogger(__name__)

__all__ = ["Front", "install", "magic_packet", "OPEN_WRITES", "PROBE_EVERY", "STAY_COOKIE"]

#: Changes the Pi does make: they are its own sign-ins, not the library.
OPEN_WRITES = frozenset({
    "/api/auth/login", "/api/auth/enter", "/api/auth/active", "/api/auth/lock",
    "/api/auth/unlock", "/api/auth/logout", "/api/replica/wake",
})

#: The pages that are sent on to the mini while it is awake. The API, the
#: thumbnails and the static files are always answered here: a page already
#: open on the Pi carries on from the Pi.
PAGES = ("/", "/share/", "/ask/")

#: Seconds between looks at whether the mini is awake.
PROBE_EVERY = 20.0

#: Seconds a look may take before the mini counts as asleep.
PROBE_TIMEOUT = 3.0

#: Seconds between two wake-up packets.
WAKE_EVERY = 60.0

#: The cookie that keeps one browser on the Pi.
STAY_COOKIE = "ninaivu_on_pi"

_MAC = re.compile(r"^([0-9A-Fa-f]{2})([:-]?)([0-9A-Fa-f]{2})(\2[0-9A-Fa-f]{2}){4}$")


def magic_packet(mac: str) -> bytes:
    """The Wake-on-LAN packet for *mac*: six 0xFF bytes, then the address 16 times."""
    text = (mac or "").strip()
    if not _MAC.match(text):
        raise ValueError(f"{mac!r} is not a network (MAC) address like 3c:22:fb:12:34:56")
    raw = bytes.fromhex(re.sub(r"[:-]", "", text))
    return b"\xff" * 6 + raw * 16


class Front:
    """Whether the mini is awake, and waking it."""

    def __init__(self, cfg, replica=None, *, opener=None, sender=None) -> None:
        self.cfg = cfg
        self.replica = replica
        self._open = opener or urllib.request.urlopen
        self._send = sender or self._broadcast
        self._awake: bool | None = None
        self._looked_at = 0.0
        self._woken_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the mini ----------------------------------------------------------
    @property
    def primary(self) -> str:
        return str(getattr(self.cfg, "replica_primary_url", "") or "").strip().rstrip("/")

    def look(self) -> bool:
        """Ask the mini's health check now; remember and return the answer."""
        if not self.primary:
            self._awake = False
            return False
        try:
            with self._open(f"{self.primary}/healthz", timeout=PROBE_TIMEOUT) as answer:
                awake = 200 <= getattr(answer, "status", 200) < 300
        except (urllib.error.URLError, OSError, ValueError):
            awake = False
        if awake != self._awake and self._awake is not None:
            log.info("the Mac mini is %s", "awake: sending people to it" if awake
                     else "not answering: serving the Pi's copy")
        self._awake = awake
        self._looked_at = time.time()
        return awake

    @property
    def awake(self) -> bool:
        return bool(self._awake)

    def keep_looking(self) -> None:
        if not self.primary or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-replica-front",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self.look()
        while not self._stop.wait(PROBE_EVERY):
            self.look()

    def stop(self) -> None:
        self._stop.set()

    # -- waking it ---------------------------------------------------------
    @property
    def can_wake(self) -> bool:
        return bool(str(getattr(self.cfg, "replica_wake_mac", "") or "").strip())

    @staticmethod
    def _broadcast(packet: bytes) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, ("255.255.255.255", 9))

    def wake(self) -> bool:
        """Send the mini a wake-up packet, unless one went in the last minute.
        Returns whether one was sent now or recently."""
        if not self.can_wake:
            return False
        with self._lock:
            if time.time() - self._woken_at < WAKE_EVERY:
                return True
            try:
                self._send(magic_packet(self.cfg.replica_wake_mac))
            except (OSError, ValueError) as exc:
                log.warning("could not wake the Mac mini: %s", exc)
                return False
            self._woken_at = time.time()
            log.info("asked the Mac mini to wake up")
            return True

    # -- what the page is told ---------------------------------------------
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"replica": True, "primary_awake": self.awake,
                               "primary_url": self.primary, "can_wake": self.can_wake}
        if self.replica is not None:
            out.update({k: v for k, v in self.replica.status().items() if k != "error"})
        return out


def _is_page(path: str) -> bool:
    return path == "/" or any(path.startswith(p) for p in PAGES if p != "/")


def install(app: Flask, front: Front) -> None:
    """Put the three rules on *app* (the family app of a replica)."""
    app.config["MV_REPLICA_FRONT"] = front
    bp = Blueprint("replica", __name__)

    @bp.get("/api/replica")
    def replica_status():
        # The Mac's address and the hand-over's state are the family's to
        # know; a passer-by learns only that this is the Pi.
        user = auth.current_user()
        if not user.id or user.is_guest:
            return jsonify({"replica": True})
        return jsonify(front.status())

    @bp.post("/api/replica/wake")
    def replica_wake():
        user = auth.current_user()
        if not user.id or user.is_guest:
            return jsonify({"error": "Sign in to wake the Mac mini.", "status": 403}), 403
        if not front.can_wake:
            return jsonify({"woken": False, "error": "The Pi was not given the Mac mini's "
                            "network address (--replica-wake)."}), 409
        return jsonify({"woken": front.wake()})

    app.register_blueprint(bp)

    @app.before_request
    def _send_pages_to_the_mini():
        if request.method not in ("GET", "HEAD") or not _is_page(request.path):
            return None
        stay = request.args.get("pi")
        if stay in ("0", "1"):
            # Followed by the redirect below, or not, with the choice kept.
            args = request.args.to_dict(flat=False)
            args.pop("pi", None)
            query = urlencode(args, doseq=True)
            response = redirect(request.path + (f"?{query}" if query else ""), code=303)
            if stay == "1":
                response.set_cookie(STAY_COOKIE, "1", max_age=365 * 86400,
                                    samesite="Lax", httponly=True)
            else:
                response.delete_cookie(STAY_COOKIE)
            response.headers["Cache-Control"] = "no-store"
            return response
        if request.cookies.get(STAY_COOKIE) == "1" or not front.primary or not front.awake:
            return None
        target = front.primary + request.full_path.rstrip("?")
        response = redirect(target, code=307)
        # Never remembered: the next visit asks again, and the mini may be asleep.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def _change_nothing():
        if request.method in ("GET", "HEAD", "OPTIONS") or request.path in OPEN_WRITES:
            return None
        body = refusal("This is the copy of the library kept on the Pi while the Mac mini "
                       "sleeps. You can look at everything here; changes are made on the "
                       "mini.")
        user = auth.current_user()
        woken = bool(user.id) and not user.is_guest and front.wake()
        if woken:
            body["error"] += " The mini has been asked to wake up; try again in a minute."
        body.update({"status": 503, "replica": True, "waking": woken})
        response = jsonify(body)
        response.status_code = 503
        response.headers["Retry-After"] = "60"
        return response

    @app.context_processor
    def _replica_notice():
        made = front.replica.as_of if front.replica is not None else None
        return {"replica_notice": {
            "as_of": time.strftime("%Y-%m-%d %H:%M", time.localtime(made)) if made else "",
            "primary": front.primary,
        }}
