"""Noticing a drive that is failing, before it takes the photographs with it.

Everything this household knows about its failing drives it learned by reading
the Windows event log by hand, after the fact: a backup disk that logged bad
blocks for three days and then dropped off the bus, a USB SSD that reset
itself hundreds of times mid-consolidation and made a run skip sixty-one
folders, and the library disk itself failing to flush its journal the moment
it vanished for a second. Windows wrote all of it down. Nothing read it.

This reads it. Every few minutes it asks Windows three things, none of which
need administrator rights: the storage events in the System log since it last
looked, each physical disk's own health, and each volume's health and free
space. It files every event under the drive it happened on, keeps a week of
them, and says so — on the console, in the problem list, and by notification
for the ones that matter.

**Which drive.** Windows logs a disk error against a *number* ("Disk 3"), and
numbers a USB drive afresh every time it is plugged in. So every check records
which physical drive, by serial number, held each number, and an event is
filed under a drive only when the number meant the same drive on both sides
of it. Anything older than Ninaivu's own watching — the week before it started
— is filed as "Disk 3, as Windows numbered it then" rather than guessed at.
File-system events name the volume, and some the serial, and are filed by that.

**What it does not do.** It never runs anything that writes to a disk, needs
elevation, or could stress a failing drive: no SMART self-tests, no reads of
the disk itself. On other systems it reports that it cannot see drive health
rather than pretending everything is fine.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = ["DiskWatch", "classify", "CRITICAL", "WARNING"]

CRITICAL = "critical"
WARNING = "warning"

#: How far back the report looks, and how long events are kept.
WINDOW = 7 * 24 * 3600
KEEP = 30 * 24 * 3600

#: How often the watcher looks.
EVERY = 10 * 60

#: Lost writes on one drive in a week before it counts as failing.
LOST_WRITES_CRITICAL = 3

#: A drive Ninaivu uses with less free than this is "nearly full".
LOW_FRACTION = 0.05
LOW_BYTES = 20 * 1024 ** 3

#: What each Windows storage event means, in words a household can act on.
#: (provider, event id) -> (kind, severity, what it counts)
EVENTS: dict[tuple[str, int], tuple[str, str, str]] = {
    ("disk", 7): ("bad_blocks", CRITICAL, "bad blocks"),
    ("disk", 154): ("hardware_errors", CRITICAL,
                    "reads or writes that failed with a hardware error"),
    ("disk", 51): ("io_errors", WARNING, "errors while reading or writing"),
    ("disk", 153): ("retried", WARNING, "reads or writes that had to be retried"),
    ("disk", 11): ("controller_errors", WARNING, "controller errors"),
    ("disk", 157): ("removed", WARNING, "times it vanished without being ejected"),
    ("UASPStor", 129): ("resets", WARNING, "USB resets"),
    ("storahci", 129): ("resets", WARNING, "resets"),
    ("stornvme", 129): ("resets", WARNING, "resets"),
    ("Ntfs", 50): ("writes_lost", CRITICAL, "writes Windows could not save"),
    ("Microsoft-Windows-Ntfs", 140): ("writes_lost", CRITICAL,
                                      "writes Windows could not save"),
    ("Ntfs", 55): ("filesystem", CRITICAL, "file-system damage"),
    ("Ntfs", 137): ("filesystem", WARNING, "file-system errors"),
    ("Microsoft-Windows-Ntfs", 98): ("filesystem", CRITICAL,
                                     "checks that found the file system needs repair"),
}

PROVIDERS = sorted({provider for provider, _ in EVENTS})

#: What to do about each, said once per drive.
ADVICE = {
    "bad_blocks": "Copy anything irreplaceable off it now, with a verified copy, "
                  "and stop using it for anything else.",
    "hardware_errors": "Copy anything irreplaceable off it now and replace it.",
    "writes_lost": "Something written to it did not arrive. If it was unplugged "
                   "or lost power at that moment, that explains it; otherwise check "
                   "the cable. Either way, check the files most recently copied there.",
    "filesystem": "Run a file-system check on it once its contents are safe elsewhere.",
    "resets": "Usually the cable, the USB port or hub, or the enclosure running "
              "hot, before the drive itself. Try another port and cable.",
    "retried": "Often the cable or port. Watch whether it gets worse.",
    "io_errors": "Often the cable or port. Watch whether it gets worse.",
    "removed": "It was unplugged or lost power while in use.",
    "controller_errors": "Usually the cable or port.",
    "unhealthy": "Windows itself reports this drive as unhealthy. Copy "
                 "anything irreplaceable off it.",
    "low_space": "Free up space or move the library to a larger drive.",
}

_HARDDISK = re.compile(r"\\Device\\Harddisk(\d+)\\", re.I)
_LETTER = re.compile(r"^([A-Za-z]):(?:\\|$)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS disk_events (
    record_id INTEGER PRIMARY KEY,
    at        REAL NOT NULL,
    provider  TEXT NOT NULL,
    event_id  INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    severity  TEXT NOT NULL,
    drive     TEXT NOT NULL,
    label     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_disk_events_at ON disk_events(at);
CREATE TABLE IF NOT EXISTS disk_seen (
    drive    TEXT PRIMARY KEY,
    name     TEXT NOT NULL DEFAULT '',
    serial   TEXT NOT NULL DEFAULT '',
    bus      TEXT NOT NULL DEFAULT '',
    letters  TEXT NOT NULL DEFAULT '',
    seen_at  REAL NOT NULL DEFAULT 0
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def classify(provider: str, event_id: int, message: str = "") -> tuple[str, str, str] | None:
    """(kind, severity, words) for a storage event, or None if it is not one."""
    found = EVENTS.get((provider, int(event_id)))
    if found is None:
        return None
    if (provider, int(event_id)) == ("Microsoft-Windows-Ntfs", 98) and \
            "healthy" in (message or "").lower() and "not" not in (message or "").lower():
        return None                       # "Volume C: is healthy"
    return found


# ---------------------------------------------------------------------------
# Asking Windows
# ---------------------------------------------------------------------------

_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$since = [DateTime]::UtcNow.AddSeconds(-__SINCE__)
$events = @()
foreach ($p in @(__PROVIDERS__)) {
  try {
    $events += Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName=$p; StartTime=$since} -MaxEvents 5000 -ErrorAction Stop
  } catch { }
}
$epoch = [DateTime]'1970-01-01'
$ev = @($events | ForEach-Object {
  [pscustomobject]@{
    r = $_.RecordId; p = $_.ProviderName; id = $_.Id
    t = [math]::Round(($_.TimeCreated.ToUniversalTime() - $epoch).TotalSeconds, 0)
    props = @($_.Properties | Select-Object -First 16 | ForEach-Object { "$($_.Value)" })
    msg = "$(($_.Message -split "`n")[0])"
  }
})
$disks = @(Get-PhysicalDisk | ForEach-Object {
  [pscustomobject]@{ n = [int]$_.DeviceId; name = "$($_.FriendlyName)".Trim()
    serial = "$($_.SerialNumber)".Trim(); bus = "$($_.BusType)"
    health = "$($_.HealthStatus)"; op = "$($_.OperationalStatus)" }
})
$parts = @(Get-Partition | Where-Object DriveLetter | ForEach-Object {
  [pscustomobject]@{ n = [int]$_.DiskNumber; l = "$($_.DriveLetter)" }
})
$vols = @(Get-Volume | Where-Object DriveLetter | ForEach-Object {
  [pscustomobject]@{ l = "$($_.DriveLetter)"; label = "$($_.FileSystemLabel)"
    health = "$($_.HealthStatus)"; op = "$($_.OperationalStatus)"
    size = [int64]$_.Size; free = [int64]$_.SizeRemaining }
})
[pscustomobject]@{ events = $ev; disks = $disks; parts = $parts; volumes = $vols } |
  ConvertTo-Json -Depth 5 -Compress
"""


