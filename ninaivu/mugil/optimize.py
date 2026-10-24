"""Optimize Mac storage: which originals the Primary could let go, and why
the rest stay. It only reads.

The agreed design keeps on the Primary the index, the thumbnails, a display
copy of everything, and the full originals still *in use*: the last
:data:`IN_USE_DAYS` days of photographs and videos, and everybody's
favourites. Every other original could live on the Secondary only, and be
sent for when somebody needs it (fetch.py).

No original is let go until all of these hold, and this says, for every
original, the first that does not:

* the Secondary has confirmed it, with the fingerprint the file has now
  (``mugil-sent.db``);
* the off-site copy (Backblaze B2 or another, cloud/offsite.py) holds it at
  its size and time now, so there are two copies away from this computer;
* it was confirmed on the Secondary at least :data:`HOLD_DAYS` days ago;
* it has a display copy (an original that is its own display copy, such as
  an iPhone's HEIC, stays).

Nothing here deletes, moves or changes a file: it is the plan the household
reads before saying whether space should be freed at all.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .courier import SENT
from .display import LEDGER

#: Captured within this many days: in use, kept here in full.
IN_USE_DAYS = 365
#: How long an original must have been on the Secondary before it may go.
HOLD_DAYS = 30

#: Why an original stays, in the order each is judged.
REASONS = ("in_use", "favourite", "not_on_secondary", "not_off_site", "held", "no_display_copy")


def plan(conn: sqlite3.Connection, state_dir: str | Path, roots: Iterable[str], *,
         now: float | None = None) -> dict[str, Any]:
    """Counts and bytes of the originals that could go, and of those that
    stay, by the first reason they stay."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"ready": {"files": 0, "bytes": 0},
                           "staying": {r: {"files": 0, "bytes": 0} for r in REASONS},
                           "let_go": {"files": 0, "bytes": 0},
                           "files": 0, "bytes": 0, "in_use_days": IN_USE_DAYS,
                           "hold_days": HOLD_DAYS}
    for row in _rows(conn, state_dir, roots, now):
        size = int(row["size"] or 0)
        out["files"] += 1
        out["bytes"] += size
        if row["gone"]:
            bucket = out["let_go"]
        else:
            why = _why(row, now)
            bucket = out["ready"] if why is None else out["staying"][why]
        bucket["files"] += 1
        bucket["bytes"] += size
    return out


def ready(conn: sqlite3.Connection, state_dir: str | Path, roots: Iterable[str], *,
          now: float | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """The originals that may go now, oldest first, with what the Secondary
    confirmed of each."""
    now = time.time() if now is None else now
    out = []
    for row in _rows(conn, state_dir, roots, now, detail=True):
        if not row["gone"] and _why(row, now) is None:
            out.append(dict(row))
            if limit is not None and len(out) >= limit:
                break
    return out


def _rows(conn: sqlite3.Connection, state_dir: str | Path, roots: Iterable[str], now: float,
          *, detail: bool = False) -> list[sqlite3.Row]:
    state = Path(state_dir)
    roots = [str(r) for r in roots]
    if not roots:
        return []
    since = time.strftime("%Y-%m-%d", time.localtime(now - IN_USE_DAYS * 86400))
    attached = []
    for alias, name in (("sent", SENT), ("display", LEDGER)):
        path = state / name
        if path.exists():
            conn.execute(f"ATTACH DATABASE ? AS mugil_{alias}", (str(path),))
            attached.append(alias)
    try:
        sent = ("LEFT JOIN mugil_sent.sent s ON s.root=a.root AND s.rel_path=a.rel_path "
                "AND s.size=a.size AND ABS(s.mtime - a.mtime) < 0.001"
                if "sent" in attached else "")
        display = ("LEFT JOIN mugil_display.copies c ON c.digest=s.digest"
                   if "display" in attached and "sent" in attached else "")
        offsite = _has_table(conn, "offsite_copies")
        marks = ",".join("?" * len(roots))
        columns = [
            "a.size AS size",
            "a.date_key >= ? AS recent",
            "EXISTS (SELECT 1 FROM user_assets u WHERE u.asset_id=a.id AND u.favorite=1) AS fav"
            if _has_table(conn, "user_assets") else "0 AS fav",
            "s.sent_at AS sent_at" if sent else "NULL AS sent_at",
            ("EXISTS (SELECT 1 FROM offsite_copies o WHERE o.root=a.root AND o.rel_path=a.rel_path "
             "AND o.size=a.size AND ABS(o.mtime - a.mtime) < 0.001) AS off_site")
            if offsite else "0 AS off_site",
            "c.outcome AS shown" if display else "NULL AS shown",
            ("EXISTS (SELECT 1 FROM mugil_let_go g WHERE g.root=a.root AND g.rel_path=a.rel_path) "
             "AS gone") if _has_table(conn, "mugil_let_go") else "0 AS gone",
        ]
        if detail:
            columns += ["a.id AS id", "a.root AS root", "a.rel_path AS rel_path",
                        "a.mtime AS mtime",
                        "s.algo AS algo" if sent else "NULL AS algo",
                        "s.digest AS digest" if sent else "NULL AS digest"]
        previous = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                f"SELECT {', '.join(columns)} FROM assets a {sent} {display} "
                f"WHERE a.trashed=0 AND a.root IN ({marks}) "
                f"ORDER BY a.date_key, a.id", (since, *roots)).fetchall()
        finally:
            conn.row_factory = previous
    finally:
        for alias in attached:
            conn.execute(f"DETACH DATABASE mugil_{alias}")


def _why(row, now: float) -> str | None:
    recent, fav, sent_at = row["recent"], row["fav"], row["sent_at"]
    off_site, shown = row["off_site"], row["shown"]
    if recent:
        return "in_use"
    if fav:
        return "favourite"
    if not sent_at:
        return "not_on_secondary"
    if not off_site:
        return "not_off_site"
    if now - float(sent_at) < HOLD_DAYS * 86400:
        return "held"
    # Without a display copy the original is what is shown: it stays.
    if shown != "made":
        return "no_display_copy"
    return None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


__all__ = ["plan", "ready", "REASONS", "IN_USE_DAYS", "HOLD_DAYS"]
