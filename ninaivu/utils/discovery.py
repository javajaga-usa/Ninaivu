"""Announcing a name on the home network, so nobody has to remember an address.

``http://192.168.1.24`` is a fine thing for a computer and a terrible thing to
ask a family to type. This module publishes a name over mDNS — the
zero-configuration protocol Apple calls Bonjour — so the same library answers
to ``http://ninaivu.local`` on every device in the house, with nothing to
configure on the router and nothing to install on the phones. (The family
app's default port, 80, is the standard HTTP one, so it needs no port typed
either — see the family/admin split below for the one that does.)

Two records are published:

*A hostname*, so ``ninaivu.local`` resolves to this machine's LAN address. This
is the one that makes the URL work. The admin console answers to its own
second hostname by default — ``ninaivu-admin.local`` — so the name alone hints
at which app you are opening; pass the same label for both to go back to one
shared hostname instead.

*Two ``_http._tcp`` services*, so Ninaivu also turns up in anything that browses
for services on the network — some TVs, file managers and network scanners
list it by name.

The name follows the machine, not the address: pick up the laptop, move to a
different subnet, and the name still works because the answer is recomputed
from whatever address it has now.

**When it will not work.** mDNS is answered by the device asking, not by your
router, so a device has to speak it. Apple devices always do, Windows 10 and
later do, most Linux desktops do through Avahi, and Android's support is
patchy below Android 12. Anything that misses out can still use the plain IP
address, and the alternative that always works — a static DNS entry on the
router — is covered in the README.

This is an optional feature: without the ``zeroconf`` package Ninaivu says so
once and carries on serving by address.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
import uuid
from typing import Any

__all__ = ["Announcement", "advertise", "available", "filter_reachable_addresses", "normalise_name"]

#: A DNS label: letters, digits and hyphens, not starting or ending with one.
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def available() -> bool:
    """Whether the optional dependency is installed."""
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        return False
    return True


def normalise_name(name: str) -> str:
    """Turn what somebody typed into a usable ``.local`` label.

    Accepts ``Ninaivu``, ``ninaivu.local`` or ``ninaivu.local.`` and returns
    ``ninaivu``. Raises :class:`ValueError` for anything mDNS cannot carry, so
    a bad name fails at startup with an explanation rather than silently not
    resolving later.
    """
    label = (name or "").strip().rstrip(".").lower()
    if label.endswith(".local"):
        label = label[: -len(".local")]
    if not label:
        raise ValueError("A name cannot be empty.")
    if not _LABEL.match(label):
        raise ValueError(
            f"“{name}” cannot be used on the network. Use letters, digits and "
            f"hyphens only — for example: ninaivu, family-photos, upstairs-pc."
        )
    return label


def claim_rank(started: Any, instance: Any) -> tuple[float, str] | None:
    """Order two runs by who started last; None for a run that does not say.

    The instance id only breaks an exact tie, so that of two runs started in
    the same instant exactly one yields rather than both or neither.
    """
    try:
        return float(started), str(instance or "")
    except (TypeError, ValueError):
        return None


def _properties(info: Any) -> dict[str, Any]:
    """A service record's properties, as text."""
    return {(k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in (info.properties or {}).items()}


def filter_reachable_addresses(addresses: list[str]) -> list[str]:
    """Filter out internal virtual switch addresses (WSL/Hyper-V/Docker) when real LAN addresses exist.

    A Windows box with WSL or Hyper-V running has internal virtual switch adapters
    with no default gateway. If mDNS advertises them, other devices on the LAN
    attempt to connect to the unreachable internal IP and time out.
    """
    try:
        from . import netinfo
        gateways = netinfo.gateway_addresses()
    except Exception:
        gateways = []

    if gateways:
        reachable = [a for a in addresses if a in gateways]
        if reachable:
            return reachable
    return addresses


class Announcement:
    """A live mDNS registration. Close it to take the name(s) off the network."""

    def __init__(self, name: str, addresses: list[str],
                 hostnames: dict[str, str] | None = None) -> None:
        self.name = name
        #: The family app's hostname — kept as a plain attribute for callers
        #: (and tests) that only ever dealt with one name.
        self.hostname = f"{name}.local"
        #: role ("family", "admin") -> hostname. Both roles point at
        #: ``self.hostname`` unless ``advertise()`` was given ``admin_name``.
        self.hostnames = hostnames or {"family": self.hostname, "admin": self.hostname}
        self.addresses = addresses
        self._zc: Any = None
        self._services: list[Any] = []
        self._stop_watcher = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        #: When this run started and who it is, published so that another
        #: Ninaivu claiming the same hostname can tell which of them is newer.
        self.started = time.time()
        self.instance = uuid.uuid4().hex
        #: hostname -> addresses of the newer run it was given up to.
        self.yielded: dict[str, list[str]] = {}
        #: hostname -> this run's records withdrawn for it, to put back when
        #: the run it was given up to has gone.
        self._given_up: dict[str, list[Any]] = {}
        #: hostname -> the run it was given up to: its record, rank, addresses,
        #: port, and how many checks in a row it has not answered.
        self._claimants: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._claim_browser: Any = None
        self._claim_queue: Any = None
        self._claim_thread: threading.Thread | None = None

    def update_addresses(self, addresses: list[str]) -> bool:
        """Update advertised addresses if they have changed. Return True if updated."""
        filtered = filter_reachable_addresses(addresses)
        packed = [socket.inet_aton(a) for a in filtered if _is_ipv4(a)]
        if not packed or filtered == self.addresses:
            return False
        if self._zc is None:
            self.addresses = filtered
            return True

        from zeroconf import ServiceInfo
        updated_services = []
        with self._lock:
            try:
                for old_info in self._services:
                    new_info = ServiceInfo(
                        old_info.type,
                        old_info.name,
                        addresses=packed,
                        port=old_info.port,
                        properties=old_info.properties,
                        server=old_info.server,
                    )
                    self._zc.update_service(new_info)
                    updated_services.append(new_info)
                self._services = updated_services
                self.addresses = filtered
                return True
            except Exception:
                return False

    def watch_for_changes(self, address_getter: Any, interval: float = 15.0) -> None:
        """Start a background daemon thread checking for LAN IP address changes."""
        if self._watcher_thread is not None:
            return

        def _worker() -> None:
            reported = None
            while not self._stop_watcher.wait(interval):
                try:
                    latest = address_getter()
                    if latest:
                        self.update_addresses(latest)
                    reported = None
                except Exception as exc:          # noqa: BLE001 - keep watching
                    # Once per distinct failure, not every fifteen seconds: a
                    # name left pointing at an old address is otherwise silent.
                    if repr(exc) != reported:
                        reported = repr(exc)
                        logging.getLogger(__name__).warning(
                            "could not refresh the address behind %s: %s",
                            self.hostname, exc)

        self._watcher_thread = threading.Thread(
            target=_worker, name="ninaivu-mdns-watcher", daemon=True)
        self._watcher_thread.start()

    # -- the newest run keeps the name ----------------------------------------
    #
    # mDNS checks a *service* name for conflicts but never a hostname, so two
    # Ninaivus on one network both answer for ``ninaivu.local`` and a phone gets
    # whichever replies first -- or whatever it cached from a machine that has
    # since been switched off. Timing cannot settle it: an old run answering a
    # query looks, on the wire, exactly like a new run announcing itself. So
    # each run says when it started, in its service record, and an older run
    # that sees a newer one on its hostname steps back. A Ninaivu too old to
    # say when it started is never yielded to, and cannot be made to yield;
    # the newest run's own start-up announcement still flushes it from caches.
    #
    # A run takes a name it gave up back only once the newer run has gone:
    # withdrawn its records, or stopped answering at its address. Stepping
    # back for good left the name answering nothing: another computer switched
    # on for an evening took ninaivu.local from the Mac serving the library,
    # was switched off, and every phone lost the family app until the Mac was
    # restarted. The two cannot trade the name: the newer run never yields to
    # the older, and the older steps back again as soon as the newer is seen.

    #: How often a run that gave a name up asks whether the newer run is still
    #: answering, and how many checks in a row it must miss.
    RECHECK_SECONDS = 30.0
    RECLAIM_AFTER_MISSES = 3

    def watch_for_claims(self, service_types: list[str]) -> None:
        """Browse for other Ninaivus and give up any hostname a newer run uses."""
        if self._zc is None or self._claim_browser is not None:
            return
        import queue
        from zeroconf import ServiceBrowser, ServiceStateChange

        seen: "queue.Queue[tuple[str, str, str] | None]" = queue.Queue()

        def on_change(zeroconf: Any, service_type: str, name: str,
                      state_change: Any) -> None:
            # Called on zeroconf's own thread, which must not block on a
            # lookup; hand the name to ours instead.
            if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
                seen.put(("seen", service_type, name))
            elif state_change == ServiceStateChange.Removed:
                seen.put(("gone", service_type, name))

        def worker() -> None:
            while not self._stop_watcher.is_set():
                try:
                    item = seen.get(timeout=self.RECHECK_SECONDS)
                except queue.Empty:
                    item = ("check", "", "")
                if item is None:
                    return
                event, service_type, name = item
                try:
                    if event == "seen":
                        self._consider_claim(service_type, name)
                    elif event == "gone":
                        self._claimant_left(service_type, name)
                    else:
                        self._check_claimants()
                except Exception:                # noqa: BLE001 - never take the server down
                    pass

        self._claim_queue = seen
        self._claim_thread = threading.Thread(target=worker, name="ninaivu-mdns-claims",
                                              daemon=True)
        self._claim_thread.start()
        self._claim_browser = ServiceBrowser(self._zc, service_types, handlers=[on_change])

    def _consider_claim(self, service_type: str, name: str) -> None:
        zc = self._zc
        if zc is None:
            return
        info = zc.get_service_info(service_type, name, timeout=3000)
        if info is None or not info.server:
            return
        props = _properties(info)
        if props.get("instance") == self.instance:
            return
        theirs = claim_rank(props.get("started"), props.get("instance"))
        if theirs is None or theirs <= (self.started, self.instance):
            return
        host = info.server.rstrip(".").lower()
        # Both runs publish an address for the same hostname, so a lookup of the
        # newer one returns this machine's address too; report only theirs.
        addresses = []
        for packed in info.addresses or []:
            try:
                address = socket.inet_ntoa(packed)
            except OSError:
                continue
            if address not in self.addresses and address not in addresses:
                addresses.append(address)
        claimant = {"key": (service_type, name), "rank": theirs,
                    "addresses": addresses, "port": int(info.port or 0), "misses": 0}
        with self._lock:
            known = self._claimants.get(host)
            if known is not None:
                # Already given up. A run newer still is the one to watch.
                if theirs > known["rank"]:
                    self._claimants[host] = claimant
                    self.yielded[host] = addresses
                return
            mine = [s for s in self._services if s.server.rstrip(".").lower() == host]
            if not mine:
                return
            for service in mine:
                try:
                    zc.unregister_service(service)
                except Exception:                    # noqa: BLE001
                    pass
            self._services = [s for s in self._services if s not in mine]
            self._given_up[host] = mine
            self._claimants[host] = claimant
            self.yielded[host] = addresses
        logging.getLogger(__name__).warning(
            "%s is now answered by a Ninaivu started later%s; this one has stepped "
            "back from the name and is still reachable at %s",
            host, f" at {', '.join(addresses)}" if addresses else "",
            ", ".join(self.addresses))

    @staticmethod
    def _still_answering(claimant: dict[str, Any]) -> bool | None:
        """Whether the run a name was given up to still accepts a connection.

        None when that cannot be asked: it gave no address of its own (a second
        run on this machine) or no port. Then only its withdrawing its records
        gives the name back.
        """
        port = claimant.get("port") or 0
        if not claimant.get("addresses") or not port:
            return None
        for address in claimant["addresses"]:
            try:
                with socket.create_connection((address, port), timeout=2.0):
                    return True
            except OSError:
                continue
        return False

    def _check_claimants(self) -> None:
        """Take back each name whose newer run has stopped answering."""
        with self._lock:
            watched = list(self._claimants.items())
        for host, claimant in watched:
            answering = self._still_answering(claimant)
            if answering is None:
                continue
            if answering:
                claimant["misses"] = 0
                continue
            claimant["misses"] += 1
            if claimant["misses"] >= self.RECLAIM_AFTER_MISSES:
                self._take_back(host, "no longer answers")

    def _claimant_left(self, service_type: str, name: str) -> None:
        """The newer run withdrew its record, or it expired: take the name back
        unless it is still answering at its address."""
        with self._lock:
            watched = [(host, c) for host, c in self._claimants.items()
                       if c["key"] == (service_type, name)]
        for host, claimant in watched:
            answering = self._still_answering(claimant)
            if answering:
                continue
            # Two runs can publish under the same service name, and this run's
            # own goodbye, when it stepped back, is a removal of that name too.
            # While the newer run still publishes its record it is there,
            # whatever one connection attempt said: a port that was slow to
            # answer once used to hand the name straight back, and the two
            # runs traded it — the very thing RECLAIM_AFTER_MISSES exists to
            # stop. One miss is for _check_claimants to count.
            if self._record_still_there(claimant):
                continue
            self._take_back(host, "has gone")

    def _record_still_there(self, claimant: dict[str, Any]) -> bool:
        zc = self._zc
        if zc is None:
            return False
        info = zc.get_service_info(*claimant["key"], timeout=1500)
        if info is None:
            return False
        props = _properties(info)
        return claim_rank(props.get("started"), props.get("instance")) == claimant["rank"]

    def _take_back(self, host: str, why: str) -> None:
        """Announce this run's records for *host* again, at today's address."""
        zc = self._zc
        if zc is None:
            return
        from zeroconf import ServiceInfo
        packed = [socket.inet_aton(a) for a in self.addresses if _is_ipv4(a)]
        with self._lock:
            withdrawn = self._given_up.pop(host, [])
            claimant = self._claimants.pop(host, None)
            self.yielded.pop(host, None)
            for old in withdrawn:
                info = ServiceInfo(old.type, old.name, addresses=packed, port=old.port,
                                   properties=old.properties, server=old.server)
                try:
                    zc.register_service(info, allow_name_change=True)
                except Exception:                    # noqa: BLE001
                    continue
                self._services.append(info)
        theirs = ", ".join((claimant or {}).get("addresses") or [])
        logging.getLogger(__name__).info(
            "%s: the Ninaivu that took the name%s %s; this one answers for it again at %s",
            host, f" at {theirs}" if theirs else "", why, ", ".join(self.addresses))

    def close(self) -> None:
        """Withdraw the records, so the name does not linger in caches."""
        self._stop_watcher.set()
        if self._claim_browser is not None:
            try:
                self._claim_browser.cancel()
            except Exception:                    # noqa: BLE001 - shutting down
                pass
            self._claim_browser = None
        if self._claim_queue is not None:
            self._claim_queue.put(None)
        if self._claim_thread is not None:
            self._claim_thread.join(timeout=1.0)
            self._claim_thread = None
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=1.0)
            self._watcher_thread = None
        if self._zc is None:
            return
        try:
            for service in self._services:
                self._zc.unregister_service(service)
        except Exception:                        # noqa: BLE001 — shutting down
            pass
        try:
            self._zc.close()
        except Exception:                        # noqa: BLE001
            pass
        self._zc = None
        self._services = []

    def __enter__(self) -> "Announcement":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def advertise(name: str, ports: dict[str, int], addresses: list[str],
              scheme: str = "http", admin_name: str | None = None,
              watch_fn: Any = None) -> Announcement | None:
    """Publish *name*``.local`` pointing at this machine. None if unavailable.

    ``ports`` maps a label ("family", "admin") to its port; each becomes a
    browsable service. By default every role shares one hostname, so
    ``http://ninaivu.local:5000`` (family) and ``http://ninaivu.local:3000``
    (admin) both resolve — or, on the family app's default port (80, the
    standard HTTP one), no port at all: plain ``http://ninaivu.local``.
    Passing ``admin_name`` gives the admin console a *second*, distinct
    hostname instead (``ninaivu-admin.local``) — a port not on its scheme's
    default (3000 is not 80) still has to be typed for it either way; this
    only changes which name resolves to it, so someone can tell the two apart
    by name as well as by port.
    """
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError:
        return None

    if not addresses:
        return None

    label = normalise_name(name)
    admin_label = normalise_name(admin_name) if admin_name is not None else label
    hostnames = {"family": f"{label}.local", "admin": f"{admin_label}.local"}
    filtered_addresses = filter_reachable_addresses(addresses)
    announcement = Announcement(label, filtered_addresses, hostnames)
    packed = [socket.inet_aton(a) for a in filtered_addresses if _is_ipv4(a)]
    if not packed:
        return None

    try:
        zc = Zeroconf(ip_version=IPVersion.V4Only)
    except OSError:
        # No multicast on this interface; not fatal, just no name.
        return None

    announcement._zc = zc
    service_type = f"_{scheme}._tcp.local."
    try:
        for role, port in ports.items():
            server_label = admin_label if role == "admin" else label
            info = ServiceInfo(
                service_type,
                f"Ninaivu {role}.{service_type}",
                addresses=packed,
                port=int(port),
                properties={
                    "path": "/",
                    "role": role,
                    "scheme": scheme,
                    "started": f"{announcement.started:.6f}",
                    "instance": announcement.instance,
                },
                # family and admin share this hostname unless admin_name was
                # given, in which case each role gets its own A record.
                server=f"{server_label}.local.",
            )
            zc.register_service(info, allow_name_change=True)
            announcement._services.append(info)
    except Exception:                            # noqa: BLE001
        announcement.close()
        return None

    if watch_fn is not None:
        announcement.watch_for_changes(watch_fn)
    announcement.watch_for_claims([service_type])

    return announcement


def _is_ipv4(address: str) -> bool:
    try:
        socket.inet_aton(address)
    except OSError:
        return False
    return address.count(".") == 3
