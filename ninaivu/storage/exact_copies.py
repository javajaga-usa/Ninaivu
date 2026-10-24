"""Exact copies, set aside before the first big copy.

The same photograph saved twice (a phone backed up into two folders, a card
copied once to "Imports" and again to "2019") is stored twice, and with two
computers it is then kept twice on the Secondary and sent twice off site.
The content fingerprints (storage/fingerprints.py) already know which files
are byte for byte the same. This chooses, in each such group, the one copy to
keep, and moves the others to the recycle bin, where they keep their names
and can be put back. Nothing is erased here.

Which copy stays:

* the one somebody has cared for: in an album, a favourite, or rated. A group
  where more than one copy has been cared for is left alone, for a person to
  decide, since moving either would lose that care;
* otherwise the one that came into the library first, and then the shortest
  path (``2019/IMG_1.jpg`` before ``Imports/old/2019/IMG_1.jpg``).

A group is also left alone when any copy is half of a live photo (its still
or its clip), so a still never leaves its clip behind as a lone video, or
the other way round.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from . import db, fingerprints


def _cared_for(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    marks = ",".join("?" * len(ids))
    found = {int(r[0]) for r in conn.execute(
        f"SELECT DISTINCT asset_id FROM album_items WHERE asset_id IN ({marks})", ids)}
    try:
        found |= {int(r[0]) for r in conn.execute(
            f"SELECT DISTINCT asset_id FROM user_assets WHERE asset_id IN ({marks}) "
            "AND (favorite=1 OR rating>0)", ids)}
    except sqlite3.OperationalError:
        pass                                            # an index with no profiles yet
    return found


def plan(conn: sqlite3.Connection, roots: Iterable[str] | None = None, *,
         limit: int = 5000) -> dict[str, Any]:
    """Each group of exact copies, with the copy kept and the ones set aside,
    or why the group is left alone. Only reads."""
    roots = set(roots) if roots is not None else None
    groups, spare_files, spare_bytes, left = [], 0, 0, 0
    for group in fingerprints.duplicate_groups(conn, limit=limit):
        files = [f for f in group["files"] if roots is None or f["root"] in roots]
        if len(files) < 2:
            continue
        ids = [int(f["id"]) for f in files]
        why = None
        if len(db.with_companions(conn, ids)) != len(ids):
            why = "live_photo"
        cared = _cared_for(conn, ids)
        if why is None and len(cared) > 1:
            why = "cared_for_twice"
        added = {int(r["id"]): float(r["added_at"] or 0) for r in conn.execute(
            f"SELECT id, added_at FROM assets WHERE id IN ({','.join('?' * len(ids))})", ids)}
        ordered = sorted(files, key=lambda f: (int(f["id"]) not in cared,
                                               added.get(int(f["id"]), 0) or float("inf"),
                                               len(f["rel_path"]), f["rel_path"], int(f["id"])))
        entry = {"fingerprint": group["fingerprint"], "size": group["size"],
                 "keep": ordered[0], "set_aside": [] if why else ordered[1:],
                 "left_alone": why}
        if why:
            left += 1
        else:
            spare_files += len(ordered) - 1
            spare_bytes += group["size"] * (len(ordered) - 1)
        groups.append(entry)
    return {"groups": groups, "spare_files": spare_files, "spare_bytes": spare_bytes,
            "left_alone": left, "fingerprinted": fingerprints.summary(conn)}


def set_aside(conn: sqlite3.Connection, *, user_id: int | None, roots: list[str],
              fingerprint: str | None = None) -> dict[str, Any]:
    """Move the spare copies of every group (or of the one *fingerprint*) to
    the recycle bin; what moved."""
    from . import recycle                                         # noqa: PLC0415
    chosen = [g for g in plan(conn, roots)["groups"]
              if not g["left_alone"] and (fingerprint is None or g["fingerprint"] == fingerprint)]
    ids = [int(f["id"]) for g in chosen for f in g["set_aside"]]
    if not ids:
        return {"deleted": 0, "failed": [], "groups": 0}
    result = recycle.recycle(conn, ids, user_id=user_id, roots=roots)
    return {"deleted": result["deleted"], "failed": result["failed"], "groups": len(chosen)}


__all__ = ["plan", "set_aside"]
