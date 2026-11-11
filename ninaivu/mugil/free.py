"""On the Primary: bringing back the originals that were let go.

Up to 4.0.0 the Primary could free space by letting originals go that the
Secondary and the off-site copy both kept, and show them from a smaller
display copy (display.py). The household found those copies poor to look
at, and from 4.0.1 **nothing is let go and no smaller copy is made**: the
Primary keeps, and shows, its originals.

What is left here is the way back for a library that used it. Each original
let go is written down in the index (``mugil_let_go``); **Bring every
original back** copies each one from the Secondary to the place it had,
with its own modified time, checked against its fingerprint, and forgets
that it was let go. It runs as one piece of work with its progress on the
Two computers page, stops when the disk has too little room left, and can
be started again. Until an original is back it is still found: opened, it
is sent for from the Secondary (fetch.py).

When none is left, the display copies have nothing to stand in for and
their folder is removed (:func:`clear_display_copies`).
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

from . import pairing
from .courier import Partner, PartnerError
from ..words import said
from .hold import hasher

log = logging.getLogger(__name__)

#: Said when somebody still asks for it.
REMOVED = ("Freeing space was removed in 4.0.1: this computer keeps its originals. "
           "Bring back the ones that were let go.")
#: Bringing back stops when the disk would have less than this left.
KEEP_FREE_BYTES = 2 * 1024 ** 3


def wanted(cfg) -> bool:
    """Whether originals may be let go: never, since 4.0.1."""
    return False


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
    """Brings every let-go original back, one at a time, as one piece of work.

    (Named for what it was: until 4.0.0 it let originals go.)"""

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
        return {"on": False, "running": self.running, "let_go": gone, **self.live}

    def start(self) -> dict[str, Any]:
        """Letting originals go: removed."""
        raise PartnerError(REMOVED, 410)

    def bring_all(self) -> dict[str, Any]:
        """Start bringing every let-go original back; the status."""
        with self._lock:
            if pairing.load(self.cfg.state_dir) is None or not self.cfg.mugil_partner_url:
                raise PartnerError("This computer is not paired with a Secondary.", 409)
            if not self.running:
                self._stop.clear()
                self.live = {"started": time.time(), "back": 0, "back_bytes": 0, "missed": 0,
                             "error": ""}
                self._thread = threading.Thread(target=self._run_logged, name="mugil-bring-back",
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
            log.exception("Bringing the originals back stopped")
            self.live["error"] = str(exc)
        finally:
            self.live["finished"] = time.time()
            self.live.pop("current", None)

    def run(self) -> dict[str, Any]:
        """Bring back every original that can be; the totals."""
        if not self.live:
            self.live = {"back": 0, "back_bytes": 0, "missed": 0, "error": ""}
        conn = self.connect_db()
        try:
            waiting = let_go(conn)
            self.live["total"] = len(waiting)
            for item in waiting:
                if self._stop.is_set():
                    break
                self._pause()
                root, rel, size = item["root"], item["rel_path"], int(item["size"] or 0)
                self.live["current"] = rel
                if not _room_for(Path(root), size):
                    self.live["error"] = said("This computer's disk is nearly full: the rest of the originals stay on the Secondary until there is room.")  # noqa: E501
                    break
                if bring_back(self.cfg, conn, root, rel):
                    self.live["back"] += 1
                    self.live["back_bytes"] += size
                else:
                    self.live["missed"] += 1
            if summary(conn)["files"] == 0:
                clear_display_copies(self.cfg.state_dir, conn)
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


def _room_for(root: Path, size: int) -> bool:
    """Whether *root*'s disk takes *size* more and keeps :data:`KEEP_FREE_BYTES`."""
    try:
        return shutil.disk_usage(root).free - size >= KEEP_FREE_BYTES
    except OSError:
        return True                     # cannot tell: the copy itself will say


def clear_display_copies(state_dir: str | Path, conn: sqlite3.Connection) -> bool:
    """Remove the smaller copies once no original is on the Secondary only:
    they stood in for those, and for nothing else. Whether they were removed."""
    from . import display                                            # noqa: PLC0415
    try:
        if summary(conn)["files"]:
            return False
    except sqlite3.Error:
        return False
    folder = display.folder(state_dir)
    ledger = Path(state_dir) / display.LEDGER
    if not folder.exists() and not ledger.exists():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    for name in (display.LEDGER, display.LEDGER + "-wal", display.LEDGER + "-shm"):
        (Path(state_dir) / name).unlink(missing_ok=True)
    log.info("The smaller display copies were removed: every original is on this computer")
    return True


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


__all__ = ["Freeing", "REMOVED", "bring_back", "bring_back_to_delete", "clear_display_copies", "let_go", "summary", "wanted", "fingerprint"]