def windows_probe(since_seconds: float) -> dict[str, Any] | None:
    """Events of the last *since_seconds*, and every disk and volume now."""
    if sys.platform != "win32":
        return None
    script = (_SCRIPT.replace("__SINCE__", str(int(max(60, since_seconds))))
              .replace("__PROVIDERS__", ",".join(f"'{p}'" for p in PROVIDERS)))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not ask Windows about the drives: {exc}") from exc
    text = done.stdout.decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError("Windows said nothing about the drives: "
                           + done.stderr.decode("utf-8", "replace")[:200])
    data = json.loads(text)
    for key in ("events", "disks", "parts", "volumes"):
        value = data.get(key)
        data[key] = [] if value is None else value if isinstance(value, list) else [value]
    return data


# ---------------------------------------------------------------------------
# Watching
# ---------------------------------------------------------------------------

class DiskWatch:
    """Looks every :data:`EVERY` seconds; files, keeps and reports."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 probe: Callable[[float], dict[str, Any] | None] = windows_probe,
                 notify: Callable[[str, str, str], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self._connect = connect_db
        self._probe = probe
        self._notify = notify
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #: {disk number: drive key} at the previous look, and when.
        self._numbers: dict[int, str] = {}
        self._numbers_at = 0.0
        self._last_look = 0.0
        self.supported = sys.platform == "win32"
        self.checked_at = 0.0
        self.error = ""
        self._disks: list[dict[str, Any]] = []
        self._volumes: list[dict[str, Any]] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-disks",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def _loop(self) -> None:
        # A minute in: start-up is busy, and nothing here is urgent to the second.
        if self._stop.wait(60):
            return
        while True:
            try:
                self.check()
            except Exception:                               # noqa: BLE001
                log.exception("could not check the drives")
            if self._stop.wait(EVERY):
                return

    # -- one look -----------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """Look now. Returns the report."""
        with self._lock:
            now = self._clock()
            since = WINDOW if not self._last_look else max(60.0, now - self._last_look + 120)
            try:
                data = self._probe(since)
            except Exception as exc:                        # noqa: BLE001
                # Said once, and again only when the reason changes: a probe
                # that cannot run (PowerShell blocked, say) wrote the same
                # warning every ten minutes for as long as Ninaivu ran. The
                # console shows the reason all the while.
                note = log.warning if str(exc) != self.error else log.debug
                note("could not check the drives: %s", exc)
                self.error = str(exc)
                return self.report()
            if data is None:
                self.supported = False
                self.checked_at = now
                return self.report()
            self.supported = True
            self.error = ""
            from . import db                                       # noqa: PLC0415
            conn = self._connect()
            init_schema(conn)
            with db._write_lock:                                    # noqa: SLF001
                numbers = self._remember_drives(conn, data, now)
                fresh = self._file_events(conn, data.get("events") or [], numbers, now)
                conn.execute("DELETE FROM disk_events WHERE at < ?", (now - KEEP,))
                conn.commit()
            self._numbers, self._numbers_at = numbers, now
            self._last_look = now
            self.checked_at = now
            self._disks = data.get("disks") or []
            self._volumes = data.get("volumes") or []
            report = self.report()
            self._speak(conn, report, fresh)
            return report

    def _remember_drives(self, conn, data, now) -> dict[int, str]:
        """Record who is who, and return {disk number: drive key} as of now."""
        letters: dict[int, list[str]] = {}
        for part in data.get("parts") or []:
            letters.setdefault(int(part.get("n", -1)), []).append(str(part.get("l", "")).upper())
        numbers: dict[int, str] = {}
        for disk in data.get("disks") or []:
            number = int(disk.get("n", -1))
            key = _drive_key(disk)
            numbers[number] = key
            conn.execute(
                "INSERT INTO disk_seen(drive, name, serial, bus, letters, seen_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(drive) DO UPDATE SET "
                "name=excluded.name, bus=excluded.bus, letters=excluded.letters, "
                "seen_at=excluded.seen_at",
                (key, str(disk.get("name") or ""), str(disk.get("serial") or ""),
                 str(disk.get("bus") or ""), ",".join(sorted(letters.get(number, []))), now))
        return numbers

    def _file_events(self, conn, events, numbers, now) -> list[dict[str, Any]]:
        """File each new event under the drive it happened on."""
        serials = {r["serial"]: r["drive"] for r in conn.execute(
            "SELECT drive, serial FROM disk_seen WHERE serial != ''")}
        by_letter = {}
        for row in conn.execute("SELECT drive, letters FROM disk_seen"):
            for letter in filter(None, (row["letters"] or "").split(",")):
                by_letter.setdefault(letter, row["drive"])
        ordered = sorted(events, key=lambda e: float(e.get("t") or 0))
        disk_times = [(float(e.get("t") or 0), _disk_number(e)) for e in ordered
                      if e.get("p") == "disk" and _disk_number(e) is not None]
        fresh = []
        for event in ordered:
            provider, event_id = str(event.get("p") or ""), int(event.get("id") or 0)
            meaning = classify(provider, event_id, str(event.get("msg") or ""))
            if meaning is None:
                continue
            at = float(event.get("t") or 0)
            drive, label = self._whose(event, at, numbers, serials, by_letter,
                                       disk_times, now)
            cur = conn.execute(
                "INSERT OR IGNORE INTO disk_events(record_id, at, provider, event_id, "
                "kind, severity, drive, label) VALUES(?,?,?,?,?,?,?,?)",
                (int(event.get("r") or 0), at, provider, event_id, meaning[0],
                 meaning[1], drive, label))
            if cur.rowcount:
                fresh.append({"drive": drive, "label": label, "kind": meaning[0],
                              "severity": meaning[1], "words": meaning[2], "at": at})
        return fresh

    def _whose(self, event, at, numbers, serials, by_letter, disk_times, now):
        """(drive key, label) for one event."""
        props = [str(p) for p in event.get("props") or []]
        for value in props:                    # an event that names the drive
            if value.strip() in serials:
                return serials[value.strip()], ""
        for value in props:                    # one that names the volume
            match = _LETTER.match(value.strip())
            if match:
                letter = match.group(1).upper()
                return by_letter.get(letter, f"volume:{letter}"), f"Drive {letter}:"
        number = _disk_number(event)
        if number is None and event.get("p") != "disk":
            # A USB reset names a port, not a disk. The disk it belongs to is
            # the one Windows logged trouble on in the same few seconds.
            near = [n for t, n in disk_times if abs(t - at) <= 10]
            number = near[0] if near else None
        if number is None:
            return "unidentified", ""
        # Trust the number only if it meant the same drive at both looks
        # either side of the event — or, on a first look, if it is recent.
        known_now = numbers.get(number)
        if known_now:
            if self._numbers_at and at >= self._numbers_at - EVERY:
                if self._numbers.get(number) == known_now:
                    return known_now, ""
            elif not self._numbers_at and now - at <= EVERY:
                return known_now, ""
        return f"disk:{number}", f"A drive Windows called Disk {number}"

    # -- what it adds up to ---------------------------------------------------

    def used_drives(self) -> dict[str, list[str]]:
        """{drive letter: what Ninaivu uses it for}."""
        used: dict[str, list[str]] = {}

        def add(path: str | None, why: str) -> None:
            letter = _letter_of(path)
            if letter and why not in used.setdefault(letter, []):
                used[letter].append(why)

        for root in getattr(self.cfg, "roots", []) or []:
            add(root, "library")
        add(str(getattr(self.cfg, "state_dir", "") or ""), "Ninaivu's own records")
        try:
            from ..archive import database as archive_db          # noqa: PLC0415
            settings = archive_db.load_settings()
            add(settings.get("destination_dir"), "archive")
            for source in settings.get("source_dirs") or []:
                add(source.get("path"), "archive source")
        except Exception:                                          # noqa: BLE001
            log.debug("could not read the archive's drives", exc_info=True)
        return used

    def report(self) -> dict[str, Any]:
        conn = self._connect()
        init_schema(conn)
        now = self._clock()
        used = self.used_drives()
        seen = {r["drive"]: dict(r) for r in conn.execute("SELECT * FROM disk_seen")}
        problems: dict[str, dict[str, dict[str, Any]]] = {}
        for row in conn.execute(
                "SELECT drive, MAX(label) AS label, kind, MIN(severity) AS severity, "
                "COUNT(*) AS n, MIN(at) AS first, MAX(at) AS last FROM disk_events "
                "WHERE at >= ? GROUP BY drive, kind", (now - WINDOW,)):
            entry = problems.setdefault(row["drive"], {})
            words = next((w for (k, s, w) in EVENTS.values() if k == row["kind"]), row["kind"])
            severity = row["severity"]
            if row["kind"] == "writes_lost" and int(row["n"]) < LOST_WRITES_CRITICAL:
                # One lost write is what a drive yanked mid-copy leaves behind —
                # worth a look, not an alarm. A drive that keeps losing them is.
                severity = WARNING
            entry[row["kind"]] = {"kind": row["kind"], "severity": severity,
                                  "words": words, "count": int(row["n"]),
                                  "first": row["first"], "last": row["last"],
                                  "label": row["label"],
                                  "advice": ADVICE.get(row["kind"], "")}
        connected = {_drive_key(d): d for d in self._disks}
        volumes = {str(v.get("l", "")).upper(): v for v in self._volumes}
        keys = list(dict.fromkeys([*connected, *problems]))
        drives = []
        for key in keys:
            disk = connected.get(key)
            known = seen.get(key, {})
            letters = sorted(filter(None, (known.get("letters") or "").split(","))) \
                if key in seen else ([key.split(":")[1]] if key.startswith("volume:") else [])
            found = list(problems.get(key, {}).values())
            vols = [volumes[letter] for letter in letters if letter in volumes] if disk else []
            for vol in vols:
                if str(vol.get("health", "Healthy")) not in ("Healthy", ""):
                    found.append({"kind": "unhealthy", "severity": CRITICAL,
                                  "words": f"{vol['l']}: reports {vol['health']}"
                                           f" ({vol.get('op', '')})",
                                  "count": 1, "first": now, "last": now,
                                  "advice": ADVICE["unhealthy"]})
                free, size = int(vol.get("free") or 0), int(vol.get("size") or 0)
                if (vol["l"] in used and size and
                        (free < LOW_BYTES or free / size < LOW_FRACTION)):
                    found.append({"kind": "low_space", "severity": WARNING,
                                  "words": f"{vol['l']}: has {_size(free)} free of "
                                           f"{_size(size)}",
                                  "count": 1, "first": now, "last": now,
                                  "advice": ADVICE["low_space"]})
            if disk and str(disk.get("health", "Healthy")) not in ("Healthy", ""):
                found.append({"kind": "unhealthy", "severity": CRITICAL,
                              "words": f"Windows reports the drive {disk['health']}",
                              "count": 1, "first": now, "last": now,
                              "advice": ADVICE["unhealthy"]})
            status = (CRITICAL if any(p["severity"] == CRITICAL for p in found)
                      else WARNING if found else "ok")
            label = next((p.get("label") for p in found if p.get("label")), "")
            drives.append({
                "key": key,
                "name": (disk or {}).get("name") or known.get("name") or label
                        or ("A drive Windows could not name" if key == "unidentified"
                            else key),
                "serial": (disk or {}).get("serial") or known.get("serial") or "",
                "bus": (disk or {}).get("bus") or known.get("bus") or "",
                "connected": disk is not None,
                "letters": letters,
                "used_for": sorted({why for letter in letters for why in used.get(letter, [])}),
                "volumes": [{"letter": v["l"], "label": v.get("label", ""),
                             "health": v.get("health", ""), "free": v.get("free"),
                             "size": v.get("size")} for v in vols],
                "status": status,
                "problems": sorted(found, key=lambda p: (p["severity"] != CRITICAL,
                                                         -p["count"])),
            })
        order = {CRITICAL: 0, WARNING: 1, "ok": 2}
        drives.sort(key=lambda d: (order[d["status"]], not d["used_for"], d["name"]))
        return {
            "supported": self.supported,
            "checked_at": self.checked_at or None,
            "error": self.error,
            "window_days": WINDOW // 86400,
            "drives": drives,
            "worst": drives[0]["status"] if drives else "ok",
        }

    # -- telling somebody ---------------------------------------------------------

    #: A drive is reported again only after this long, however much it logs.
    RETELL = 24 * 3600

    def _speak(self, conn, report, fresh) -> None:
        if fresh:
            counts: dict[tuple[str, str], int] = {}
            for item in fresh:
                counts[(item["drive"], item["words"])] = counts.get(
                    (item["drive"], item["words"]), 0) + 1
            names = {d["key"]: d["name"] for d in report["drives"]}
            for (drive, words), n in counts.items():
                log.warning("drive %s: %d new %s", names.get(drive, drive), n, words)
        if self._notify is None:
            return
        from . import db                                           # noqa: PLC0415
        try:
            told = json.loads(db.get_meta(conn, "disk_health_told", "{}") or "{}")
        except ValueError:
            told = {}
        now = self._clock()
        for drive in report["drives"]:
            if drive["status"] != CRITICAL:
                continue
            newest = max(float(p.get("last") or 0) for p in drive["problems"])
            before = told.get(drive["key"])
            if isinstance(before, dict):
                # Said already: again only when there is something new, and
                # not more than once a day however much a dying drive logs.
                if newest <= float(before.get("last", 0)) or                         now - float(before.get("at", 0)) < self.RETELL:
                    continue
            worst = drive["problems"][0]
            letters = ", ".join(f"{letter}:" for letter in drive["letters"])
            where = (letters if drive["connected"] else
                     f"not connected; it was {letters}" if letters else "not connected")
            uses = f" Ninaivu uses it for: {', '.join(drive['used_for'])}." \
                if drive["used_for"] else ""
            detail = (f"{drive['name']} ({where}) — "
                      + "; ".join(f"{p['count']:,} {p['words']}" if p["count"] > 1
                                  else p["words"] for p in drive["problems"][:4])
                      + f" in the last {report['window_days']} days.{uses} "
                      + (worst.get("advice") or ""))
            try:
                self._notify("disk_health", f"{drive['name']} may be failing", detail)
            except Exception:                                    # noqa: BLE001
                log.debug("could not report a failing drive", exc_info=True)
            told[drive["key"]] = {"at": now, "last": newest}
        with db._write_lock:                                        # noqa: SLF001
            db.set_meta(conn, "disk_health_told", json.dumps(told))
            # set_meta leaves the transaction open, and on this thread's
            # connection an open write would block every other writer.
            conn.commit()


def _drive_key(disk: dict[str, Any]) -> str:
    serial = str(disk.get("serial") or "").strip()
    if serial:
        return serial
    return f"disk:{int(disk.get('n', -1))}"


def _disk_number(event: dict[str, Any]) -> int | None:
    for value in event.get("props") or []:
        match = _HARDDISK.search(str(value))
        if match:
            return int(match.group(1))
    return None


def _letter_of(path: str | None) -> str | None:
    # From the text, not Path.anchor, which only knows drive letters on Windows.
    match = re.match(r"^([A-Za-z]):", str(path or "").strip())
    return match.group(1).upper() if match else None


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"

