"""The always-on Secondary waking the Primary for its nightly catch-up.

A Mac that sleeps does no work at night unless something wakes it. macOS can
wake itself on a schedule (``pmset repeat``), but that needs Terminal and an
administrator's password, and it is one more thing to remember. With two
computers there is a simpler way: the Secondary is always on, so it sends the
Mac a Wake-on-LAN packet a few minutes before the catch-up's stretch opens.

* **The Primary** says where it is: the hardware addresses of its network
  connections and when it wants waking (five minutes before
  ``daily_catchup_at``, only while the daily catch-up is on). It tells the
  Secondary on every run, over the pairing (``PUT /api/mugil/wake``), so a
  change of time reaches it by the next run, and turning the catch-up off
  stops the waking.
* **The Secondary** (:class:`Waker`) sends the packet once a day at that time,
  to every network it is on, and the console has a button to send it now.

The Mac answers it when *Wake for network access* is on (System Settings,
Energy), which the Mugil checklist reads. Nothing here runs with one computer.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import SECONDARY, pairing, role

log = logging.getLogger(__name__)

#: In the Secondary's state folder: whom to wake, and when.
RECORD = "mugil-wake.json"
#: In the Primary's state folder: what the Secondary was last told.
TOLD = "mugil-wake-told.json"
#: How long before the catch-up's stretch the Mac is woken.
EARLY_MINUTES = 5
#: A wake missed by more than this (the Secondary was off) waits for the next day.
LATE_MINUTES = 30
#: Wake-on-LAN's own port; 7 is the other one some cards listen on.
PORTS = (9, 7)

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def clean_mac(text: str) -> str | None:
    mac = str(text or "").strip().lower().replace("-", ":")
    return mac if _MAC.match(mac) and mac != "00:00:00:00:00:00" else None


def magic_packet(mac: str) -> bytes:
    """Six 0xFF, then the address sixteen times."""
    raw = bytes.fromhex(mac.replace(":", ""))
    return b"\xff" * 6 + raw * 16


def hardware_addresses() -> list[str]:
    """This computer's network hardware addresses, of the connections that
    have an IPv4 address: those are the ones the house network reaches."""
    try:
        import psutil                                              # noqa: PLC0415
        found: set[str] = set()
        for name, addresses in psutil.net_if_addrs().items():
            if name.startswith(("lo", "utun", "awdl", "llw", "bridge", "docker", "veth", "tailscale")):
                continue
            if not any(a.family == socket.AF_INET for a in addresses):
                continue
            for address in addresses:
                if address.family == psutil.AF_LINK:
                    mac = clean_mac(address.address)
                    if mac:
                        found.add(mac)
        return sorted(found)
    except Exception:                                             # noqa: BLE001 - nice to have
        return []


def wanted_at(cfg) -> str:
    """When the Primary wants waking, "HH:MM", or "" for never."""
    if not getattr(cfg, "daily_catchup", False):
        return ""
    try:
        hour, minute = (int(x) for x in str(cfg.daily_catchup_at).split(":", 1))
    except ValueError:
        return ""
    total = (hour * 60 + minute - EARLY_MINUTES) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def what_to_ask(cfg) -> dict[str, Any]:
    """The Primary's request, as it is sent to the Secondary."""
    return {"macs": hardware_addresses(), "at": wanted_at(cfg)}


def remember_told(state_dir: str | Path, asked: dict[str, Any]) -> None:
    """On the Primary: what the Secondary took, for the Mugil checklist."""
    path = Path(state_dir) / TOLD
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({**asked, "told_at": time.time()}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("Could not note the wake: %s", exc)


def told(state_dir: str | Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(state_dir) / TOLD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# -- on the Secondary -----------------------------------------------------------

def load(state_dir: str | Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(state_dir) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(state_dir: str | Path, data: dict[str, Any]) -> None:
    path = Path(state_dir) / RECORD
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def remember(state_dir: str | Path, asked: dict[str, Any]) -> dict[str, Any]:
    """Keep what the Primary asked for; the record as kept."""
    macs = sorted({m for m in (clean_mac(x) for x in (asked.get("macs") or [])[:8]) if m})
    at = str(asked.get("at") or "")
    if at and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", at):
        at = ""
    data = {**load(state_dir), "macs": macs, "at": at, "asked_at": time.time()}
    _save(state_dir, data)
    return data


def forget(state_dir: str | Path) -> None:
    """Unpaired: neither computer keeps a wake for the other."""
    (Path(state_dir) / RECORD).unlink(missing_ok=True)
    (Path(state_dir) / TOLD).unlink(missing_ok=True)


def _broadcasts() -> list[str]:
    from .meeting import _broadcasts as broadcasts                 # noqa: PLC0415
    return broadcasts()


def send(macs: list[str], *, to: list[str] | None = None,
         sender: Callable[[bytes, tuple[str, int]], Any] | None = None) -> int:
    """Send the packet for each address to every network; how many went."""
    sock = None
    if sender is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sender = sock.sendto
    sent = 0
    try:
        for mac in macs:
            packet = magic_packet(mac)
            for address in to or _broadcasts():
                for port in PORTS:
                    try:
                        sender(packet, (address, port))
                        sent += 1
                    except OSError as exc:
                        log.debug("Could not send the wake to %s: %s", address, exc)
    finally:
        if sock is not None:
            sock.close()
    return sent


def wake_now(state_dir: str | Path, *, sender=None, now: float | None = None) -> int:
    data = load(state_dir)
    macs = data.get("macs") or []
    if not macs:
        return 0
    sent = send(macs, sender=sender)
    if sent:
        data["last_woken"] = now or time.time()
        _save(state_dir, data)
    return sent


def due(data: dict[str, Any], now: float) -> bool:
    """Whether today's wake is due at *now* and not sent yet."""
    at, macs = data.get("at"), data.get("macs")
    if not at or not macs:
        return False
    here = _dt.datetime.fromtimestamp(now)
    hour, minute = (int(x) for x in at.split(":"))
    when = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when > here:
        when -= _dt.timedelta(days=1)
    if (here - when).total_seconds() > LATE_MINUTES * 60:
        return False
    return float(data.get("last_woken") or 0) < when.timestamp()


class Waker:
    """On the Secondary: sends the Primary its wake at the time it asked."""

    LOOK_SECONDS = 30.0

    def __init__(self, cfg, *, sender=None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.sender = sender
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def wanted(self) -> bool:
        return role(self.cfg) == SECONDARY and pairing.load(self.cfg.state_dir) is not None

    def keep(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="mugil-wake", daemon=True)
            self._thread.start()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    def look(self) -> bool:
        """Send the wake if it is due; whether it was sent."""
        if not self.wanted():
            return False
        now = self.clock()
        if not due(load(self.cfg.state_dir), now):
            return False
        sent = wake_now(self.cfg.state_dir, sender=self.sender, now=now)
        if sent:
            log.info("Woke the Primary for its nightly catch-up")
        return bool(sent)

    def _loop(self) -> None:
        while not self._stop.wait(self.LOOK_SECONDS):
            try:
                self.look()
            except Exception:                                    # noqa: BLE001 - try again later
                log.exception("Could not wake the Primary")


__all__ = ["Waker", "magic_packet", "hardware_addresses", "wanted_at", "what_to_ask",
           "remember", "remember_told", "told", "load", "forget", "send", "wake_now", "due", "clean_mac"]
