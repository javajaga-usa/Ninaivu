"""How many copies of each photograph there are, and where.

Ninaivu keeps up to four: the library itself, the cloud backup in Google
Drive (Mugil), the second copy on another disk, and the encrypted copy away
from home. Each has its own page and its own
numbers, and none of them answers the question a household actually has: *is
there any photograph we would lose if one thing went wrong?*

This answers it, file by file, from what the three already record:

* **the library** — every file in the index that is not in the bin;
* **Google Drive** — recorded as uploaded, and not among the files a caller
  knows to be missing from Drive (``drive_missing``);
* **the other disk** — recorded as copied there, at the size and time the
  file has now;
* **the off-site copy** — recorded as sent, encrypted, to a folder or bucket
  away from home (ninaivu/cloud/offsite.py), at the size and time it has now.

A file with one copy is one disk failure from gone. So beside the counts,
each such file is given the reason it has no other: waiting to be uploaded,
kept back as hidden or by a backup rule, failed, missing from Drive, or not
yet queued. Nothing here reads a photograph or asks Google anything; it is
joins over tables Ninaivu already keeps.
"""

from __future__ import annotations

from typing import Any, Sequence

__all__ = ["summary", "single", "REASONS"]

#: Why a file has no copy beyond the library, in words.
REASONS = {
    "waiting": "waiting to be uploaded",
    "failed": "the upload failed",
    "rule": "left out by a backup rule",
    "approval": "large: waiting for an administrator's approval",
    "declined": "large: declined for the cloud by an administrator",
    "kept": "kept back (hidden, or waiting to be checked)",
    "missing": "recorded as uploaded, but missing from Google Drive",
    "unqueued": "not yet offered to the cloud backup",
}


