"""Sharing one machine between the household and the background work.

Ninaivu's background jobs are long: a first scan's analysis is hours, a face
pass most of a day, a cloud upload of a family library several days, a storage
check a read of every file. They run on the same machine, and usually the same
disk, that is serving somebody the gallery — and the one thing a household
notices is a video that stutters because the indexer is hashing the drive it
is being read from.

Each job already had its own rules (the upload's hours and speed cap, the
indexer standing down for a consolidation). This is the rule *between* them and
the people using the app, in three modes an administrator picks from:

``balanced`` (the default)
    Background work carries on while people browse. Indexing, analysis and
    storage checks give way while a video is playing, and the cloud upload
    only takes its larger idle-time chunks when nobody is around.

``quiet``
    Background work waits while anybody is using the app, and resumes when
    they stop. New files are still indexed so they appear — just not while a
    video is playing.

``overnight``
    The heavy work — analysis, uploads, storage checks — waits for the night
    hours and runs flat out then. Indexing new files still happens straight
    away, so photographs appear when they arrive; only understanding them is
    left for the night. A video being watched at night still comes first.

What is *not* governed here: a consolidation, a restore from Drive, an
approval — anything a person started on purpose and is waiting for.

**How it knows.** The family app tells it about each request, and it keeps two
timestamps: when somebody last asked for anything, and when a video or audio
file was last streamed (a byte-range request for an original, a converted copy
or a live photo's clip). Polling endpoints are left out, so a tab left open on
a table does not count as somebody browsing all day. The console is left out
too: an administrator watching a scan is not a household to make way for.

**How jobs use it.** A job asks :meth:`Workload.hold` between pieces of work —
per batch, per photograph, per chunk — and waits while it gets a reason back;
:meth:`Workload.wait_turn` does the waiting, in short slices, so a stop is
still instant. The reason is shown on the activity strip, so a job that is
waiting reads as waiting and not as stuck.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from typing import Any, Callable, Sequence

from ..cloud.limits import Window, format_clock, parse_clock
from ..words import said

__all__ = ["Workload", "MODES", "JOBS", "INDEX", "ANALYSIS", "UPLOAD", "CHECK"]

BALANCED, QUIET, OVERNIGHT = "balanced", "quiet", "overnight"
MODES = (BALANCED, QUIET, OVERNIGHT)

#: The kinds of background work this governs.
INDEX = "index"            # walking the library and thumbnailing new files
ANALYSIS = "analysis"      # the AI passes: tagging, videos, text, faces
UPLOAD = "upload"          # the cloud backup
CHECK = "check"            # the storage check
JOBS = (INDEX, ANALYSIS, UPLOAD, CHECK)

JOB_NAMES = {INDEX: said("Indexing new files"), ANALYSIS: said("Analysis"),
             UPLOAD: said("Cloud upload"), CHECK: said("Storage check")}

#: How recently somebody must have asked for something to count as using the
#: app. Long enough to cover somebody looking at a photograph for a minute
#: without the page asking for anything; short enough that walking away frees
#: the machine soon after.
BROWSING_SECONDS = 120.0

#: How recently a media byte range must have been served to count as a video
#: playing. A browser buffers ahead, so a playing video goes quiet for tens of
#: seconds at a time.
WATCHING_SECONDS = 45.0

#: How often a waiting job looks again.
POLL_SECONDS = 5.0

#: Requests that are not a person doing anything: pages polling for status,
#: the service worker, health checks.
_NOT_A_PERSON = ("/api/status", "/api/events", "/healthz", "/readyz", "/sw.js",
                 "/manifest.webmanifest", "/static/", "/favicon")

#: Where video and audio are streamed from.
_STREAMS = ("/api/file/", "/api/proxy/", "/api/live-video/")

#: Tailscale's addresses, kept as the ranges a caller gets when it names no
#: provider: what ``from_outside`` did before remote access became a choice.
_TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_FORWARDED = ("X-Forwarded-For", "X-Real-IP", "Forwarded")

#: What a person away from home is sent that is heavy enough to share the
#: house's internet connection with the backup: pictures, video, downloads.
#: Not the pages' own polling, a few hundred bytes every few seconds: counted,
#: a console left open away from home held the backup for as long as it was.
_MEDIA = ("/api/thumb/", "/api/file/", "/api/proxy/", "/api/live-video/", "/api/preview/",
          "/api/download/", "/api/faces/thumb/", "/api/share/")

_own: tuple[float, tuple[frozenset[str], ...], frozenset[str]] = (0.0, (), frozenset())


def own_interfaces() -> tuple[frozenset[str], ...]:
    """This computer's own addresses, interface by interface, looked up at
    most once a minute. Loopback is always among them.

    Kept per interface because which addresses sit *together* says what an
    interface is: a global IPv6 address beside a private IPv4 one is the
    house's Wi-Fi, where the whole /64 is the household; the same global
    address alone is a VPS uplink or a tunnel, where the /64 is shared with
    strangers (``server/remote.py``)."""
    global _own
    now = time.monotonic()
    if _own[1] and now - _own[0] < 60:
        return _own[1]
    groups = [frozenset({"127.0.0.1", "::1"})]
    try:
        import psutil                                    # noqa: PLC0415
        for addresses in psutil.net_if_addrs().values():
            held = frozenset(str(address.address).split("%", 1)[0] for address in addresses
                             if address.family in (socket.AF_INET, socket.AF_INET6))
            if held:
                groups.append(held)
    except Exception:                                    # noqa: BLE001 - loopback at least
        pass
    _own = (now, tuple(groups), frozenset().union(*groups))
    return _own[1]


def own_addresses() -> frozenset[str]:
    """This computer's own addresses, its tailnet one among them, as one set.
    A request from one of them never left the computer: the console opened
    here at the Tailscale address came from 100.115.249.50, read as a device
    away from home, and held the backup."""
    own_interfaces()
    return _own[2]


def from_outside(remote_addr: str | None, headers: Any = None, trusted_proxies: int = 0,
                 own: frozenset[str] | set[str] | None = None,
                 outside_networks: Sequence[Any] | None = None) -> bool:
    """Whether a request came from outside the house network: a device on the
    household's tunnel, the internet, or anything through a proxy. What it is
    sent goes up the house's internet connection, which is what the cloud
    backup fills.

    A proxy Ninaivu trusts (``trusted_proxies``) has already put the real
    address in *remote_addr*; one it does not trust is taken as outside.
    This computer's own addresses (*own*, else :func:`own_addresses`) are not.
    *outside_networks* are the ranges the remote-access provider says are away
    from home (``server/remote.py``); without them, Tailscale's.
    """
    if not trusted_proxies and headers is not None and any(headers.get(h) for h in _FORWARDED):
        return True
    try:
        ip = ipaddress.ip_address(str(remote_addr or "").split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if str(ip) in (own if own is not None else own_addresses()):
        return False
    ranges = _TAILNET if outside_networks is None else outside_networks
    if any(ip in net for net in ranges if net.version == ip.version):
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


class Workload:
    """The household's claim on the machine, and each job's turn."""

    def __init__(self, cfg: Any, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self._clock = clock
        self._lock = threading.Lock()
        self._browsing_at = 0.0
        self._watching_at = 0.0
        self._outside_at = 0.0

    # -- what the household is doing ---------------------------------------

    def noticed(self, path: str, *, ranged: bool = False) -> None:
        """A request from the family app. Cheap: called on every one."""
        if not path or path.startswith(_NOT_A_PERSON):
            return
        now = self._clock()
        with self._lock:
            self._browsing_at = now
            if ranged and path.startswith(_STREAMS):
                self._watching_at = now

    def noticed_outside(self, path: str) -> None:
        """Media sent outside the house (see :func:`from_outside`), from the
        family app or the console alike."""
        if not path or not path.startswith(_MEDIA):
            return
        with self._lock:
            self._outside_at = self._clock()

    def browsing(self) -> bool:
        return self._clock() - self._browsing_at < BROWSING_SECONDS

    def outside(self) -> bool:
        """Somebody has used Ninaivu from outside the house in the last two minutes."""
        return self._clock() - self._outside_at < BROWSING_SECONDS

    def watching(self) -> bool:
        return self._clock() - self._watching_at < WATCHING_SECONDS

    # -- the settings ------------------------------------------------------

    @property
    def mode(self) -> str:
        mode = str(getattr(self.cfg, "workload_mode", BALANCED) or BALANCED)
        return mode if mode in MODES else BALANCED

    def night(self) -> Window:
        start = getattr(self.cfg, "workload_night_start", "23:00") or "23:00"
        end = getattr(self.cfg, "workload_night_end", "06:00") or "06:00"
        return Window(start, end)

    def is_night(self) -> bool:
        night = self.night()
        return not night.always_open and night.is_open(self._clock())

    # -- the rule ------------------------------------------------------------

    def upload_full_speed(self) -> bool:
        """The household asked for the backup to go at full speed (cloud_full_speed)."""
        return bool(getattr(self.cfg, "cloud_full_speed", False))

    def hold(self, job: str) -> str | None:
        """Why *job* should wait right now, in words for the strip; or None."""
        if job == UPLOAD and self.outside():
            # Whatever that person opens comes up the same internet connection
            # the backup fills: at full speed, all of it, and a phone away from
            # home waited seconds for each photograph. In every mode, and at
            # full speed too; people at home use the house network, not that.
            return said("someone is using Ninaivu from outside the house")
        if job == UPLOAD and self.upload_full_speed():
            return None
        mode = self.mode
        watching = self.watching()
        if mode == OVERNIGHT:
            if job in (ANALYSIS, UPLOAD, CHECK) and not self.is_night():
                return f"saved for the night ({self.night().label()})"
            if job != UPLOAD and watching:
                return said("someone is watching a video")
            return None
        if mode == QUIET:
            if watching:
                return said("someone is watching a video")
            if job != INDEX and self.browsing():
                return said("someone is using Ninaivu")
            return None
        # Balanced. The upload goes over the internet and reads far less than
        # it sends, and people at home use the house network, so it is left to
        # its own pacing (somebody outside the house is handled above).
        if job != UPLOAD and watching:
            return said("someone is watching a video")
        return None

    def boost(self, job: str) -> bool:
        """May *job* use its faster, idle-time pace right now?"""
        if self.hold(job):
            return False
        if job == UPLOAD and self.upload_full_speed():
            return True
        if self.mode == OVERNIGHT and self.is_night():
            return not self.watching()
        if self.mode == QUIET:
            return False
        return not self.browsing() and not self.watching()

    def wait_turn(self, job: str, stop: threading.Event | None = None,
                  on_hold: Callable[[str | None], None] | None = None) -> bool:
        """Wait until *job* may carry on. False if *stop* was set instead.

        *on_hold* is told the reason when the wait starts, and None when it
        ends, so the job can say so where people will see it.
        """
        reason = self.hold(job)
        if not reason:
            return not (stop is not None and stop.is_set())
        if on_hold is not None:
            on_hold(reason)
        try:
            while reason:
                if stop is not None:
                    if stop.wait(POLL_SECONDS):
                        return False
                else:
                    time.sleep(POLL_SECONDS)
                reason = self.hold(job)
        finally:
            if on_hold is not None:
                on_hold(None)
        return not (stop is not None and stop.is_set())

    # -- what the console shows ------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        night = self.night()
        return {
            "mode": self.mode,
            "modes": list(MODES),
            "night_start": format_clock(night.start) if night.start is not None else "",
            "night_end": format_clock(night.end) if night.end is not None else "",
            "night_label": night.label(),
            "is_night": self.is_night(),
            "browsing": self.browsing(),
            "watching": self.watching(),
            "last_seen": self._browsing_at or None,
            "seconds_since_seen": (round(now - self._browsing_at)
                                   if self._browsing_at else None),
            "jobs": [{"job": job, "name": JOB_NAMES[job], "holding": self.hold(job),
                      "boost": self.boost(job)} for job in JOBS],
        }


def valid_clock(value: Any) -> str | None:
    """``HH:MM`` for a time of day, or None if it is not one."""
    minutes = parse_clock(str(value or "").strip())
    return format_clock(minutes) if minutes is not None else None
