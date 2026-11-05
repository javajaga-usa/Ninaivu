"""On the Primary: bringing in the photographs only the Secondary has.

The Secondary finds them (mugil/survey.py): files in its library folders the
Primary never had, and its own versions of files the Primary sent different
ones of. This copies each into the library here, so the family sees it:

* at the same path in the library, written as the index spells that path;
* beside the Primary's own file, as ``<name> (from <Secondary>).<ext>``, when
  the Primary has a different file at that path: neither is lost;
* not at all when the Primary has that very file there already, or when it
  is a photograph somebody here deleted or erased (the recycle bin's record),
  which is not brought back;
* checked against the fingerprint the Secondary worked out as it arrives,
  written under a temporary name and renamed, with its own modified time.

A sidecar (``IMG_1.jpg.xmp``) comes with its photograph and follows it when
the photograph is put beside another; reading it back gives the photograph
its names, albums and stars here (storage/xmp_read.py). Each library that
was given files is scanned afterwards.

Nothing is removed or replaced on either computer. It is only offered after a
complete run of sending the originals, when everything the Primary has is
kept on the Secondary, so a photograph the Primary has under another name is
known by its content and not brought in twice.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import pairing
from .courier import AWAY, AWAY_FIRST, AWAY_LONGEST, TIMEOUT, Partner, PartnerError
from .hold import clean_path, hasher
from .rebuild import _fingerprint, index_spellings
from .survey import BROUGHT
from ..storage import library_id

log = logging.getLogger(__name__)

PAGE = 500
AWAY_SAID = {"reason": "The Secondary is not answering. Bringing the files in carries on when it is back."}


def _besides(target: Path, who: str):
    """The names beside *target*, in order: ``<name> (from <who>).<ext>``, then
    ``(from <who> 2)`` and on."""
    stem, suffix = target.name, ""
    if "." in target.name.lstrip("."):
        stem, _, ext = target.name.rpartition(".")
        suffix = f".{ext}"
    n = 1
    while True:
        tag = f" (from {who})" if n == 1 else f" (from {who} {n})"
        yield target.with_name(f"{stem}{tag}{suffix}")
        n += 1


def beside(target: Path, who: str) -> Path:
    """Where the Secondary's version of *target* goes: beside it, never on it."""
    for candidate in _besides(target, who):
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise AssertionError("unreachable")


def _same(path: Path, size: int, algo: str, digest: str) -> bool:
    try:
        return (path.is_file() and not path.is_symlink() and path.stat().st_size == size
                and _fingerprint(path, algo) == digest)
    except OSError:
        return False


def _deleted_here(conn: sqlite3.Connection | None, root: str, rel: str, size: int) -> bool:
    """Whether somebody here deleted or erased the photograph at *rel*."""
    if conn is None:
        return False
    try:
        if conn.execute("SELECT 1 FROM recycled WHERE root = ? AND rel_path = ? "
                        "AND restored_at IS NULL LIMIT 1", (root, rel)).fetchone():
            return True
        return conn.execute("SELECT 1 FROM erased WHERE root = ? AND rel_path = ? AND size = ? "
                            "LIMIT 1", (root, rel, size)).fetchone() is not None
    except sqlite3.Error:
        return False


