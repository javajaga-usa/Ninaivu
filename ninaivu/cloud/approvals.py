"""Large files wait for an administrator before they go to the cloud.

Cloud storage is paid for by the gigabyte, and a library swept up from old
drives holds a few very large files — a film, a disk image, an hour of
camcorder tape — that can cost more to keep in Drive than every photograph
together. So a file larger than ``cloud_approval_mb`` (1 GB unless the
household changes it; 0 turns this off) is not uploaded until an
administrator has said yes to it, by name, in the Cloud tab.

How it holds, so that nothing slips past:

* **Per file, just before it is sent** — the same moment the backup rules are
  asked (``CloudService._kept_back``), with the size read from the disk, not
  only the index, so a file that grew past the limit is caught too. An upload
  that was part-way through a large file when this began is held the next
  time it is picked up, because resuming goes through the same question.
* **In bulk** — :func:`apply` sets aside everything already waiting in the
  queue over the limit, so the queue and the console agree at once.

A decision is about one file at one size: approved at 1.4 GB, a file that is
later edited to 2 GB is asked about again. **Declined** files stay out and are
listed as declined; nothing already in Drive is ever touched by this.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["SCHEMA", "REASON", "DECLINED", "limit_bytes", "why", "apply", "waiting", "decide"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_approvals (
    root        TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    decision    TEXT NOT NULL,              -- approved | declined
    decided_by  INTEGER,
    decided_at  REAL NOT NULL,
    PRIMARY KEY (root, rel_path)
);
"""

#: How a row held for approval is recognised again, to release it.
REASON = "waiting for approval"
DECLINED = "declined by an administrator"


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def limit_bytes(cfg) -> int:
    """The size above which a file needs approval; 0 when nothing does."""
    return max(0, int(getattr(cfg, "cloud_approval_mb", 1024) or 0)) * 1024 * 1024


def _gb(size: int) -> str:
    return f"{size / 1024 ** 3:.1f} GB"


def why(conn: sqlite3.Connection, cfg, root: str, rel_path: str, size: int) -> str | None:
    """Why this file may not go yet, or None if it may."""
    limit = limit_bytes(cfg)
    if not limit:
        return None
    try:
        size = max(int(size or 0), (Path(root) / rel_path).stat().st_size)
    except OSError:
        size = int(size or 0)
    if size <= limit:
        return None
    row = conn.execute("SELECT size, decision FROM cloud_approvals WHERE root=? AND rel_path=?",
                       (root, rel_path)).fetchone()
    if row is not None and int(row["size"]) == size:
        if row["decision"] == "approved":
            return None
        return f"{DECLINED}: {_gb(size)}"
    return f"{REASON}: {_gb(size)}, over the {_gb(limit)} that needs an administrator's yes"


def apply(conn: sqlite3.Connection, cfg) -> dict[str, int]:
    """Set aside what is queued over the limit and not approved; release what
    no longer needs approval (the limit was raised, or it was approved)."""
    init(conn)
    limit = limit_bytes(cfg)
    released = conn.execute(
        "UPDATE cloud_uploads SET state='pending', error='', attempts=0 "
        "WHERE state='skipped' AND error LIKE ? AND (? = 0 OR size <= ? OR EXISTS ("
        "  SELECT 1 FROM cloud_approvals p WHERE p.root = cloud_uploads.root "
        "  AND p.rel_path = cloud_uploads.rel_path AND p.size = cloud_uploads.size "
        "  AND p.decision = 'approved'))", (REASON + "%", limit, limit)).rowcount
    held = 0
    if limit:
        # Declined at this size: back out as declined, not as waiting — a queue
        # refresh must not turn an administrator's no into a question again.
        held += conn.execute(
            "UPDATE cloud_uploads SET state='skipped', error=? || ': declined earlier', "
            "resume_url='' WHERE state = 'pending' AND size > ? AND EXISTS ("
            "  SELECT 1 FROM cloud_approvals p WHERE p.root = cloud_uploads.root "
            "  AND p.rel_path = cloud_uploads.rel_path AND p.size = cloud_uploads.size "
            "  AND p.decision = 'declined')", (DECLINED, limit)).rowcount
        held += conn.execute(
            "UPDATE cloud_uploads SET state='skipped', error=?, resume_url='' "
            # Pending only: a row being sent right now is the uploader's to
            # finish or stop, and the per-file question holds it next time.
            "WHERE state = 'pending' AND size > ? AND NOT EXISTS ("
            "  SELECT 1 FROM cloud_approvals p WHERE p.root = cloud_uploads.root "
            "  AND p.rel_path = cloud_uploads.rel_path AND p.size = cloud_uploads.size "
            "  AND p.decision = 'approved')",
            (f"{REASON}: over {_gb(limit)}", limit)).rowcount
    conn.commit()
    return {"held": int(held), "released": int(released)}


def waiting(conn: sqlite3.Connection, *, declined: bool = False, limit: int = 500) -> list[dict[str, Any]]:
    """Files waiting for a decision (or those declined), largest first."""
    init(conn)
    prefix = DECLINED if declined else REASON
    rows = conn.execute(
        "SELECT c.id, c.root, c.rel_path, c.filename, c.size, c.kind, c.error, c.queued_at, "
        "a.id AS asset_id, a.thumb FROM cloud_uploads c LEFT JOIN assets a "
        "ON a.root = c.root AND a.rel_path = c.rel_path "
        "WHERE c.state='skipped' AND c.error LIKE ? ORDER BY c.size DESC LIMIT ?",
        (prefix + "%", int(limit))).fetchall()
    return [dict(r) for r in rows]


def totals(conn: sqlite3.Connection) -> dict[str, int]:
    init(conn)
    out = {}
    for key, prefix in (("waiting", REASON), ("declined", DECLINED)):
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cloud_uploads "
                           "WHERE state='skipped' AND error LIKE ?", (prefix + "%",)).fetchone()
        out[key], out[f"{key}_bytes"] = int(row[0]), int(row[1])
    return out


def decide(conn: sqlite3.Connection, ids: list[int], decision: str, user_id: int) -> int:
    """Approve or decline queued files by their queue id. Returns how many."""
    if decision not in ("approved", "declined"):
        raise ValueError("decision is approved or declined")
    init(conn)
    done = 0
    now = time.time()
    for upload_id in ids:
        row = conn.execute("SELECT root, rel_path, size FROM cloud_uploads WHERE id=?",
                           (int(upload_id),)).fetchone()
        if row is None:
            continue
        try:
            size = (Path(row["root"]) / row["rel_path"]).stat().st_size
        except OSError:
            size = int(row["size"] or 0)
        conn.execute(
            "INSERT INTO cloud_approvals(root, rel_path, size, decision, decided_by, decided_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(root, rel_path) DO UPDATE SET size=excluded.size, "
            "decision=excluded.decision, decided_by=excluded.decided_by, decided_at=excluded.decided_at",
            (row["root"], row["rel_path"], size, decision, int(user_id), now))
        if decision == "approved":
            conn.execute("UPDATE cloud_uploads SET state='pending', error='', attempts=0, size=? "
                         "WHERE id=?", (size, int(upload_id)))
        else:
            conn.execute("UPDATE cloud_uploads SET state='skipped', error=?, resume_url='' WHERE id=?",
                         (f"{DECLINED}: {_gb(size)}", int(upload_id)))
        done += 1
    conn.commit()
    return done
