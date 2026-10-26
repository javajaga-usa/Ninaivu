"""On the Primary: sending the originals to the Secondary, each one checked.

A run goes through every library folder that is plugged in, a few hundred
files at a time. For each group it asks the Secondary which it already keeps
(the same path, size and fingerprint), and sends the rest one by one, each
as it is on the disk. The Secondary keeps a file only when what arrived has
the fingerprint the Primary named (:mod:`.hold`); a file that changed while
it was read is not kept, and goes again next run.

Only reads here: no original on this computer is opened for writing, moved
or removed. Fingerprints missing from the index are worked out and stored
there, as ``content_fingerprints`` would. What the Secondary confirmed is
written to ``mugil-sent.db`` in the state folder, which later stages read
before they let a light copy stand in for an original.

It makes way for the household like the cloud backup does (the ``upload``
hold of server/workload.py), and on its own runs every
``mugil_copy_every_hours``.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import sqlite3
import ssl
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

from .. import __version__
from . import PRIMARY, pairing, role
from ..storage import fingerprints, library_id

log = logging.getLogger(__name__)

#: Files asked about at a time.
GROUP = 200

#: How long one request may take to start answering, in seconds.
TIMEOUT = 60

#: How often the keeper looks at whether a run is owed, in seconds.
LOOK_EVERY = 10 * 60.0

#: After pairing, how long before the first copy starts by itself, in
#: seconds: long enough for the Secondary's household to say the library is
#: to be copied there by hand instead (mugil/adopt.py).
FIRST_COPY_AFTER = 60.0

#: After a run that did not finish, how long before one is tried by itself.
RETRY_AFTER = 60 * 60.0

#: How often the keeper asks after the Secondary between runs, and how long
#: it may go unseen, or how full its disk may be, before somebody is told.
ASK_AFTER_EVERY = 60 * 60.0
AWAY_TOO_LONG = 24 * 60 * 60.0
NEARLY_FULL = 0.05

#: While the Secondary is away: the first wait before asking again, and the
#: longest, in seconds. It doubles in between.
AWAY_FIRST, AWAY_LONGEST = 30.0, 10 * 60.0

#: Answers that mean the Secondary is away (switched off, asleep, restarting,
#: off the network) rather than that it refused: the run waits for it.
AWAY = (0, 502, 503, 504)

#: The Secondary is taking in a copy of the library made by hand
#: (mugil/adopt.py): the run waits, as for one that is away, without
#: saying it is away.
NOT_YET = 425

#: The record of what the Secondary confirmed, in the state folder.
SENT = "mugil-sent.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent (
    root     TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    algo     TEXT NOT NULL,
    digest   TEXT NOT NULL,
    sent_at  REAL NOT NULL,
    PRIMARY KEY (root, rel_path)
);
CREATE TABLE IF NOT EXISTS libraries (
    root    TEXT PRIMARY KEY,
    library TEXT NOT NULL,
    name    TEXT NOT NULL
);
"""


