"""The two computers finding each other, so nobody types an address or a code.

**The Secondary listens** (:class:`Beacon`). While this computer is the
Secondary and the family app is on the network, it answers a short question
on UDP port :data:`PORT`, "is there a Ninaivu Secondary here?", with its name,
the port its family app is on, and whether it is *waiting* to be paired.
Choosing the part opens the wait for half an hour (:class:`Welcome`), and so
does starting Ninaivu while it is not paired; the console can open it again.

**The Primary asks** (:func:`discover`). It sends the question to every
network this computer is on (a broadcast on each) and to each computer on
the tailnet, Tailscale's own peers, because a broadcast does not cross it.
For each waiting Secondary that answers, it introduces itself
(``/api/mugil/introduce``): the two exchange fresh X25519 public halves, and
each works out the same six-digit *match number* from them
(:func:`pairing.match_number`). Both consoles show it.

**One tap pairs them.** The Primary's console lists the Secondary by name
with the number; Pair sends a proof that it holds the key the two halves
give (``/api/mugil/pair``, :meth:`Welcome.accept`). A device that pretended to
be either one would have halves of its own, so the two real screens would
show different numbers, and the wait closes after one pairing.

When a firewall stops the question, the code shown on the Secondary still
pairs them by address (pairing.py).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
from typing import Any, Callable

from . import SECONDARY, pairing, role

log = logging.getLogger(__name__)

#: The UDP port the Secondary listens on for the question.
PORT = 47391

#: The question, and what every answer starts with.
QUESTION = b'{"ninaivu-mugil": 1, "ask": "secondary?"}'
TAG = "ninaivu-mugil"

#: How long a wait for the Primary lasts, in seconds.
WAIT = 30 * 60

#: How long the Primary listens for answers, in seconds.
LISTEN = 1.5

#: The most introductions a wait keeps: one is needed, a few allow a retry.
MOST = 8


class Welcome:
    """On the Secondary: the wait for a Primary, and who has introduced itself."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._until = 0.0
        self._private = None
        self._half = b""
        self._met: dict[str, dict[str, Any]] = {}

    def open(self, seconds: float = WAIT) -> None:
        with self._lock:
            self._private, self._half = pairing.new_half()
            self._until = self._clock() + seconds
            self._met = {}

    def close(self) -> None:
        with self._lock:
            self._until = 0.0
            self._private = None
            self._met = {}

    @property
    def waiting(self) -> bool:
        return self._private is not None and self._clock() < self._until

    def status(self) -> dict[str, Any]:
        with self._lock:
            waiting = self._private is not None and self._clock() < self._until
            return {"waiting": waiting, "until": self._until if waiting else None,
                    "introduced": [{"name": m["name"], "address": m["address"],
                                    "number": m["number"], "at": m["at"]}
                                   for m in self._met.values()] if waiting else []}

    def introduce(self, theirs_hex: str, name: str, address: str) -> dict[str, str] | None:
        """A Primary's public half arrived: this side's half and the number,
        or None when no wait is open."""
        try:
            theirs = bytes.fromhex(str(theirs_hex))
        except ValueError:
            return None
        if len(theirs) != 32:
            return None
        with self._lock:
            if self._private is None or self._clock() >= self._until:
                return None
            if theirs.hex() not in self._met and len(self._met) >= MOST:
                return None
            number = pairing.match_number(theirs, self._half)
            self._met[theirs.hex()] = {"name": str(name or "")[:120], "address": address,
                                       "number": number, "at": self._clock()}
            return {"public": self._half.hex(), "number": number}

    def accept(self, theirs_hex: str, proof: str) -> tuple[str, dict[str, Any]] | None:
        """The tap on the Primary: the key and who it is, or None. Closes the
        wait: one pairing per wait."""
        with self._lock:
            met = self._met.get(str(theirs_hex or "").lower())
            if self._private is None or self._clock() >= self._until or met is None:
                return None
            theirs = bytes.fromhex(theirs_hex)
            key = pairing.tap_key(self._private, theirs, theirs, self._half)
            if not _same(pairing.tap_proof(key), proof):
                return None
            self._until, self._private, self._met = 0.0, None, {}
        return key, met


def _same(a: str, b: str) -> bool:
    import hmac                                                    # noqa: PLC0415
    return hmac.compare_digest(str(a), str(b or ""))


# -- the Secondary answering ----------------------------------------------------

def answer_for(cfg, welcome: Welcome, name: str) -> dict[str, Any]:
    return {TAG: 1, "role": role(cfg), "name": name, "port": int(cfg.port),
            "waiting": welcome.waiting, "id": machine_id(cfg)}