class BringIn:
    """Copies the Secondary's own files into this computer's libraries."""

    def __init__(self, cfg, *, partner: Callable[..., Partner] = Partner,
                 hold: Callable[[], str | None] | None = None,
                 done: Callable[[str], Any] | None = None) -> None:
        self.cfg = cfg
        self.partner = partner
        self.hold = hold or (lambda: None)
        self.done = done
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.live: dict[str, Any] = {}
        #: When this computer last asked the Secondary to look (its own clock).
        self.asked_at = 0.0

    # -- talking to the Secondary -------------------------------------------

    def _partner(self) -> Partner:
        pair = pairing.load(self.cfg.state_dir) or {}
        if not pair.get("key") or not self.cfg.mugil_partner_url:
            raise PartnerError("Pair this computer with the Secondary first.", 409)
        return self.partner(self.cfg.mugil_partner_url, pair.get("key"), timeout=TIMEOUT)

    def look(self) -> dict[str, Any]:
        """Ask the Secondary to look for the files only it has."""
        answer = self._partner().request("POST", "/api/mugil/survey")
        self.asked_at = time.time()
        return answer

    def found(self) -> dict[str, Any]:
        """How the Secondary's look is going, and what it found."""
        return self._partner().request("GET", "/api/mugil/survey")

    # -- what it is doing ----------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self.live, "running": self.running, "asked_at": self.asked_at or None}

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise PartnerError("The files are being brought in already.", 409)
            self._stop.clear()
            self.live = {"started": time.time(), "copied": 0, "copied_bytes": 0, "beside": 0,
                         "already": 0, "deleted_here": 0, "not_a_library_here": 0,
                         "not_same": 0, "libraries": []}
            self._thread = threading.Thread(target=self._run_logged, daemon=True,
                                            name="ninaivu-mugil-bring-in")
            self._thread.start()
        return self.status()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=30)

    def _run_logged(self) -> None:
        try:
            self.run()
        except PartnerError as exc:
            self.live["error"] = str(exc)
        except Exception as exc:                          # noqa: BLE001 — said, not raised
            log.exception("Bringing in the Secondary's files stopped")
            self.live["error"] = str(exc)
        self.live["finished"] = time.time()

    # -- the run -------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        for key in ("copied", "copied_bytes", "beside", "already", "deleted_here",
                    "not_a_library_here", "not_same"):
            self.live[key] = 0
        self.live["libraries"] = []
        for key in ("complete", "error", "finished", "current", "waiting"):
            self.live.pop(key, None)
        partner = self._partner()
        hello = self._ask(lambda: partner.request("GET", "/api/mugil/hello")) or {}
        who = str(hello.get("name") or "the Secondary").strip() or "the Secondary"
        roots = {library_id.read_id(root): str(root) for root in self.cfg.libraries}
        roots.pop(None, None)
        db_path = Path(getattr(self.cfg, "db_path", "") or "")
        conn = sqlite3.connect(db_path, timeout=30) if db_path.is_file() else None
        try:
            given: list[str] = []
            after = ""
            spelled: dict[str, dict[str, str]] = {}
            moved: dict[tuple[str, str], str] = {}     # a photograph put beside: where
            while not self._stop.is_set():
                page = self._ask(lambda after=after: partner.request(
                    "GET", "/api/mugil/survey/items",
                    query={"kinds": ",".join(BROUGHT), "after": after, "limit": PAGE}))
                if page is None:
                    break
                items = [i for i in page.get("items") or [] if isinstance(i, dict)]
                if not items:
                    self.live["complete"] = True
                    break
                for item in items:
                    if self._stop.is_set():
                        break
                    self._pause()
                    root = roots.get(str(item.get("library") or ""))
                    if root is None or not Path(root).is_dir():
                        # A library this computer does not have, or one whose
                        # disk is away now: nothing to put it in.
                        self.live["not_a_library_here"] += 1
                        continue
                    if root not in spelled:
                        spelled[root] = index_spellings(self.cfg, Path(root))
                    if self._one(partner, conn, root, spelled[root], item, who, moved) \
                            and root not in given:
                        given.append(root)
                after = str(items[-1].get("path", ""))
            self.live["libraries"] = given
        finally:
            if conn is not None:
                conn.close()
        for root in given if self.live.get("complete") else []:
            if self.done is not None:
                self.done(root)
        return dict(self.live)

    def _ask(self, call: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        wait = AWAY_FIRST
        while not self._stop.is_set():
            try:
                answer = call()
                self.live.pop("waiting", None)
                return answer
            except PartnerError as exc:
                if exc.status not in AWAY:
                    raise
                self.live["waiting"] = AWAY_SAID
                if self._stop.wait(wait):
                    return None
                wait = min(wait * 2, AWAY_LONGEST)
        return None

    def _pause(self) -> None:
        while True:
            why = self.hold()
            if not why:
                return
            self.live["waiting"] = why
            if self._stop.wait(30):
                return

    def _one(self, partner: Partner, conn: sqlite3.Connection | None, root: str,
             spelled: dict[str, str], item: dict[str, Any], who: str,
             moved: dict[tuple[str, str], str]) -> bool:
        """Bring one file in; whether a file was written."""
        rel = clean_path(str(item.get("rel_path", "")))
        if rel is None:
            return False
        size, algo, digest = int(item.get("size") or 0), str(item.get("algo", "")), \
            str(item.get("digest", ""))
        mtime = float(item.get("mtime") or 0)
        base = Path(root)
        self.live["current"] = rel
        if rel.lower().endswith(".xmp") and (root, rel[:-4]) in moved:
            # The sidecar of a photograph put beside the Primary's: beside too.
            target = base / f"{moved[(root, rel[:-4])]}.xmp"
        else:
            target = base / spelled.get(rel, rel)
        if _deleted_here(conn, root, spelled.get(rel, rel), size):
            self.live["deleted_here"] += 1
            return False
        if target.exists() or target.is_symlink() or item.get("kind") == "other_version":
            if _same(target, size, algo, digest):
                self.live["already"] += 1
                return False
            if target.exists() or target.is_symlink():
                # Brought in beside it before: that copy is this file.
                for earlier in _besides(target, who):
                    if not earlier.exists() and not earlier.is_symlink():
                        break
                    if _same(earlier, size, algo, digest):
                        moved[(root, rel)] = earlier.relative_to(base).as_posix()
                        self.live["already"] += 1
                        return False
                target = beside(target, who)
                moved[(root, rel)] = target.relative_to(base).as_posix()
                self.live["beside"] += 1
        fresh = {"now": hasher(algo)}
        if fresh["now"] is None:
            self.live["not_same"] += 1
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.ninaivu-partial-{secrets.token_hex(4)}")
        try:
            with open(partial, "xb") as handle:
                def write(chunk: bytes, handle=handle) -> None:
                    fresh["now"].update(chunk)
                    handle.write(chunk)
                got = None
                wait = AWAY_FIRST
                while got is None and not self._stop.is_set():
                    try:
                        got = partner.download("/api/mugil/survey/file",
                                               query={"path": str(item.get("path", ""))},
                                               write=write)
                    except PartnerError as exc:
                        if exc.status not in AWAY:
                            log.info("The Secondary did not send %s: %s", rel, exc)
                            self.live["not_same"] += 1
                            return False
                        handle.seek(0)
                        handle.truncate()
                        fresh["now"] = hasher(algo)
                        self.live["waiting"] = AWAY_SAID
                        if self._stop.wait(wait):
                            return False
                        wait = min(wait * 2, AWAY_LONGEST)
                if got is None:
                    return False
                self.live.pop("waiting", None)
                handle.flush()
                os.fsync(handle.fileno())
            if got != size or fresh["now"].hexdigest() != digest:
                log.warning("%s did not arrive as the Secondary keeps it; not kept", rel)
                self.live["not_same"] += 1
                return False
            os.chmod(partial, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            os.utime(partial, (mtime, mtime))
            if target.exists() or target.is_symlink():
                self.live["already"] += 1                # somebody put one there meanwhile
                return False
            os.replace(partial, target)
        except OSError as exc:
            log.warning("Could not bring %s in: %s", rel, exc)
            self.live["error"] = str(exc)
            self._stop.set()
            return False
        finally:
            partial.unlink(missing_ok=True)
        self.live["copied"] += 1
        self.live["copied_bytes"] += size
        return True


def ready(cfg, courier) -> str | None:
    """Why the files cannot be brought in yet, or None."""
    from . import PRIMARY, role                                   # noqa: PLC0415
    if role(cfg) != PRIMARY:
        return "This computer is not the Primary."
    if pairing.load(cfg.state_dir) is None or not cfg.mugil_partner_url:
        return "Pair this computer with the Secondary first."
    last = (courier.status().get("last") or {}) if courier is not None else {}
    if not last.get("complete"):
        return NOT_YET["reason"]
    return None


#: As a "reason", so tools/server_messages.py collects it for translation.
NOT_YET = {"reason": "Wait for a complete run of sending the originals first: until then a "
                     "photograph this computer has may look as if only the Secondary had it."}


__all__ = ["BringIn", "beside", "ready"]