def _prepare(conn, drive_missing: Sequence[str]) -> None:
    """The tables the joins need, present whether or not their features ran."""
    from ..cloud import store                               # noqa: PLC0415
    from ..cloud.offsite import SCHEMA as OFFSITE_SCHEMA    # noqa: PLC0415
    from .mirror import SCHEMA as MIRROR_SCHEMA             # noqa: PLC0415

    store.init_schema(conn)
    conn.executescript(MIRROR_SCHEMA)
    conn.executescript(OFFSITE_SCHEMA)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS drive_gone (remote_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM drive_gone")
    conn.executemany("INSERT OR IGNORE INTO drive_gone(remote_id) VALUES(?)",
                     [(r,) for r in drive_missing])


def _base(roots: Sequence[str]) -> tuple[str, list[Any]]:
    marks = ",".join("?" * len(roots)) or "''"
    sql = f"""
        SELECT a.id, a.filename, a.rel_path, a.root, a.size, a.kind, a.thumb,
               c.state AS cloud_state, c.error AS cloud_error,
               (c.state = 'done'
                AND c.remote_id NOT IN (SELECT remote_id FROM drive_gone)) AS in_drive,
               (c.state = 'done' AND c.remote_id IN (SELECT remote_id FROM drive_gone)) AS gone,
               (m.root IS NOT NULL) AS on_disk,
               (o.root IS NOT NULL) AS offsite
          FROM assets a
          LEFT JOIN cloud_uploads c ON c.root = a.root AND c.rel_path = a.rel_path
          LEFT JOIN mirror_copies m ON m.root = a.root AND m.rel_path = a.rel_path
               AND m.size = a.size AND ABS(m.mtime - a.mtime) < 0.001
          LEFT JOIN offsite_copies o ON o.root = a.root AND o.rel_path = a.rel_path
               AND o.size = a.size AND ABS(o.mtime - a.mtime) < 0.001
         WHERE a.trashed = 0 AND a.root IN ({marks})
    """
    return sql, list(roots)


_REASON = """
    CASE WHEN cloud_state IN ('pending', 'uploading') THEN 'waiting'
         WHEN cloud_state = 'failed' THEN 'failed'
         WHEN cloud_state = 'skipped' AND cloud_error LIKE 'kept back by a backup rule%' THEN 'rule'
         WHEN cloud_state = 'skipped' AND cloud_error LIKE 'waiting for approval%' THEN 'approval'
         WHEN cloud_state = 'skipped' AND cloud_error LIKE 'declined by an administrator%' THEN 'declined'
         WHEN cloud_state = 'skipped' THEN 'kept'
         WHEN gone THEN 'missing'
         ELSE 'unqueued' END
"""


def summary(conn, roots: Sequence[str], drive_missing: Sequence[str] = ()) -> dict[str, Any]:
    """How many files have one, two and three copies, and why the ones have one."""
    _prepare(conn, drive_missing)
    base, params = _base(roots)
    counts = {1: [0, 0], 2: [0, 0], 3: [0, 0], 4: [0, 0]}
    where = {"drive": [0, 0], "disk": [0, 0], "offsite": [0, 0]}
    for row in conn.execute(
            f"SELECT 1 + COALESCE(in_drive, 0) + on_disk + offsite AS copies, "
            f"COALESCE(in_drive, 0) AS d, on_disk AS k, offsite AS f, COUNT(*) AS n, "
            f"COALESCE(SUM(size), 0) AS b FROM ({base}) GROUP BY copies, d, k, f", params):
        slot = counts[int(row["copies"])]
        slot[0] += int(row["n"])
        slot[1] += int(row["b"])
        if row["d"]:
            where["drive"][0] += int(row["n"])
            where["drive"][1] += int(row["b"])
        if row["k"]:
            where["disk"][0] += int(row["n"])
            where["disk"][1] += int(row["b"])
        if row["f"]:
            where["offsite"][0] += int(row["n"])
            where["offsite"][1] += int(row["b"])
    reasons = {key: {"label": label, "files": 0, "bytes": 0} for key, label in REASONS.items()}
    for row in conn.execute(
            f"SELECT {_REASON} AS reason, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b "
            f"FROM ({base}) WHERE NOT COALESCE(in_drive, 0) AND NOT on_disk AND NOT offsite "
            f"GROUP BY reason",
            params):
        reasons[row["reason"]].update(files=int(row["n"]), bytes=int(row["b"]))
    total = sum(n for n, _ in counts.values())
    return {
        "files": total,
        "bytes": sum(b for _, b in counts.values()),
        "copies": {str(k): {"files": n, "bytes": b} for k, (n, b) in counts.items()},
        "in_drive": {"files": where["drive"][0], "bytes": where["drive"][1]},
        "on_disk": {"files": where["disk"][0], "bytes": where["disk"][1]},
        "offsite": {"files": where["offsite"][0], "bytes": where["offsite"][1]},
        "single_reasons": {k: v for k, v in reasons.items() if v["files"]},
    }


def single(conn, roots: Sequence[str], drive_missing: Sequence[str] = (), *,
           reason: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """The files with no copy but the library's, largest first."""
    _prepare(conn, drive_missing)
    base, params = _base(roots)
    where = "NOT COALESCE(in_drive, 0) AND NOT on_disk AND NOT offsite"
    args: list[Any] = list(params)
    if reason in REASONS:
        where += f" AND ({_REASON}) = ?"
        args.append(reason)
    rows = conn.execute(
        f"SELECT *, {_REASON} AS reason FROM ({base}) WHERE {where} "
        f"ORDER BY size DESC, id LIMIT ? OFFSET ?",
        (*args, max(1, min(int(limit), 500)), max(0, int(offset)))).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM ({base}) WHERE {where}", args).fetchone()[0]
    return {"total": int(total), "items": [{
        "id": r["id"], "name": r["filename"], "path": r["rel_path"], "size": int(r["size"] or 0),
        "kind": r["kind"], "has_thumb": bool(r["thumb"]), "reason": r["reason"],
        "reason_text": REASONS[r["reason"]]} for r in rows]}
