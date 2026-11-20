"""What each person sees: the visibility gate's answer for every profile, as
an administrator can read it.

The rules are the household's and they are kept in one place, the gate
(server/visibility.py): administrators see everything; the family app hides
from everybody else what is hidden and what is explicit; screenshots and
documents are hidden (db_visibility.hide_screens), and only adult content is
explicit. What nobody could tell before was how those rules land on one
person: whether Paati, kept to one folder with a date limit, sees the
wedding; how many of the photographs a guest link-holder sees; why a cousin
cannot find the album.

For each profile this counts, with the gate the family app would build for
them (``visibility.for_person``), what they can open, and for everything else
in the library the *first* reason they cannot, in the order the gate asks:

1. outside the library folder or sub-folder they were given;
2. outside their role's date limit;
3. somebody else's "Only to me";
4. explicit;
5. hidden as a screenshot or document;
6. hidden (a folder rule, one by one, or hidden on disk), or for a guest
   anything not shared with guests;
7. recordings, which only administrators hear.

The bin is left out of every count, and a live photograph's clip and a
RAW+JPEG pair's RAW are counted once with their photograph, as the gallery
shows them. Nothing is changed; it only reads.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import auth, visibility

#: The reasons, in the order they are asked; the first that applies is the one counted.
REASONS = ("folders", "dates", "private", "explicit", "screens", "hidden", "recordings")

#: Photographs shown as a sample of what somebody sees.
SAMPLE = 12


def _dates_sql(viewer: visibility.Viewer) -> str:
    return viewer._dates("a")                                   # noqa: SLF001


def _folders_sql(viewer: visibility.Viewer) -> tuple[str, list[Any]]:
    from ..storage import db                                    # noqa: PLC0415
    roots = viewer.roots
    if not roots:
        return "0 = 1", []
    parts = [f"a.root IN ({','.join('?' * len(roots))})"]
    params: list[Any] = list(roots)
    if viewer.scope:
        scope_sql, scope_params = db.folder_clause("a.folder", viewer.scope)
        parts.append(scope_sql)
        params += scope_params
    return " AND ".join(parts), params


def _count(conn: sqlite3.Connection, viewer: visibility.Viewer, seen_sql: str,
           seen_params: list[Any], mine_sql: str, mine_params: list[Any]) -> dict[str, Any]:
    from ..storage import db                                    # noqa: PLC0415
    base = "a.trashed = 0 AND a.live_clip = 0 AND a.raw_twin = 0"
    total = int(conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {base}").fetchone()[0])
    sees = int(conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {base} AND {seen_sql}",
                            seen_params).fetchone()[0])
    own = int(conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {base} AND {mine_sql}",
                           mine_params).fetchone()[0]) if viewer.viewer_id else 0

    withheld = dict.fromkeys(REASONS, 0)
    if not viewer.is_admin:
        folders, folder_params = _folders_sql(viewer)
        dates = _dates_sql(viewer)
        ceiling = viewer.max_visibility
        kinds = tuple(viewer.admin_only_kinds)
        kind_sql = (f"a.kind IN ({','.join('?' * len(kinds))})" if kinds else "0 = 1")
        reason = (
            "CASE "
            f"WHEN NOT ({folders}) THEN 'folders' "
            f"WHEN NOT ({dates}) THEN 'dates' "
            "WHEN a.private_to IS NOT NULL AND a.private_to != ? THEN 'private' "
            "WHEN a.private_to IS NOT NULL AND a.private_to = ? THEN NULL "
            "WHEN a.nsfw = 1 THEN 'explicit' "
            "WHEN a.visibility > ? AND a.vis_source = ? THEN 'screens' "
            "WHEN a.visibility > ? THEN 'hidden' "
            f"WHEN {kind_sql} THEN 'recordings' "
            "ELSE NULL END")
        params = [*folder_params, viewer.viewer_id, viewer.viewer_id, ceiling,
                  db.SCREEN_SOURCE, ceiling, *kinds]
        for why, n in conn.execute(
                f"SELECT {reason} AS why, COUNT(*) FROM assets a WHERE {base} GROUP BY why",
                params):
            if why in withheld:
                withheld[why] = int(n)

    sample = [int(r[0]) for r in conn.execute(
        f"SELECT a.id FROM assets a WHERE {base} AND {seen_sql} AND a.thumb IS NOT NULL "
        f"ORDER BY COALESCE(a.captured_at, a.mtime) DESC LIMIT {SAMPLE}", seen_params)]


    hidden = explicit = 0
    if viewer.is_admin:
        hidden, explicit = conn.execute(
            f"SELECT SUM(CASE WHEN a.visibility >= 2 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN a.nsfw = 1 THEN 1 ELSE 0 END) FROM assets a WHERE {base}").fetchone()
    return {"total": total, "sees": sees, "own": own, "withheld": withheld, "sample": sample,
            "hidden": int(hidden or 0), "explicit": int(explicit or 0)}


def report(conn: sqlite3.Connection, viewer: visibility.Viewer,
           user: auth.User | None = None) -> dict[str, Any]:
    """One person's counts (see the module's note) and a sample of what they see."""
    from ..storage import db                                    # noqa: PLC0415
    seen_sql, seen_params = viewer.sql("a")
    mine_sql, mine_params = viewer.sql("a", mine="only")
    # Counted over the whole library (several full passes: 0.6 s a person at
    # 200,000 items), so kept until the library changes. What the answer
    # depends on besides the rows is in the key: the gate's own SQL and
    # parameters, which carry the folders, the date limit and the ceiling.
    key = ("privacy_report", seen_sql, tuple(seen_params), mine_sql, tuple(mine_params),
           viewer.viewer_id, bool(viewer.is_admin), tuple(map(str, viewer.roots)),
           viewer.scope, _dates_sql(viewer), tuple(viewer.admin_only_kinds),
           viewer.max_visibility)
    counted = db.cached_aggregate(
        conn, key, lambda: _count(conn, viewer, seen_sql, seen_params, mine_sql, mine_params),
        max_age=300.0)
    total, sees, own = counted["total"], counted["sees"], counted["own"]
    withheld, sample = counted["withheld"], counted["sample"]

    policy = viewer.policy
    out: dict[str, Any] = {
        "role": viewer.role, "admin": viewer.is_admin,
        "folders": [str(r) for r in viewer.roots], "sub_folder": viewer.scope or "",
        "dates": None if policy is None or policy[1] == "all"
        else {"cutoff": policy[0], "range": policy[1]},
        "total": total, "sees": sees, "own_private": own,
        "withheld": withheld, "withheld_total": sum(withheld.values()),
        "sample": sample,
    }
    if viewer.is_admin:
        out["admin_sees_hidden"] = counted["hidden"]
        out["admin_sees_explicit"] = counted["explicit"]
    if user is not None:
        public = user.public()
        out.update({k: public[k] for k in ("id", "name", "active", "avatar", "color",
                                           "initials", "role_label")})
    return out


def everybody(conn: sqlite3.Connection, cfg: Any) -> list[dict[str, Any]]:
    """Every profile's report, administrators first, then family, then guests."""
    order = {auth.ROLE_ADMIN: 0, auth.ROLE_FAMILY: 1, auth.ROLE_GUEST: 2}
    people = sorted(auth.list_users(conn), key=lambda u: (order.get(u.role, 3),
                                                           u.display_name.casefold()))
    return [report(conn, visibility.for_person(u, cfg, conn), u) for u in people]


def nobody_signed_in(conn: sqlite3.Connection, cfg: Any) -> dict[str, Any]:
    """What the family app shows with nobody signed in, where open browsing
    lets anybody look: a guest's view."""
    guest = auth.User(id=0, username="", display_name="", role=auth.ROLE_GUEST, active=True)
    found = report(conn, visibility.for_person(guest, cfg, conn))
    found.update(id=0, name="", active=bool(getattr(cfg, "open_browsing", False)))
    return found