class PartnerError(Exception):
    """The Secondary could not be reached, or said no."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class Partner:
    """The Secondary, spoken to as one Ninaivu speaks to another."""

    def __init__(self, url: str, key: str | None = None, *, timeout: float = TIMEOUT) -> None:
        parts = urlsplit(str(url or "").strip().rstrip("/"))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PartnerError("Give the other computer's console address, such as http://linux-pc:3000.")
        self.url = f"{parts.scheme}://{parts.netloc}"
        self._parts = parts
        self.key = key
        self.timeout = timeout

    def _connection(self) -> http.client.HTTPConnection:
        if self._parts.scheme == "https":
            return http.client.HTTPSConnection(self._parts.hostname, self._parts.port,
                                               timeout=self.timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self._parts.hostname, self._parts.port,
                                          timeout=self.timeout)

    def request(self, method: str, path: str, *, query: dict[str, Any] | None = None,
                body: Any = None, length: int | None = None, digest: str | None = None,
                headers: dict[str, str] | None = None, signed: bool = True) -> dict[str, Any]:
        target = path + (f"?{urlencode(query, quote_via=quote)}" if query else "")
        sending = dict(headers or {})
        # Not a browser page: the console's cross-origin guard lets a script
        # through when it says so (ninaivu/__init__.py).
        sending["X-Requested-With"] = "ninaivu-mugil"
        sending["X-Ninaivu-Version"] = __version__
        if body is None or isinstance(body, (bytes, dict, list)):
            data = b"" if body is None else (
                body if isinstance(body, bytes) else json.dumps(body).encode())
            if isinstance(body, (dict, list)):
                sending["Content-Type"] = "application/json"
            sending["Content-Length"] = str(len(data))
            digest = digest or pairing.body_digest(data)
        else:
            data = body
            sending["Content-Length"] = str(length)
        if signed:
            if not self.key:
                raise PartnerError("This computer is not paired with another.")
            sending.update(pairing.signed_headers(self.key, method, target, digest or ""))
        conn = self._connection()
        try:
            conn.request(method, target, body=data, headers=sending)
            response = conn.getresponse()
            raw = response.read(4 * 1024 * 1024)
        except (OSError, http.client.HTTPException) as exc:
            raise PartnerError(f"The other computer did not answer: {exc}") from exc
        finally:
            conn.close()
        try:
            answer = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            answer = {}
        if response.status >= 400:
            said = (answer or {}).get("refused") or (answer or {}).get("error")
            raise PartnerError(str(said or f"HTTP {response.status}"),
                               response.status)
        return answer if isinstance(answer, dict) else {"answer": answer}

    def download(self, path: str, *, query: dict[str, Any], write: Callable[[bytes], Any]) -> int:
        """GET *path* and hand its body to *write* a piece at a time; the bytes."""
        target = path + (f"?{urlencode(query, quote_via=quote)}" if query else "")
        if not self.key:
            raise PartnerError("This computer is not paired with another.")
        sending = {"X-Requested-With": "ninaivu-mugil",
                   **pairing.signed_headers(self.key, "GET", target, pairing.body_digest(b""))}
        conn = self._connection()
        got = 0
        try:
            try:
                conn.request("GET", target, headers=sending)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                raise PartnerError(f"The other computer did not answer: {exc}") from exc
            if response.status >= 400:
                raw = response.read(64 * 1024)
                try:
                    said = (json.loads(raw.decode("utf-8")) or {}).get("refused")
                except ValueError:
                    said = None
                raise PartnerError(str(said or f"HTTP {response.status}"), response.status)
            while True:
                try:
                    chunk = response.read(1024 * 1024)
                except (OSError, http.client.HTTPException) as exc:
                    raise PartnerError(f"The other computer stopped answering: {exc}") from exc
                if not chunk:
                    break
                got += len(chunk)
                # This computer's own disk failing is an OSError, not the other's.
                write(chunk)
        finally:
            conn.close()
        return got


@contextlib.contextmanager
def sent_record(state_dir: str | Path):
    conn = sqlite3.connect(Path(state_dir) / SENT, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def move_root(state_dir: str | Path, old: str, new: str) -> dict[str, int]:
    """The library at *old* is now at *new* (storage/reroot.py): what the
    Secondary confirmed of it, and which of its libraries it is, go with it.
    Left at the old path, an original let go of could not be sent for, and
    the next run sent the whole library again as if it were a new one."""
    if not (Path(state_dir) / SENT).exists():
        return {}
    counts: dict[str, int] = {}
    with sent_record(state_dir) as conn:
        for table in ("sent", "libraries"):
            moved = conn.execute(f"UPDATE OR IGNORE {table} SET root=? WHERE root=?",
                                 (str(new), str(old))).rowcount
            conn.execute(f"DELETE FROM {table} WHERE root=?", (str(old),))
            if moved:
                counts[f"mugil {table}.root"] = int(moved)
    return counts


def confirmed(state_dir: str | Path, root: str) -> dict[str, Any]:
    """How many files of *root* the Secondary has confirmed, and their bytes."""
    if not (Path(state_dir) / SENT).exists():
        return {"files": 0, "bytes": 0}
    with sent_record(state_dir) as conn:
        row = conn.execute("SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes "
                           "FROM sent WHERE root=?", (str(root),)).fetchone()
    return {"files": row["files"], "bytes": row["bytes"]}


def _paired_secondary(hello: dict[str, Any]) -> dict[str, Any]:
    """*hello* when it came from the Secondary, ready or not."""
    if hello.get("role") != "secondary":
        raise PartnerError("The computer at that address is not the Secondary any more.", 409)
    return hello


def _ready(hello: dict[str, Any]) -> dict[str, Any]:
    """*hello* when the Secondary can keep originals now.

    A Secondary that is no longer one is a reason to stop; one whose
    originals disk is unplugged is away, and is waited for like a computer
    that is switched off."""
    if hello.get("role") != "secondary":
        raise PartnerError("The computer at that address is not the Secondary any more.", 409)
    if not hello.get("originals_ready"):
        raise PartnerError("The Secondary's originals folder cannot be reached.", 503)
    if (hello.get("manual") or {}).get("state", "ready") != "ready":
        raise PartnerError("The Secondary is still taking in a copy made by hand.", NOT_YET)
    return hello


def _still_to_send(conn: sqlite3.Connection, state_dir, roots: list[str]) -> int:
    """Bytes of the library the Secondary has not confirmed yet, about."""
    total = 0
    for root in roots:
        row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM assets WHERE root=? AND trashed=0",
                           (str(root),)).fetchone()
        total += int(row[0] or 0) - int(confirmed(state_dir, root)["bytes"])
    return max(0, total)


class Courier:
    """Sends this computer's originals to the Secondary it is paired with."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 hold: Callable[[], str | None] | None = None,
                 partner: Callable[[str, str], Partner] = Partner,
                 notify: Callable[[str, str, str], None] | None = None,
                 file_backups: Callable[[sqlite3.Connection, int], Any] | None = None) -> None:
        self.cfg = cfg
        self.file_backups = file_backups
        self.connect_db = connect_db
        self.hold = hold or (lambda: None)
        self.partner = partner
        self.notify = notify or (lambda event, summary, detail: None)
        self._asked_at = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._keeper: threading.Thread | None = None
        self._stop = threading.Event()
        self._quit = threading.Event()
        self._progress: dict[str, Any] = {}
        self.last: dict[str, Any] = {}
        self._first_copy_at = 0.0
        self._nudge = threading.Event()

    # -- what it is doing ----------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.running, "progress": dict(self._progress),
                    "last": dict(self.last),
                    "every_hours": float(getattr(self.cfg, "mugil_copy_every_hours", 0) or 0)}

    def ready(self) -> str | None:
        """Why a run cannot start, or None."""
        if role(self.cfg) != PRIMARY:
            return "This computer is not the Primary."
        if pairing.load(self.cfg.state_dir) is None or not self.cfg.mugil_partner_url:
            return "Pair this computer with the Secondary first."
        if self.running:
            return "The originals are being sent already."
        return None

    # -- starting and stopping -------------------------------------------------

    def start(self) -> dict[str, Any]:
        problem = self.ready()
        if problem:
            return {"started": False, "reason": problem}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_logged, name="mugil-courier",
                                        daemon=True)
        self._thread.start()
        return {"started": True}

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join:
            self._quit.set()
            self._nudge.set()
            for thread in (self._thread, self._keeper):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=10)

    def keep(self) -> None:
        """Send by itself every ``mugil_copy_every_hours``, while this is the Primary."""
        if self._keeper is not None:
            return
        self._quit.clear()
        self._keeper = threading.Thread(target=self._keep, name="mugil-keeper", daemon=True)
        self._keeper.start()

    def owed(self, now: float | None = None) -> bool:
        every = float(getattr(self.cfg, "mugil_copy_every_hours", 0) or 0) * 3600
        if every <= 0 or self.ready() is not None:
            return False
        now = time.time() if now is None else now
        last = self.last.get("finished") or 0
        wait = every if self.last.get("complete", True) else min(every, RETRY_AFTER)
        return now - last >= wait

    def soon(self, after: float = FIRST_COPY_AFTER) -> None:
        """Just paired: start the first copy in *after* seconds, not at the
        next look."""
        self._first_copy_at = time.time() + after
        self._nudge.set()

    def _keep(self) -> None:
        while not self._quit.is_set():
            wait = LOOK_EVERY
            if self._first_copy_at:
                wait = min(wait, max(0.5, self._first_copy_at - time.time()))
            looked = not self._nudge.wait(wait)
            self._nudge.clear()
            if self._quit.is_set():
                break
            try:
                if self._first_copy_at and time.time() >= self._first_copy_at:
                    self._first_copy_at = 0.0
                    if self.ready() is None:
                        self.start()
                if not looked:
                    continue
                if self.owed():
                    self.start()
                self.look_after_the_secondary()
            except Exception:                            # noqa: BLE001 - keep looking
                log.exception("Could not start sending the originals")

    def look_after_the_secondary(self, now: float | None = None) -> list[str]:
        """Ask after the Secondary now and then, and tell somebody when it has
        been away a day, or its originals disk is failing or nearly full.
        The events sent (the notifier keeps one of each from repeating)."""
        from . import machines                                   # noqa: PLC0415
        now = time.time() if now is None else now
        pair = pairing.load(self.cfg.state_dir)
        if role(self.cfg) != PRIMARY or pair is None or not self.cfg.mugil_partner_url:
            return []
        if not self.running and now - self._asked_at >= ASK_AFTER_EVERY:
            self._asked_at = now
            try:
                hello = self.partner(self.cfg.mugil_partner_url, pair.get("key")).request(
                    "GET", "/api/mugil/hello")
                machines.remember_hello(self.cfg.state_dir, hello)
            except PartnerError:
                pass
        seen = machines.remembered(self.cfg.state_dir) or {}
        name = (seen.get("machine") or {}).get("name") or pair.get("partner_name") or "The Secondary"
        told = []
        since = float(seen.get("seen_at") or pair.get("paired_at") or now)
        if now - since >= AWAY_TOO_LONG:
            self.notify("secondary_away", f"{name} has not answered for a day",
                        f"The Primary last heard from {name} at "
                        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}. "
                        "Originals are not being copied to it until it is back.")
            told.append("secondary_away")
        health = seen.get("health") or {}
        smart = health.get("smart") or {}
        total = int(health.get("total_bytes") or 0)
        free = int(health.get("free_bytes") or 0)
        if smart.get("passed") is False:
            self.notify("secondary_disk", f"The originals disk on {name} reports a problem",
                        "Its own health check failed. Copy the originals to another disk soon.")
            told.append("secondary_disk")
        elif total and free / total < NEARLY_FULL:
            self.notify("secondary_disk", f"The originals disk on {name} is nearly full",
                        f"{free // (1024 ** 3)} GB free of {total // (1024 ** 3)} GB.")
            told.append("secondary_disk")
        return told

    # -- a run -----------------------------------------------------------------

    def _run_logged(self) -> None:
        started = time.time()
        totals = {"sent": 0, "bytes": 0, "already": 0, "changed": 0, "failed": 0,
                  "skipped_libraries": []}
        result: dict[str, Any] = {"started": started, **totals}
        try:
            result.update(self.run(totals))
        except PartnerError as exc:
            result.update(totals, complete=False, error=str(exc))
        except Exception as exc:                          # noqa: BLE001 - said, not raised
            log.exception("Sending the originals stopped")
            result.update(totals, complete=False, error=str(exc))
        result["finished"] = time.time()
        with self._lock:
            self.last = result
            self._progress = {}

    def run(self, totals: dict[str, Any]) -> dict[str, Any]:
        pair = pairing.load(self.cfg.state_dir) or {}
        partner = self.partner(self.cfg.mugil_partner_url, pair.get("key"))
        hello = self._ask(lambda: _paired_secondary(partner.request("GET", "/api/mugil/hello")),
                          partner)
        if hello is None:
            return {"complete": False, "stopped": True}
        from . import machines, wake                             # noqa: PLC0415
        machines.remember_hello(self.cfg.state_dir, hello)
        # What this computer keeps, told first: a Secondary being copied to
        # by hand says where each library goes and whether it fits.
        self._tell_about_this_one(partner)
        if (hello.get("manual") or {}).get("state", "ready") != "ready" \
                or not hello.get("originals_ready"):
            hello = self._ask(lambda: _ready(partner.request("GET", "/api/mugil/hello")), partner)
            if hello is None:
                return {"complete": False, "stopped": True}
            machines.remember_hello(self.cfg.state_dir, hello)
        # When to wake this computer for its catch-up, and how (mugil/wake.py).
        asked = wake.what_to_ask(self.cfg)
        try:
            partner.request("PUT", "/api/mugil/wake", body=asked)
            wake.remember_told(self.cfg.state_dir, asked)
        except PartnerError as exc:
            log.info("The Secondary was not told when to wake this computer: %s", exc)
        self._check_room(hello, totals)
        conn = self.connect_db()
        try:
            # Only the library folders plugged in now: one that is away has
            # nothing to read, and is sent when it is back.
            for root in list(self.cfg.libraries):
                if self._stop.is_set():
                    return {"complete": False, "stopped": True}
                library = library_id.read_id(root)
                if not library:
                    totals["skipped_libraries"].append(str(root))
                    continue
                self._send_root(conn, partner, str(root), library, totals)
            # And what phones sent the Secondary while this computer was away
            # (mugil/inbox.py). A Secondary that cannot hand them over now
            # keeps them for the next run.
            if not self._stop.is_set():
                from . import inbox                              # noqa: PLC0415
                try:
                    totals["inbox"] = inbox.collect(self.cfg, conn, partner,
                                                    file_backups=self.file_backups,
                                                    stop=self._stop)
                except PartnerError as exc:
                    log.info("Could not collect the phones' files from the Secondary: %s", exc)
        finally:
            if threading.current_thread() is self._thread:
                # Each run is a thread of its own: its connection to the
                # index went with it unclosed, one more every run.
                conn.close()
        stopped = self._stop.is_set()
        return {"complete": not stopped and not totals["failed"], "stopped": stopped}

    def _tell_about_this_one(self, partner: Partner) -> None:
        conn = self.connect_db()
        try:
            libraries = []
            for root in list(self.cfg.libraries):
                row = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM assets "
                                   "WHERE root=? AND trashed=0", (str(root),)).fetchone()
                libraries.append({"root": str(root), "name": Path(root).name or "Library",
                                  "files": int(row[0]), "bytes": int(row[1])})
        finally:
            if threading.current_thread() is self._thread:
                conn.close()
        try:
            partner.request("PUT", "/api/mugil/about-primary", body={
                "libraries": libraries, "algo": fingerprints.ALGO, "version": __version__})
        except PartnerError as exc:
            # An older Secondary does not know it; nothing depends on it.
            log.info("The Secondary was not told about this computer: %s", exc)

    def _check_room(self, hello: dict[str, Any], totals: dict[str, Any]) -> None:
        """Say so, before sending, when what is still to send will not fit on
        the Secondary's disk. The run still sends what fits (each original
        there is one more kept twice) and stops when the disk is full."""
        free = (hello.get("keeping") or {}).get("free_bytes")
        if free is None:
            return
        conn = self.connect_db()
        try:
            needed = _still_to_send(conn, self.cfg.state_dir, list(self.cfg.libraries))
        finally:
            if threading.current_thread() is self._thread:
                conn.close()
        from .hold import RESERVE                                # noqa: PLC0415
        if needed > max(0, int(free) - RESERVE):
            totals["short_of_room"] = {"needed": needed, "free": int(free)}
            with self._lock:
                self._progress["short_of_room"] = totals["short_of_room"]
            name = pairing.load(self.cfg.state_dir) or {}
            self.notify("secondary_disk", "The Secondary's disk is too small for the originals",
                        f"{needed // (1024 ** 3)} GB are still to be sent and "
                        f"{int(free) // (1024 ** 3)} GB are free on "
                        f"{name.get('partner_name') or 'the Secondary'}. What fits is sent; "
                        "give it a bigger disk for the rest.")

    def _ask(self, call: Callable[[], Any], partner: Partner) -> Any:
        """*call*, waiting for the Secondary for as long as it is away.

        Once the two are paired the Secondary is the always-on one, and when
        it is not (switched off, restarting, the network down) the Primary
        does not give up: it waits, asks again, and carries on from the file
        it had reached. None when the run was stopped while waiting."""
        pause = AWAY_FIRST
        while True:
            try:
                return call()
            except PartnerError as exc:
                if exc.status not in (*AWAY, NOT_YET):
                    raise
                waiting = "secondary_preparing" if exc.status == NOT_YET else "secondary_away"
                with self._lock:
                    self._progress.setdefault(waiting, time.time())
                log.info("The Secondary is not ready (%s); waiting %.0f s", exc, pause)
            while not self._stop.wait(pause):
                try:
                    _ready(partner.request("GET", "/api/mugil/hello"))
                except PartnerError as exc:
                    if exc.status not in (*AWAY, NOT_YET):
                        raise
                    pause = min(pause * 2, AWAY_LONGEST)
                    continue
                break
            if self._stop.is_set():
                return None
            with self._lock:
                self._progress.pop("secondary_away", None)
                self._progress.pop("secondary_preparing", None)
            pause = AWAY_FIRST

    def _wait_for_the_household(self) -> None:
        while not self._stop.is_set():
            why = self.hold()
            if not why:
                return
            with self._lock:
                self._progress["waiting"] = why
            self._stop.wait(30)
        with self._lock:
            self._progress.pop("waiting", None)

    def _send_root(self, conn: sqlite3.Connection, partner: Partner, root: str,
                   library: str, totals: dict[str, Any]) -> None:
        rows = conn.execute("SELECT id, rel_path, size, mtime FROM assets "
                            "WHERE root=? AND trashed=0 ORDER BY id", (root,)).fetchall()
        name = Path(root).name or "Library"
        # Kept, so an original can be sent for while its disk is away and
        # its library's own marker cannot be read (fetch.py).
        with sent_record(self.cfg.state_dir) as record:
            record.execute("INSERT OR REPLACE INTO libraries (root, library, name) VALUES (?,?,?)",
                           (root, library, name))
        with self._lock:
            self._progress = {"library": root, "files": len(rows), "done": 0}
        for start in range(0, len(rows), GROUP):
            if self._stop.is_set():
                return
            self._wait_for_the_household()
            group = rows[start:start + GROUP]
            known = fingerprints.fingerprinted(conn, root, [r["rel_path"] for r in group])
            items = []
            for row in group:
                rel = row["rel_path"]
                if rel not in known:
                    try:
                        found = fingerprints.fingerprint_file(Path(root) / rel, self._stop.is_set)
                    except (OSError, fingerprints.Unchanged):
                        totals["failed"] += 1
                        continue
                    if found is None:
                        return
                    fingerprints.store(conn, [(row["id"], *found)])
                    known[rel] = (found[0], found[1])
                digest, size = known[rel]
                items.append({"path": rel, "size": size, "algo": fingerprints.ALGO,
                              "digest": digest, "mtime": float(row["mtime"] or 0)})
            if not items:
                continue
            # The name and the times are for a copy made by hand (mugil/adopt.py):
            # the folder it went into, and the times it may have lost.
            asked = {"library": library, "name": name, "items": [
                {k: i[k] for k in ("path", "size", "algo", "digest", "mtime")} for i in items]}
            answer = self._ask(lambda asked=asked: partner.request("POST", "/api/mugil/have", body=asked),
                               partner)
            if answer is None:
                return
            held = list(answer.get("have") or [])
            elsewhere = list(answer.get("elsewhere") or [])
            elsewhere += [False] * (len(items) - len(elsewhere))
            confirmed_now = []
            for index, (item, has) in enumerate(zip(items, held + [False] * (len(items) - len(held)))):
                if self._stop.is_set():
                    break
                if has:
                    totals["already"] += 1
                    confirmed_now.append(item)
                    continue
                if elsewhere[index]:
                    # The same content is kept there already, at another
                    # path: it copies its own, and nothing crosses the network.
                    outcome = self._ask(
                        lambda item=item: self._copy_there(partner, root, library, name, item),
                        partner)
                    if outcome == "kept":
                        totals["copied_there"] = totals.get("copied_there", 0) + 1
                        totals["sent"] += 1
                        confirmed_now.append(item)
                        continue
                    if outcome is None:
                        break
                outcome = self._ask(
                    lambda item=item: self._send_file(partner, root, library, name, item),
                    partner)
                if outcome is None:
                    break
                if outcome == "kept":
                    totals["sent"] += 1
                    totals["bytes"] += item["size"]
                    confirmed_now.append(item)
                elif outcome == "changed":
                    totals["changed"] += 1
                else:
                    totals["failed"] += 1
            self._record(root, confirmed_now)
            with self._lock:
                self._progress["done"] = min(len(rows), start + len(group))
                self._progress.update({k: totals[k] for k in ("sent", "bytes", "already")})

    def _copy_there(self, partner: Partner, root: str, library: str, name: str,
                    item: dict[str, Any]) -> str:
        """Ask the Secondary to keep *item* from the same content it has."""
        try:
            before = os.stat(Path(root) / item["path"])
        except OSError:
            return "failed"
        if before.st_size != item["size"]:
            return "changed"
        try:
            partner.request("POST", "/api/mugil/originals/copy", body={
                "library": library, "name": name, "path": item["path"], "size": item["size"],
                "algo": item["algo"], "digest": item["digest"], "mtime": before.st_mtime})
        except PartnerError as exc:
            if exc.status in (*AWAY, 401, 403, 507):
                raise
            # Anything else (the kept copy went bad, an older Secondary that
            # does not know this): the file is sent as usual.
            return "send"
        return "kept"

    def _send_file(self, partner: Partner, root: str, library: str, name: str,
                   item: dict[str, Any]) -> str:
        path = Path(root) / item["path"]
        try:
            before = os.stat(path)
            if before.st_size != item["size"]:
                return "changed"
            with open(path, "rb") as handle:
                partner.request(
                    "PUT", "/api/mugil/originals",
                    query={"library": library, "name": name, "path": item["path"]},
                    body=handle, length=item["size"],
                    # Signed with the request, so nothing between the two
                    # computers can change what the file is said to be.
                    digest=f"{item['algo']}:{item['digest']}:{item['size']}:{before.st_mtime!r}",
                    headers={"Content-Type": "application/octet-stream"})
        except PartnerError as exc:
            if exc.status == 422:
                return "changed"
            if exc.status in (*AWAY, 401, 403, 404, 507):
                # Not this file's fault: the Secondary is away (the run waits
                # for it, :meth:`_ask`), full, or no longer paired (the run
                # stops and says why).
                raise
            log.warning("The Secondary did not keep %s: %s", path, exc)
            return "failed"
        except OSError as exc:
            log.warning("Could not read %s to send it: %s", path, exc)
            return "failed"
        return "kept"

    def _record(self, root: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        now = time.time()
        with sent_record(self.cfg.state_dir) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO sent (root, rel_path, size, mtime, algo, digest, sent_at) "
                "VALUES (?,?,?,?,?,?,?)",
                [(root, i["path"], i["size"], i["mtime"], i["algo"], i["digest"], now)
                 for i in items])
