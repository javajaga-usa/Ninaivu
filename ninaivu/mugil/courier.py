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

from . import PRIMARY, pairing, role
from ..storage import fingerprints, library_id

log = logging.getLogger(__name__)

#: Files asked about at a time.
GROUP = 200

#: How long one request may take to start answering, in seconds.
TIMEOUT = 60

#: How often the keeper looks at whether a run is owed, in seconds.
LOOK_EVERY = 10 * 60.0

#: After a run that did not finish, how long before one is tried by itself.
RETRY_AFTER = 60 * 60.0

#: While the Secondary is away: the first wait before asking again, and the
#: longest, in seconds. It doubles in between.
AWAY_FIRST, AWAY_LONGEST = 30.0, 10 * 60.0

#: Answers that mean the Secondary is away (switched off, asleep, restarting,
#: off the network) rather than that it refused: the run waits for it.
AWAY = (0, 502, 503, 504)

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


def confirmed(state_dir: str | Path, root: str) -> dict[str, Any]:
    """How many files of *root* the Secondary has confirmed, and their bytes."""
    if not (Path(state_dir) / SENT).exists():
        return {"files": 0, "bytes": 0}
    with sent_record(state_dir) as conn:
        row = conn.execute("SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes "
                           "FROM sent WHERE root=?", (str(root),)).fetchone()
    return {"files": row["files"], "bytes": row["bytes"]}


def _ready(hello: dict[str, Any]) -> dict[str, Any]:
    """*hello* when the Secondary can keep originals now.

    A Secondary that is no longer one is a reason to stop; one whose
    originals disk is unplugged is away, and is waited for like a computer
    that is switched off."""
    if hello.get("role") != "secondary":
        raise PartnerError("The computer at that address is not the Secondary any more.", 409)
    if not hello.get("originals_ready"):
        raise PartnerError("The Secondary's originals folder cannot be reached.", 503)
    return hello


class Courier:
    """Sends this computer's originals to the Secondary it is paired with."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 hold: Callable[[], str | None] | None = None,
                 partner: Callable[[str, str], Partner] = Partner) -> None:
        self.cfg = cfg
        self.connect_db = connect_db
        self.hold = hold or (lambda: None)
        self.partner = partner
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._keeper: threading.Thread | None = None
        self._stop = threading.Event()
        self._quit = threading.Event()
        self._progress: dict[str, Any] = {}
        self.last: dict[str, Any] = {}

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

    def _keep(self) -> None:
        while not self._quit.wait(LOOK_EVERY):
            try:
                if self.owed():
                    self.start()
            except Exception:                            # noqa: BLE001 - keep looking
                log.exception("Could not start sending the originals")

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
        hello = self._ask(lambda: _ready(partner.request("GET", "/api/mugil/hello")), partner)
        if hello is None:
            return {"complete": False, "stopped": True}
        conn = self.connect_db()
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
        stopped = self._stop.is_set()
        return {"complete": not stopped and not totals["failed"], "stopped": stopped}

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
                if exc.status not in AWAY:
                    raise
                with self._lock:
                    self._progress.setdefault("secondary_away", time.time())
                log.info("The Secondary is not answering (%s); waiting %.0f s", exc, pause)
            while not self._stop.wait(pause):
                try:
                    _ready(partner.request("GET", "/api/mugil/hello"))
                except PartnerError as exc:
                    if exc.status not in AWAY:
                        raise
                    pause = min(pause * 2, AWAY_LONGEST)
                    continue
                break
            if self._stop.is_set():
                return None
            with self._lock:
                self._progress.pop("secondary_away", None)
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
            asked = {"library": library, "items": [
                {k: i[k] for k in ("path", "size", "algo", "digest")} for i in items]}
            answer = self._ask(lambda asked=asked: partner.request("POST", "/api/mugil/have", body=asked),
                               partner)
            if answer is None:
                return
            held = list(answer.get("have") or [])
            confirmed_now = []
            for item, has in zip(items, held + [False] * (len(items) - len(held))):
                if self._stop.is_set():
                    break
                if has:
                    totals["already"] += 1
                    confirmed_now.append(item)
                    continue
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
