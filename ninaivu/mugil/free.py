"""On the Primary: free space by letting originals go that the Secondary keeps.

This is the one step of the two-computer design that removes a file from the
Mac, so it is off until the household turns it on (``mugil_free_space``) and
then runs only when somebody presses Free. The plan (optimize.py) says which
originals could go; before each one goes, every condition is checked again,
on the file itself, on both computers:

* the original here still has the size, time and fingerprint the Secondary
  confirmed (it is read again, end to end);
* the Secondary reads its kept copy again, end to end, and it is the same;
* the off-site copy holds it at that size and time;
* it has a display copy here, so it can still be seen;
* it has been on the Secondary for :data:`~optimize.HOLD_DAYS` days, is not
  from the last :data:`~optimize.IN_USE_DAYS` days and is nobody's favourite.

Only then is it written down as let go (``mugil_let_go`` in the index) and
removed. The library keeps it: its thumbnail, display copy, names, albums and
everything else stay, and the original is sent for from the Secondary when
somebody needs it (fetch.py). A rescan does not take a let-go file for a
missing one.

Bring back puts an original back where it was, from the Secondary, checked
against its fingerprint and with its own time, and forgets that it was let go.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import PRIMARY, pairing, role
from .courier import TIMEOUT, Partner, PartnerError
from .hold import hasher

log = logging.getLogger(__name__)

#: Why an original that the plan called ready was kept after all.
KEPT_BECAUSE = ("changed_here", "not_same_there", "secondary_away", "no_display_copy",
                "could_not_remove")


def wanted(cfg) -> bool:
    return role(cfg) == PRIMARY and bool(getattr(cfg, "mugil_free_space", False))


def fingerprint(path: Path, algo: str) -> str | None:
    fresh = hasher(algo)
    if fresh is None:
        return None
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            fresh.update(chunk)
    return fresh.hexdigest()


def let_go(conn: sqlite3.Connection, root: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM mugil_let_go" + (" WHERE root=?" if root else "") + " ORDER BY let_go_at"
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, (root,) if root else ())]
    finally:
        conn.row_factory = previous


def summary(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM mugil_let_go").fetchone()
    return {"files": int(row[0]), "bytes": int(row[1])}


def _library(state_dir: str | Path, root: str) -> str | None:
    from .courier import SENT, sent_record                        # noqa: PLC0415
    if not (Path(state_dir) / SENT).exists():
        return None
    with sent_record(state_dir) as conn:
        row = conn.execute("SELECT library FROM libraries WHERE root=?", (str(root),)).fetchone()
    return row["library"] if row else None


class Freeing:
    """Lets the ready originals go, one at a time, when somebody presses Free."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 partner: Callable[..., Partner] = Partner,
                 hold: Callable[[], str | None] | None = None) -> None:
        self.cfg = cfg
        self.connect_db = connect_db
        self.partner = partner
        self.hold = hold or (lambda: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.live: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        # The index's connection is this thread's own (db.connect): not closed here.
        gone = summary(self.connect_db())
        return {"on": bool(getattr(self.cfg, "mugil_free_space", False)),
                "running": self.running, "let_go": gone, **self.live}

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not wanted(self.cfg):
                raise PartnerError("Freeing space is off. Turn it on first.", 409)
            if pairing.load(self.cfg.state_dir) is None or not self.cfg.mugil_partner_url:
                raise PartnerError("This computer is not paired with a Secondary.", 409)
            if not self.running:
                self._stop.clear()
                self.live = {"started": time.time(), "freed": 0, "freed_bytes": 0,
                             "kept": {k: 0 for k in KEPT_BECAUSE}, "error": ""}
                self._thread = threading.Thread(target=self._run_logged, name="mugil-free",
                                                daemon=True)
                self._thread.start()
        return self.status()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        thread = self._thread
        if join and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _run_logged(self) -> None:
        try:
            self.run()
        except Exception as exc:                                    # noqa: BLE001 - said in status
            log.exception("Freeing space stopped")
            self.live["error"] = str(exc)
        finally:
            self.live["finished"] = time.time()
            self.live.pop("current", None)

    def run(self, *, limit: int | None = None, now: float | None = None) -> dict[str, Any]:
        """Let every ready original go that passes its checks; the totals."""
        from . import optimize                                       # noqa: PLC0415
        if not self.live:
            self.live = {"freed": 0, "freed_bytes": 0, "kept": {k: 0 for k in KEPT_BECAUSE},
                         "error": ""}
        if not wanted(self.cfg):
            raise PartnerError("Freeing space is off. Turn it on first.", 409)
        pair = pairing.load(self.cfg.state_dir) or {}
        partner = self.partner(self.cfg.mugil_partner_url, pair.get("key"), timeout=TIMEOUT)
        conn = self.connect_db()
        try:
            ready = optimize.ready(conn, self.cfg.state_dir, list(self.cfg.roots), now=now,
                                   limit=limit)
            self.live["ready"] = len(ready)
            libraries: dict[str, str | None] = {}
            for item in ready:
                # Turned off part-way (the switch, or this computer's part):
                # nothing more goes.
                if self._stop.is_set() or not wanted(self.cfg):
                    break
                self._pause()
                root = item["root"]
                if root not in libraries:
                    libraries[root] = _library(self.cfg.state_dir, root)
                why = self._one(conn, partner, libraries[root], item)
                if why is None:
                    self.live["freed"] += 1
                    self.live["freed_bytes"] += int(item["size"] or 0)
                else:
                    self.live["kept"][why] += 1
                    if why == "secondary_away":
                        self.live["error"] = "The Secondary did not answer; nothing more was let go."
                        break
        finally:
            if threading.current_thread() is self._thread:
                conn.close()                      # this run's own thread is ending
        return dict(self.live)

    def _pause(self) -> None:
        while True:
            why = self.hold()
            if not why:
                self.live.pop("waiting", None)
                return
            self.live["waiting"] = why
            if self._stop.wait(30):
                return

    def _one(self, conn: sqlite3.Connection, partner: Partner, library: str | None,
             item: dict[str, Any]) -> str | None:
        """Let *item* go when every check passes; None, or why it stayed."""
        from . import display                                        # noqa: PLC0415
        root, rel = item["root"], item["rel_path"]
        path = Path(root) / rel
        self.live["current"] = rel
        algo, digest, size = item["algo"], item["digest"], int(item["size"] or 0)
        if display.find(self.cfg.state_dir, digest) is None:
            return "no_display_copy"
        try:
            st = path.stat()
            if (not stat.S_ISREG(st.st_mode) or path.is_symlink() or st.st_size != size
                    or abs(st.st_mtime - float(item["mtime"])) >= 0.001
                    or fingerprint(path, algo) != digest):
                return "changed_here"
        except OSError:
            return "changed_here"
        if not library:
            return "not_same_there"
        try:
            answer = partner.request("POST", "/api/mugil/originals/verify", body={
                "library": library, "path": rel, "size": size, "algo": algo, "digest": digest})
        except PartnerError as exc:
            return "not_same_there" if exc.status in (400, 404, 409) else "secondary_away"
        if answer.get("same") is not True:
            return "not_same_there"
        # Once more, after the Secondary's answer: nothing changed here meanwhile.
        try:
            st2 = path.stat()
        except OSError:
            return "changed_here"
        if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
            return "changed_here"
        conn.execute("INSERT OR REPLACE INTO mugil_let_go(root, rel_path, size, mtime, algo, digest, "
                     "let_go_at) VALUES (?,?,?,?,?,?,?)",
                     (root, rel, size, float(item["mtime"]), algo, digest, time.time()))
        conn.commit()
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Could not let %s go: %s", rel, exc)
            conn.execute("DELETE FROM mugil_let_go WHERE root=? AND rel_path=?", (root, rel))
            conn.commit()
            return "could_not_remove"
        log.info("Let %s go; the Secondary keeps it", rel)
        return None


def bring_back(cfg, conn: sqlite3.Connection, root: str, rel: str, *, partner=None) -> bool:
    """Put a let-go original back where it was; whether it is back."""
    from . import fetch                                              # noqa: PLC0415
    row = conn.execute("SELECT size, mtime, algo, digest FROM mugil_let_go "
                       "WHERE root=? AND rel_path=?", (root, rel)).fetchone()
    if row is None:
        return False
    size, mtime, algo, digest = int(row[0]), float(row[1]), str(row[2]), str(row[3])
    target = Path(root) / rel
    if target.exists():
        try:
            same = target.stat().st_size == size and fingerprint(target, algo) == digest
        except OSError:
            same = False
        if not same:
            log.warning("Something else is at %s now; the original was not brought back", rel)
            return False
    else:
        fetched = fetch.original(cfg, root, rel, partner=partner)
        if fetched is None:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.ninaivu-partial-{secrets.token_hex(4)}")
        try:
            shutil.copyfile(fetched, partial)
            os.chmod(partial, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            if partial.stat().st_size != size or fingerprint(partial, algo) != digest:
                return False
            os.utime(partial, (mtime, mtime))
            os.replace(partial, target)
        except OSError as exc:
            log.warning("Could not bring %s back: %s", rel, exc)
            return False
        finally:
            partial.unlink(missing_ok=True)
    conn.execute("DELETE FROM mugil_let_go WHERE root=? AND rel_path=?", (root, rel))
    conn.commit()
    return True


def bring_back_to_delete(cfg, conn: sqlite3.Connection, asset_ids) -> int:
    """Bring back the let-go originals among *asset_ids*, so a delete moves
    them to the recycle bin like any other file and they can be put back from
    it. One the Secondary cannot send now stays let go, and the recycle bin
    refuses it with a reason (storage/recycle.py). How many came back."""
    ids = [int(i) for i in asset_ids]
    back = 0
    for start in range(0, len(ids), 500):
        piece = ids[start:start + 500]
        try:
            rows = conn.execute(
                "SELECT a.root, a.rel_path FROM assets a JOIN mugil_let_go g "
                "ON g.root=a.root AND g.rel_path=a.rel_path "
                f"WHERE a.id IN ({','.join('?' * len(piece))})", piece).fetchall()
        except sqlite3.OperationalError:
            return back                    # an index made before the table was
        for root, rel in rows:
            if bring_back(cfg, conn, root, rel):
                back += 1
    return back


__all__ = ["Freeing", "bring_back", "bring_back_to_delete", "let_go", "summary", "wanted", "fingerprint", "KEPT_BECAUSE"]