def machine_id(cfg) -> str:
    """A random name for this installation, kept in the state folder."""
    path = cfg.state_dir / "mugil-id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    import secrets                                                 # noqa: PLC0415
    value = secrets.token_hex(8)
    try:
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass
    return value


def on_the_network(cfg) -> bool:
    """Whether the family app answers other computers, not this one only."""
    host = str(getattr(cfg, "host", "") or "")
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host not in ("localhost", "")


class Beacon:
    """On the Secondary: answers the Primary's question on UDP :data:`PORT`."""

    def __init__(self, cfg, welcome: Welcome, name: Callable[[], str]) -> None:
        self.cfg = cfg
        self.welcome = welcome
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.problem: str | None = None

    def keep(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="mugil-beacon", daemon=True)
        self._thread.start()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None

    def _serve(self) -> None:
        sock = None
        while not self._stop.is_set():
            if role(self.cfg) != SECONDARY or not on_the_network(self.cfg):
                if sock is not None:
                    sock.close()
                    sock = None
                self._stop.wait(5)
                continue
            if sock is None:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("0.0.0.0", PORT))
                    sock.settimeout(1.0)
                    self.problem = None
                except OSError as exc:
                    self.problem = str(exc)
                    log.warning("Two computers: cannot listen on UDP %s: %s", PORT, exc)
                    if sock is not None:
                        sock.close()
                    sock = None
                    self._stop.wait(30)
                    continue
            try:
                data, sender = sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                continue
            if data.strip() != QUESTION:
                continue
            reply = json.dumps(answer_for(self.cfg, self.welcome, self.name())).encode()
            try:
                sock.sendto(reply, sender)
            except OSError:
                pass
        if sock is not None:
            sock.close()


# -- the Primary asking -----------------------------------------------------------

def _broadcasts() -> list[str]:
    found = {"255.255.255.255"}
    try:
        import psutil                                              # noqa: PLC0415
        for addresses in psutil.net_if_addrs().values():
            for address in addresses:
                if address.family == socket.AF_INET and address.broadcast:
                    found.add(address.broadcast)
    except Exception:                                             # noqa: BLE001 - asked, not needed
        pass
    return sorted(found)


def tailnet_peers(runner=None) -> list[str]:
    """The IPv4 addresses of the other computers on this tailnet that are online."""
    from ..utils import tailnet                                   # noqa: PLC0415
    cli = tailnet.find_cli()
    if not cli:
        return []
    try:
        done = (runner or tailnet._run)([cli, "status", "--json"])  # noqa: SLF001
        data = json.loads(done.stdout or "{}") if done.returncode == 0 else {}
    except Exception:                                             # noqa: BLE001 - asked, not needed
        return []
    out = []
    for peer in (data.get("Peer") or {}).values():
        if not peer.get("Online", True):
            continue
        for ip in peer.get("TailscaleIPs") or []:
            if "." in str(ip):
                out.append(str(ip))
    return out


def discover(*, listen: float = LISTEN, targets: list[str] | None = None) -> list[dict[str, Any]]:
    """Every Ninaivu Secondary that answers, by its id: its address, port,
    name and whether it is waiting."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, dict[str, Any]] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        for target in (targets if targets is not None else _broadcasts() + tailnet_peers()):
            try:
                sock.sendto(QUESTION, (target, PORT))
            except OSError:
                continue
        deadline = time.monotonic() + listen
        while time.monotonic() < deadline:
            try:
                data, (address, _port) = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                answer = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(answer, dict) or answer.get(TAG) != 1 \
                    or answer.get("role") != SECONDARY:
                continue
            ident = str(answer.get("id") or address)[:64]
            try:
                port = int(answer.get("port"))
            except (TypeError, ValueError):
                continue
            found.setdefault(ident, {"id": ident, "address": address, "port": port,
                                     "name": str(answer.get("name") or address)[:120],
                                     "waiting": bool(answer.get("waiting"))})
    finally:
        sock.close()
    return list(found.values())


class Introductions:
    """On the Primary: the Secondaries it has introduced itself to, and the
    private half for each, until a tap pairs one."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._met: dict[str, dict[str, Any]] = {}

    def keep(self, ident: str, private, half: bytes, theirs: bytes, url: str, name: str) -> str:
        number = pairing.match_number(half, theirs)
        with self._lock:
            self._met = {k: v for k, v in self._met.items() if v["at"] > self._clock() - WAIT}
            self._met[ident] = {"private": private, "half": half, "theirs": theirs, "url": url,
                                "name": name, "number": number, "at": self._clock()}
        return number

    def get(self, ident: str) -> dict[str, Any] | None:
        with self._lock:
            met = self._met.get(ident)
            return dict(met) if met and met["at"] > self._clock() - WAIT else None

    def clear(self) -> None:
        with self._lock:
            self._met = {}
