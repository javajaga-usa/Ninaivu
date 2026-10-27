"""Faces, people and the groups of faces waiting for a name. Part of
:mod:`.db`, which re-exports every name.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Sequence

from . import db as _db
from .db import (  # noqa: F401
    AI_MAY_READ,
    FACE_GROUPS_GENERATION_KEY,
    FACES_GENERATION_KEY,
    ai_may_read,
    cached_aggregate,
    log,
    roots_clause,
    scope_clause,
    visibility_clause,
)


# ---------------------------------------------------------------------------
# Faces and people
# ---------------------------------------------------------------------------
#
# One rule governs everything below: a face is never reachable except through
# its asset, and the asset is filtered by exactly the same clauses the gallery
# uses. Every helper here that a family member can reach takes ``roots``,
# ``max_visibility`` and ``scope`` and joins ``assets`` to apply them.
#
# The consequence worth stating plainly: a person's face count is the number
# of photographs *that viewer* may see them in. Two people looking at the same
# library see different numbers, and neither can learn from the difference
# that a hidden photograph exists.

#: Applied to every family-facing face query. Kept as one fragment for the
#: same reason :func:`visibility_clause` is: so a later query cannot remember
#: three of the four rules.
def _face_visibility_sql(alias: str, max_visibility: int,
                         scope: str | None,
                         roots: "Sequence[str] | str") -> tuple[str, list[Any]]:
    roots_sql, params = roots_clause(alias, roots)
    where = [roots_sql, visibility_clause(alias, max_visibility)]
    params = [*params, int(max_visibility)]
    scope_sql, scope_params = scope_clause(alias, scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)
    where.append(f"{alias}.trashed = 0")
    where.append(f"{alias}.nsfw = 0")
    return " AND ".join(where), params


#: How well a new face's box must overlap an old one to take its name, and,
#: failing that, the least overlap that still counts when the old face's
#: centre is inside the new box (a different detector draws the box tighter
#: or looser around the same face).
CARRY_IOU = 0.5
CARRY_IOU_CENTRED = 0.3


def _centre_inside(old: "Sequence[float]", new: "Sequence[float]") -> bool:
    x, y, w, h = (float(v) for v in old)
    nx, ny, nw, nh = (float(v) for v in new)
    cx, cy = x + w / 2, y + h / 2
    return nx <= cx <= nx + nw and ny <= cy <= ny + nh


def _pair_boxes(olds: "Sequence[Sequence[float]]", news: "Sequence[Sequence[float]]"
                ) -> dict[int, int]:
    """Old box index -> new box index, one to one, best overlap first.

    Taking the first overlap that would do gave one confirmed name to two new
    faces (a child's cheek against her mother's), both then confirmed, and
    both teaching that person's likeness (audit C-13)."""
    pairs = []
    for i, old in enumerate(olds):
        for j, new in enumerate(news):
            iou = _box_iou(old, new)
            if iou >= CARRY_IOU or (iou >= CARRY_IOU_CENTRED and _centre_inside(old, new)):
                pairs.append((iou, i, j))
    taken: dict[int, int] = {}
    used: set[int] = set()
    for _, i, j in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        if i not in taken and j not in used:
            taken[i] = j
            used.add(j)
    return taken


def _box_of(raw: Any) -> tuple[float, float, float, float] | None:
    try:
        box = tuple(float(v) for v in json.loads(raw))
    except (TypeError, ValueError):
        return None
    return box if len(box) == 4 else None  # type: ignore[return-value]


def replace_asset_faces(conn: sqlite3.Connection, asset_id: int,
                        faces: "Sequence[dict[str, Any]]", model: str) -> int:
    """Store the faces found in one asset, replacing whatever was there.

    What people said about the old faces is carried across by position, one
    old face to one new face, the best overlap first (:func:`_pair_boxes`):
    a confirmed name, every "this is not that person", and a person's cover
    face. A confirmed name that matches no new face is kept in
    ``faces_not_carried`` rather than dropped, and the next search tries it
    again. Re-running detection is otherwise a quiet way to throw away an
    afternoon of somebody's naming.
    """
    now = time.time()
    with _db._write_lock:
        if not ai_may_read(conn, asset_id):
            # Hidden while the face pass was working through its list.
            return 0
        old_rows = [dict(r) for r in conn.execute(
            "SELECT id, person_id, source, bbox, model FROM faces WHERE asset_id=? ORDER BY id",
            (asset_id,))]
        old_rows = [r for r in old_rows if _box_of(r["bbox"]) is not None]
        ignored = {int(r["face_id"]) for r in conn.execute(
            "SELECT face_id FROM faces_ignored WHERE face_id IN "
            "(SELECT id FROM faces WHERE asset_id=?)", (asset_id,))}
        waiting = [dict(r) for r in conn.execute(
            "SELECT id, person_id, bbox, model FROM faces_not_carried WHERE asset_id=? "
            "ORDER BY id", (asset_id,))]
        waiting = [r for r in waiting if _box_of(r["bbox"]) is not None]
        rejected: dict[int, list[tuple[int, float]]] = {}
        if old_rows:
            marks = ",".join("?" * len(old_rows))
            for r in conn.execute(f"SELECT face_id, person_id, created_at FROM face_rejections "
                                  f"WHERE face_id IN ({marks})", [r["id"] for r in old_rows]):
                rejected.setdefault(int(r["face_id"]), []).append(
                    (int(r["person_id"]), float(r["created_at"] or 0)))
        covers = {int(r["id"]): int(r["cover_face_id"]) for r in conn.execute(
            "SELECT id, cover_face_id FROM people_clusters WHERE cover_face_id IN "
            "(SELECT id FROM faces WHERE asset_id=?)", (asset_id,))}

        new_boxes = [list(face["bbox"]) for face in faces]
        # Every old face finds its new one (for its rejections and a cover);
        # confirmed names, and names still waiting, are given out in that
        # order, each to a face nobody else's name went to.
        match = _pair_boxes([_box_of(r["bbox"]) for r in old_rows], new_boxes)
        named: dict[int, int] = {}
        confirmed = [n for n, r in enumerate(old_rows) if r["source"] == "confirmed" and r["person_id"]]
        name_match = _pair_boxes([_box_of(old_rows[n]["bbox"]) for n in confirmed], new_boxes)
        left: list[dict[str, Any]] = []
        for k, n in enumerate(confirmed):
            if k in name_match:
                named[name_match[k]] = int(old_rows[n]["person_id"])
            else:
                left.append({"person_id": int(old_rows[n]["person_id"]),
                             "bbox": old_rows[n]["bbox"], "model": old_rows[n]["model"] or ""})
        free = [j for j in range(len(new_boxes)) if j not in named]
        wait_match = _pair_boxes([_box_of(r["bbox"]) for r in waiting],
                                 [new_boxes[j] for j in free])
        carried_waiting = set()
        for k, row in enumerate(waiting):
            if k in wait_match and int(row["person_id"]) not in named.values():
                named[free[wait_match[k]]] = int(row["person_id"])
                carried_waiting.add(int(row["id"]))

        from .ask_family import held_for, put_back           # noqa: PLC0415
        asked = held_for(conn, [int(r["id"]) for r in old_rows])
        conn.execute("DELETE FROM faces WHERE asset_id=?", (asset_id,))
        new_ids: list[int] = []
        for j, face in enumerate(faces):
            person_id = named.get(j)
            source, confidence = ("confirmed", 1.0) if person_id else ("none", 0.0)
            cursor = conn.execute(
                "INSERT INTO faces(asset_id, person_id, source, confidence, "
                "bbox, landmarks, det_score, sharpness, quality, embedding, "
                "thumb, model, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, person_id, source, confidence,
                 json.dumps(new_boxes[j]), json.dumps(face.get("landmarks") or []),
                 float(face.get("det_score") or 0), float(face.get("sharpness") or 0),
                 float(face.get("quality") or 0), face["embedding"],
                 face.get("thumb"), model, now),
            )
            new_ids.append(int(cursor.lastrowid))
        for n, row in enumerate(old_rows):
            if n not in match:
                continue
            new_id = new_ids[match[n]]
            if int(row["id"]) in ignored and match[n] not in named:
                conn.execute("INSERT OR IGNORE INTO faces_ignored(face_id, created_at) "
                             "VALUES (?,?)", (new_id, now))
            for person_id, at in rejected.get(int(row["id"]), ()):
                if named.get(match[n]) != person_id:
                    conn.execute("INSERT OR IGNORE INTO face_rejections(face_id, person_id, "
                                 "created_at) VALUES (?,?,?)", (new_id, person_id, at))
        by_old = {int(row["id"]): new_ids[match[n]] for n, row in enumerate(old_rows) if n in match}
        # Relatives' answers to "who is this?" still waiting (audit D-10).
        put_back(conn, asked, by_old)
        for person_id, cover in covers.items():
            conn.execute("UPDATE people_clusters SET cover_face_id=? WHERE id=?",
                         (by_old.get(cover), person_id))
        if carried_waiting:
            marks = ",".join("?" * len(carried_waiting))
            conn.execute(f"DELETE FROM faces_not_carried WHERE id IN ({marks})", sorted(carried_waiting))
        for row in left:
            already = conn.execute("SELECT 1 FROM faces_not_carried WHERE asset_id=? AND person_id=?",
                                   (asset_id, row["person_id"])).fetchone()
            if already is None:
                conn.execute("INSERT INTO faces_not_carried(asset_id, person_id, bbox, model, "
                             "created_at) VALUES (?,?,?,?,?)",
                             (asset_id, row["person_id"], row["bbox"], row["model"], now))
            log.info("photograph %s: %s's name could not be put on any face found now; "
                     "kept to try again", asset_id, row["person_id"])
        conn.execute("UPDATE assets SET face_version=? WHERE id=?",
                     (int(face_schema_version()), asset_id))
        conn.commit()
    return len(new_ids)


def faces_not_carried(conn: sqlite3.Connection, roots: "Sequence[str]",
                      limit: int = 200) -> dict[str, Any]:
    """Confirmed names a new search for faces could not place, newest first,
    in the photographs of *roots*."""
    roots = list(roots)
    if not roots:
        return {"count": 0, "items": []}
    marks = ",".join("?" * len(roots))
    where = f"FROM faces_not_carried n JOIN assets a ON a.id = n.asset_id WHERE a.root IN ({marks})"
    total = conn.execute(f"SELECT COUNT(*) {where}", roots).fetchone()[0]
    rows = conn.execute(
        "SELECT n.asset_id, n.person_id, p.name, n.bbox, n.model, n.created_at "
        f"{where.replace('WHERE', 'JOIN people_clusters p ON p.id = n.person_id WHERE')} "
        "ORDER BY n.created_at DESC, n.id DESC LIMIT ?", (*roots, int(limit))).fetchall()
    return {"count": int(total), "items": [
        {**dict(r), "bbox": json.loads(r["bbox"])} for r in rows]}


def _box_iou(a: "Sequence[float]", b: "Sequence[float]") -> float:
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    union = aw * ah + bw * bh - overlap
    return overlap / union if union > 0 else 0.0


def _turn_point(x: float, y: float, step: int, width: float, height: float
                ) -> tuple[float, float]:
    """A point of a *width* x *height* picture after a clockwise turn."""
    if step == 90:
        return height - y, x
    if step == 180:
        return width - x, height - y
    if step == 270:
        return y, width - x
    return x, y


def turn_faces(conn: sqlite3.Connection, asset_id: int, step: int,
               size: tuple[int, int]) -> None:
    """Turn a photograph's face boxes with the photograph, and have its
    faces found again the right way up.

    A box is in the pixels of the picture as shown (faces.load_image_for_faces).
    Left as they were, a turned photograph's boxes pointed at the wrong
    place for a growing-up film, and the next face pass compared boxes in two
    orientations and could not carry a confirmed name across. *size* is the
    shown picture's width and height before the turn."""
    step = int(step) % 360
    if step not in (90, 180, 270):
        return
    width, height = (float(v) for v in size)
    with _db._write_lock:
        rows = conn.execute("SELECT id, bbox, landmarks FROM faces WHERE asset_id=?",
                            (asset_id,)).fetchall()
        for row in rows:
            try:
                x, y, w, h = (float(v) for v in json.loads(row["bbox"]))
            except (TypeError, ValueError):
                continue
            corners = [_turn_point(px, py, step, width, height)
                       for px, py in ((x, y), (x + w, y + h))]
            left, top = min(c[0] for c in corners), min(c[1] for c in corners)
            box = [round(left), round(top),
                   round(abs(corners[1][0] - corners[0][0])),
                   round(abs(corners[1][1] - corners[0][1]))]
            try:
                points = [list(_turn_point(float(px), float(py), step, width, height))
                          for px, py in json.loads(row["landmarks"] or "[]")]
            except (TypeError, ValueError):
                points = []
            conn.execute("UPDATE faces SET bbox=?, landmarks=? WHERE id=?",
                         (json.dumps(box), json.dumps(points), row["id"]))
        conn.execute("UPDATE assets SET face_version=0 WHERE id=?", (asset_id,))
        conn.commit()


def face_schema_version() -> int:
    from ..media.faces import FACE_VERSION  # noqa: PLC0415 - avoids an import cycle
    return FACE_VERSION


def face_model_in_use(conn: sqlite3.Connection) -> str | None:
    """The recogniser most of the stored faces were made by, or ``None``
    with no faces. Rows carry their model; after ``face_model`` is switched
    the table holds both until every photograph has been looked at again."""
    if conn is None:
        return None
    row = conn.execute(
        "SELECT model, COUNT(*) AS n FROM faces GROUP BY model ORDER BY n DESC LIMIT 1"
    ).fetchone()
    return str(row["model"]) if row and row["model"] else None


def reset_face_pass(conn: sqlite3.Connection) -> int:
    """Have the next face pass look at every photograph again.

    What switching ``face_model`` asks for: the other recogniser's embeddings
    are not comparable, so every face is found afresh by the new pair. The
    faces already stored stay until each photograph's turn comes, so the
    People page keeps working meanwhile, and :func:`replace_asset_faces`
    carries a confirmed name across by the face's position. Returns how many
    photographs will be looked at.
    """
    with _db._write_lock:
        changed = conn.execute(
            "UPDATE assets SET face_version=0 WHERE kind='picture' AND face_version<>0"
        ).rowcount
        conn.commit()
    return int(changed or 0)


def mark_faces_scanned(conn: sqlite3.Connection, asset_id: int,
                       version: int) -> None:
    with _db._write_lock:
        conn.execute("UPDATE assets SET face_version=? WHERE id=?",
                     (int(version), asset_id))
        conn.commit()


def assets_needing_faces(conn: sqlite3.Connection, root: str, version: int,
                         limit: int = 0) -> list[dict[str, Any]]:
    """Pictures whose faces have not been found at this detector version."""
    sql = ("SELECT id, rel_path, rotation FROM assets "
           "WHERE root=? AND trashed=0 AND kind='picture' AND face_version < ? "
           f"AND {AI_MAY_READ} "
           "ORDER BY COALESCE(captured_at, mtime) DESC")
    params: list[Any] = [root, int(version)]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_assets_needing_faces(conn: sqlite3.Connection, root: str, version: int) -> int:
    """How many pictures :func:`assets_needing_faces` would list."""
    return conn.execute(
        "SELECT COUNT(*) FROM assets "
        "WHERE root=? AND trashed=0 AND kind='picture' AND face_version < ? "
        f"AND {AI_MAY_READ}",
        (root, int(version))).fetchone()[0]


def load_faces(conn: sqlite3.Connection, *, person_id: int | None = None,
               unassigned: bool = False, roots: "Sequence[str] | str | None" = None,
               min_quality: float = 0.0, confirmed_only: bool = False,
               limit: int = 0) -> list[dict[str, Any]]:
    """Faces with their embeddings, for the matcher. Console-side only."""
    where, params = ["1=1"], []
    if roots is not None:
        roots_sql, roots_params = roots_clause("a", roots)
        where.append(roots_sql)
        params.extend(roots_params)
    where.append("a.trashed = 0")
    if person_id is not None:
        where.append("f.person_id = ?")
        params.append(int(person_id))
    if unassigned:
        where.append("f.person_id IS NULL")
    if confirmed_only:
        where.append("f.source = 'confirmed'")
    if min_quality:
        where.append("f.quality >= ?")
        params.append(float(min_quality))
    # ``hidden``: somebody hid the face from naming (:func:`hide_cluster`).
    sql = ("SELECT f.id, f.asset_id, f.person_id, f.source, f.quality, "
           "f.embedding, f.cluster_key, f.thumb, f.bbox, "
           f"{_IGNORED} AS hidden "
           "FROM faces f JOIN assets a ON a.id = f.asset_id "
           f"WHERE {' AND '.join(where)} ORDER BY f.quality DESC")
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_people(conn: sqlite3.Connection, roots: "Sequence[str] | str", *,
                max_visibility: int = 1, scope: str | None = None,
                min_faces: int = 1) -> list[dict[str, Any]]:
    """Named people, counted as *this viewer* can see them.

    A person whose every appearance is in hidden photographs does not appear
    in the list at all for a family member — not as a name with a zero beside
    it, which would leak the fact that they are in the library somewhere.
    """
    guard, params = _face_visibility_sql("a", max_visibility, scope, roots)
    # A group over every face in the library, asked for on every visit to
    # People: remembered until faces, names or what this viewer may see change.
    return cached_aggregate(
        conn, ("people", guard, tuple(params), int(min_faces)),
        lambda: _list_people(conn, roots, guard, params, max_visibility, scope,
                             min_faces),
        also=(FACES_GENERATION_KEY,))


def _list_people(conn: sqlite3.Connection, roots: "Sequence[str] | str",
                 guard: str, params: list[Any], max_visibility: int,
                 scope: str | None, min_faces: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.id, p.name, p.cover_face_id, p.avatar_asset_id, "
        "       COUNT(DISTINCT f.asset_id) AS photo_count, "
        "       COUNT(f.id) AS face_count, "
        "       MAX(f.id = p.cover_face_id) AS cover_seen "
        "FROM people_clusters p "
        "JOIN faces f ON f.person_id = p.id "
        "JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} "
        "GROUP BY p.id, p.name, p.cover_face_id, p.avatar_asset_id "
        "HAVING COUNT(f.id) >= ? "
        "ORDER BY photo_count DESC, p.name COLLATE NOCASE",
        (*params, int(min_faces)),
    ).fetchall()
    people = []
    for row in rows:
        person = dict(row)
        # The stored cover was chosen from every face, but the crop is served
        # only to somebody who may open its photograph. A family member was
        # shown a broken cover for anyone whose best face is in a photograph
        # from before their date cutoff, or one marked NSFW. Their best face
        # among the photographs they can see instead.
        if not person.pop("cover_seen"):
            best = person_face_ids(conn, person["id"], roots,
                                   max_visibility=max_visibility, scope=scope,
                                   limit=1)
            person["cover_face_id"] = best[0]["id"] if best else None
        if person.get("avatar_asset_id") is not None:
            # The chosen avatar photograph, named only to someone who may open
            # it: the id alone says a hidden photograph exists.
            seen = conn.execute(
                f"SELECT 1 FROM assets a WHERE a.id = ? AND {guard}",
                (person["avatar_asset_id"], *params)).fetchone()
            if seen is None:
                person["avatar_asset_id"] = None
        people.append(person)
    return people


def person_face_ids(conn: sqlite3.Connection, person_id: int,
                    roots: "Sequence[str] | str", *, max_visibility: int = 1,
                    scope: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """This person's faces, filtered to what the viewer may see."""
    guard, params = _face_visibility_sql("a", max_visibility, scope, roots)
    rows = conn.execute(
        "SELECT f.id, f.asset_id, f.bbox, f.quality, f.source, f.confidence, f.thumb "
        "FROM faces f JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} AND f.person_id = ? "
        "ORDER BY f.quality DESC LIMIT ?",
        (*params, int(person_id), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def faces_for_asset(conn: sqlite3.Connection, asset_id: int) -> list[dict[str, Any]]:
    """Every face in one asset. Callers must already have cleared the asset."""
    rows = conn.execute(
        "SELECT f.id, f.person_id, f.bbox, f.quality, f.source, f.confidence, "
        "       p.name AS person_name "
        "FROM faces f LEFT JOIN people_clusters p ON p.id = f.person_id "
        "WHERE f.asset_id = ? ORDER BY f.quality DESC", (asset_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_face(conn: sqlite3.Connection, face_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT f.*, a.root, a.folder, a.visibility, a.trashed, a.nsfw, a.kind, a.date_key "
        "FROM faces f JOIN assets a ON a.id = f.asset_id WHERE f.id = ?",
        (int(face_id),)
    ).fetchone()
    return dict(row) if row else None


#: Bumped whenever this process names, unnames, regroups or merges faces, so
#: a count of unnamed groups kept for a minute is taken again at once.
_FACE_EDITS = 0


def face_edits() -> int:
    """How many times this process has changed who a face belongs to."""
    return _FACE_EDITS


def _faces_changed() -> None:
    global _FACE_EDITS
    _FACE_EDITS += 1


def set_face_person(conn: sqlite3.Connection, face_id: int,
                    person_id: int | None, source: str = "confirmed",
                    confidence: float = 1.0) -> None:
    with _db._write_lock:
        conn.execute(
            "UPDATE faces SET person_id=?, source=?, confidence=? WHERE id=?",
            (person_id, source, float(confidence), int(face_id)))
        if person_id and source == "confirmed":
            # A person said who it is, so it is somebody after all.
            conn.execute("DELETE FROM faces_ignored WHERE face_id=?", (int(face_id),))
        conn.commit()
        _faces_changed()


def set_faces_person(conn: sqlite3.Connection,
                     rows: "Sequence[tuple[int, int | None, str, float]]") -> None:
    """:func:`set_face_person` for many faces, as ``(face_id, person_id,
    source, confidence)``, in one commit.

    Naming a group or a regroup assigns hundreds of faces at a time. One commit
    each was a disk sync each, with the write lock taken and given back
    between them, which kept every other writer waiting behind the naming.

    Only a ``confirmed`` row — a person saying so — is written whatever the
    face holds. Anything else is the matcher's guess, worked out from a read
    taken before the write, and in between somebody may have confirmed the face
    as someone else or said "not this person". So a guess only lands on a face
    that is still unassigned and unconfirmed, and never on a person it was
    rejected for; the rejection is checked in the same statement, not from a
    list read earlier.
    """
    if not rows:
        return
    human = [(person_id, source, float(confidence), int(face_id))
             for face_id, person_id, source, confidence in rows
             if source == "confirmed"]
    guessed = [(person_id, source, float(confidence), int(face_id), person_id)
               for face_id, person_id, source, confidence in rows
               if source != "confirmed"]
    with _db._write_lock:
        if human:
            conn.executemany(
                "UPDATE faces SET person_id=?, source=?, confidence=? WHERE id=?",
                human)
            # Named by a person, so not a face nobody is bothered about.
            conn.executemany("DELETE FROM faces_ignored WHERE face_id=?",
                             [(row[3],) for row in human if row[0]])
        if guessed:
            # Nor on a face somebody has just hidden from naming.
            conn.executemany(
                "UPDATE faces SET person_id=?, source=?, confidence=? "
                "WHERE id=? AND person_id IS NULL AND source <> 'confirmed' "
                "AND NOT EXISTS (SELECT 1 FROM face_rejections r "
                "WHERE r.face_id = faces.id AND r.person_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM faces_ignored i WHERE i.face_id = faces.id)",
                guessed)
        conn.commit()
        _faces_changed()


def reject_face_for_person(conn: sqlite3.Connection, face_id: int,
                           person_id: int) -> None:
    """Record 'not this person', permanently, and detach if it was attached."""
    with _db._write_lock:
        conn.execute(
            "INSERT OR IGNORE INTO face_rejections(face_id, person_id, created_at) "
            "VALUES (?,?,?)", (int(face_id), int(person_id), time.time()))
        conn.execute(
            "UPDATE faces SET person_id=NULL, source='none', confidence=0 "
            "WHERE id=? AND person_id=?", (int(face_id), int(person_id)))
        conn.commit()
        _faces_changed()


def rejections(conn: sqlite3.Connection,
               person_id: int | None = None) -> set[tuple[int, int]]:
    sql = "SELECT face_id, person_id FROM face_rejections"
    params: tuple = ()
    if person_id is not None:
        sql += " WHERE person_id = ?"
        params = (int(person_id),)
    return {(int(r["face_id"]), int(r["person_id"]))
            for r in conn.execute(sql, params).fetchall()}


def save_person_centroid(conn: sqlite3.Connection, person_id: int,
                         centroid: bytes | None, confirmed: int,
                         total: int, cover_face_id: int | None = None) -> None:
    with _db._write_lock:
        conn.execute(
            "UPDATE people_clusters SET centroid=?, confirmed_count=?, "
            "face_count=?, cover_face_id=COALESCE(?, cover_face_id), updated_at=? "
            "WHERE id=?",
            (centroid, int(confirmed), int(total), cover_face_id,
             time.time(), int(person_id)))
        conn.commit()


def save_cluster_keys(conn: sqlite3.Connection,
                      pairs: "Sequence[tuple[int, str | None]]") -> None:
    """Write the unnamed-cluster label back onto each face."""
    if not pairs:
        return
    with _db._write_lock:
        conn.executemany("UPDATE faces SET cluster_key=? WHERE id=?",
                         [(key, face_id) for face_id, key in pairs])
        conn.commit()
        _faces_changed()


#: An unnamed face somebody hid from naming (:func:`hide_cluster`).
_IGNORED = "EXISTS (SELECT 1 FROM faces_ignored i WHERE i.face_id = f.id)"


def list_unnamed_clusters(conn: sqlite3.Connection, roots: "Sequence[str] | str",
                          *, max_visibility: int = 2, scope: str | None = None,
                          min_size: int = 3, limit: int = 60, offset: int = 0,
                          hidden: bool = False) -> list[dict[str, Any]]:
    """Groups the matcher formed that nobody has named yet, largest first.

    The faces somebody hid from naming are left out of the count and the
    cover, and a group with nothing else in it is left out altogether.
    *hidden* lists those instead, for showing them again; *offset* pages
    through either, since a large library has far more than one page of
    groups and the person you are looking for is often not in the first."""
    guard, params = _face_visibility_sql("a", max_visibility, scope, roots)
    # A group over every unnamed face (about 0.6 s on a family library),
    # asked for on every visit to Faces and every page of it: kept until a
    # face, a name, a group or what is hidden from naming moves.
    key = ("unnamed clusters", guard, tuple(params), int(min_size), int(limit),
           max(0, int(offset)), bool(hidden))
    return cached_aggregate(
        conn, key, lambda: _unnamed_clusters(conn, guard, params, min_size, limit,
                                             offset, hidden),
        also=(FACES_GENERATION_KEY, FACE_GROUPS_GENERATION_KEY))


def _unnamed_clusters(conn: sqlite3.Connection, guard: str, params: list,
                      min_size: int, limit: int, offset: int,
                      hidden: bool) -> list[dict[str, Any]]:
    which = _IGNORED if hidden else f"NOT {_IGNORED}"
    rows = conn.execute(
        "SELECT f.cluster_key, COUNT(*) AS size, "
        "       COUNT(DISTINCT f.asset_id) AS photo_count, "
        "       MAX(f.quality) AS best_quality "
        "FROM faces f JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} AND f.person_id IS NULL AND f.cluster_key IS NOT NULL "
        f"AND {which} "
        "GROUP BY f.cluster_key HAVING COUNT(*) >= ? "
        "ORDER BY size DESC, f.cluster_key LIMIT ? OFFSET ?",
        (*params, int(min_size), int(limit), max(0, int(offset))),
    ).fetchall()
    out = [dict(row) for row in rows]
    # The best face of each group in one statement rather than one per group:
    # SQLite hands back the other columns from the row that holds MAX(), which
    # is the documented way to ask for "the row with the highest quality".
    covers: dict[str, dict[str, Any]] = {}
    keys = [item["cluster_key"] for item in out]
    for start in range(0, len(keys), 500):
        piece = keys[start:start + 500]
        for cover in conn.execute(
                "SELECT cluster_key, id, asset_id, thumb, MAX(quality) AS quality "
                f"FROM faces f WHERE person_id IS NULL AND {which} "
                "AND cluster_key IN (%s) "
                "GROUP BY cluster_key" % ",".join("?" * len(piece)), piece):
            covers[cover["cluster_key"]] = {
                "id": cover["id"], "asset_id": cover["asset_id"], "thumb": cover["thumb"]}
    for item in out:
        item["cover"] = covers.get(item["cluster_key"])
    return out


def count_unnamed_clusters(conn: sqlite3.Connection, roots: "Sequence[str] | str",
                           *, max_visibility: int = 2, scope: str | None = None,
                           min_size: int = 3, limit: int = 60, min_photos: int = 0) -> int:
    """How many groups :func:`list_unnamed_clusters` would list, without
    building them: the console's overview only wants the number.

    *min_photos* counts only the groups seen in at least that many
    photographs: somebody who keeps turning up, rather than a stranger in
    three pictures of one afternoon."""
    guard, params = _face_visibility_sql("a", max_visibility, scope, roots)
    return int(conn.execute(
        "SELECT COUNT(*) FROM (SELECT f.cluster_key "
        "FROM faces f JOIN assets a ON a.id = f.asset_id "
        f"WHERE {guard} AND f.person_id IS NULL AND f.cluster_key IS NOT NULL "
        f"AND NOT {_IGNORED} "
        "GROUP BY f.cluster_key HAVING COUNT(*) >= ? AND COUNT(DISTINCT f.asset_id) >= ? LIMIT ?)",
        (*params, int(min_size), int(min_photos), int(limit))).fetchone()[0])


def hide_cluster(conn: sqlite3.Connection, cluster_key: str) -> int:
    """"Not bothered": hide every unnamed face of one group from naming.

    Nothing is deleted and no photograph changes. The faces stay in the
    index, out of the groups waiting for a name and out of every guess, and
    the next regroup hides the new faces that land in a group made mostly of
    hidden ones (:func:`ninaivu.media.faceindex.plan_regroup`), so the same
    stranger does not come back after the next scan. Returns how many faces
    were hidden; 0 means the group is gone (a regroup reissued its key)."""
    with _db._write_lock:
        cur = conn.execute(
            "INSERT OR IGNORE INTO faces_ignored(face_id, created_at) "
            "SELECT id, ? FROM faces WHERE cluster_key=? AND person_id IS NULL",
            (time.time(), str(cluster_key)))
        conn.commit()
        _faces_changed()
    return int(cur.rowcount or 0)


def hide_faces(conn: sqlite3.Connection, face_ids: "Sequence[int]") -> None:
    """Hide these faces from naming too (what a regroup carries over)."""
    if not face_ids:
        return
    now = time.time()
    with _db._write_lock:
        conn.executemany(
            "INSERT OR IGNORE INTO faces_ignored(face_id, created_at) "
            "SELECT id, ? FROM faces WHERE id=? AND person_id IS NULL",
            [(now, int(face_id)) for face_id in face_ids])
        conn.commit()
        _faces_changed()


def show_cluster(conn: sqlite3.Connection, cluster_key: str | None = None) -> int:
    """Undo :func:`hide_cluster` for one group, or every hidden face when
    *cluster_key* is None. Returns how many faces came back."""
    with _db._write_lock:
        if cluster_key is None:
            cur = conn.execute("DELETE FROM faces_ignored")
        else:
            cur = conn.execute(
                "DELETE FROM faces_ignored WHERE face_id IN "
                "(SELECT id FROM faces WHERE cluster_key=?)", (str(cluster_key),))
        conn.commit()
        _faces_changed()
    return int(cur.rowcount or 0)


def hidden_face_ids(conn: sqlite3.Connection) -> set[int]:
    """Every face hidden from naming, for the matcher to pass over."""
    return {int(r[0]) for r in conn.execute("SELECT face_id FROM faces_ignored")}


def count_hidden_faces(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM faces_ignored i JOIN faces f ON f.id = i.face_id "
        "WHERE f.person_id IS NULL").fetchone()[0])


def cluster_face_ids(conn: sqlite3.Connection, cluster_key: str,
                     limit: int = 500) -> list[int]:
    return [int(r["id"]) for r in conn.execute(
        "SELECT id FROM faces WHERE cluster_key=? AND person_id IS NULL "
        "ORDER BY quality DESC LIMIT ?", (cluster_key, int(limit)))]


def merge_people(conn: sqlite3.Connection, source_id: int, target_id: int) -> int:
    """Fold one person into another, keeping every confirmation."""
    if int(source_id) == int(target_id):
        return 0
    with _db._write_lock:
        cur = conn.execute("UPDATE faces SET person_id=? WHERE person_id=?",
                           (int(target_id), int(source_id)))
        conn.execute(
            "INSERT OR IGNORE INTO face_rejections(face_id, person_id, created_at) "
            "SELECT face_id, ?, created_at FROM face_rejections WHERE person_id=?",
            (int(target_id), int(source_id)))
        # Names still waiting to be placed again go with them too.
        conn.execute("UPDATE faces_not_carried SET person_id=? WHERE person_id=?",
                     (int(target_id), int(source_id)))
        # The family tree goes with the faces: deleting the row would take the
        # source's parents, children and spouse with it (ON DELETE CASCADE).
        from .family_tree import carry_over                  # noqa: PLC0415
        carry_over(conn, int(source_id), int(target_id))
        # So does the year they were born (storage/then_and_now.py).
        from .then_and_now import carry_over as carry_born    # noqa: PLC0415
        carry_born(conn, int(source_id), int(target_id))
        conn.execute("DELETE FROM people_clusters WHERE id=?", (int(source_id),))
        conn.commit()
        _faces_changed()
    return cur.rowcount or 0


def mark_large_files_reviewed(conn: sqlite3.Connection, ids: Sequence[int]) -> int:
    """"Keep" on the largest-files screen: stop asking about these.

    Nothing about the file changes -- it is not touched, moved, or scored.
    The only effect is that :func:`query_assets` with
    ``exclude_reviewed_large=True`` stops returning it. There is no
    "un-keep" because there is nothing to undo: the file was never anything
    but itself, and the ordinary way to find it again is the ordinary way to
    find any file.
    """
    clean = [int(i) for i in ids if i]
    if not clean:
        return 0
    with _db._write_lock:
        cur = conn.execute(
            "UPDATE assets SET large_file_reviewed_at=? "
            f"WHERE id IN ({','.join('?' * len(clean))})",
            (time.time(), *clean))
        conn.commit()
    return cur.rowcount or 0


def face_stats(conn: sqlite3.Connection, root: str) -> dict[str, Any]:
    """The face status page's figures. The two that join every face to its
    photograph (a third of a second each at 150,000 faces) are remembered until
    the faces or the library change; how many photographs are still waiting
    for the face pass is read fresh, since the pass moves it on every row it
    finishes without touching either counter."""
    counted = cached_aggregate(conn, ("face_stats", root), lambda: _face_counts(conn, root),
                               also=(FACES_GENERATION_KEY,))
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM assets "
        "WHERE root=? AND trashed=0 AND kind='picture' AND face_version < ? "
        f"AND {AI_MAY_READ}",
        (root, int(face_schema_version()))).fetchone()
    return {**counted, "photos_pending": int(pending["n"] or 0)}


def _face_counts(conn: sqlite3.Connection, root: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS faces, "
        "       SUM(CASE WHEN f.person_id IS NOT NULL THEN 1 ELSE 0 END) AS assigned, "
        "       SUM(CASE WHEN f.source='confirmed' THEN 1 ELSE 0 END) AS confirmed, "
        "       COUNT(DISTINCT f.asset_id) AS photos "
        "FROM faces f JOIN assets a ON a.id=f.asset_id WHERE a.root=? AND a.trashed=0",
        (root,)).fetchone()
    # People with at least one face in this library, the same people the
    # People list can show. A bare count of names also counted names left with
    # no faces behind them, so the number and the list disagreed.
    people = conn.execute(
        "SELECT COUNT(DISTINCT f.person_id) AS n "
        "FROM faces f JOIN assets a ON a.id=f.asset_id "
        "WHERE f.person_id IS NOT NULL AND a.root=? AND a.trashed=0 AND a.nsfw=0",
        (root,)).fetchone()
    return {
        "faces": int(row["faces"] or 0),
        "assigned": int(row["assigned"] or 0),
        "confirmed": int(row["confirmed"] or 0),
        "photos_with_faces": int(row["photos"] or 0),
        "people": int(people["n"] or 0),
    }


# ---------------------------------------------------------------------------
# People Clusters
# ---------------------------------------------------------------------------

def list_people_clusters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM people_clusters ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def create_or_update_person_cluster(
    conn: sqlite3.Connection,
    name: str,
    avatar_asset_id: int | None = None,
) -> dict[str, Any]:
    now = time.time()
    with _db._write_lock:
        row = conn.execute("SELECT id FROM people_clusters WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if row:
            cid = int(row["id"])
            if avatar_asset_id is not None:
                conn.execute("UPDATE people_clusters SET avatar_asset_id=? WHERE id=?", (avatar_asset_id, cid))
        else:
            cur = conn.execute(
                "INSERT INTO people_clusters(name, avatar_asset_id, created_at) VALUES (?, ?, ?)",
                (name, avatar_asset_id, now),
            )
            cid = int(cur.lastrowid)
        conn.commit()
    r = conn.execute("SELECT * FROM people_clusters WHERE id=?", (cid,)).fetchone()
    return _person_cluster_dict(r)


def create_person_cluster(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """A new person with this name, even when somebody already has it."""
    with _db._write_lock:
        cur = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES (?, ?)",
                           (name, time.time()))
        conn.commit()
    r = conn.execute("SELECT * FROM people_clusters WHERE id=?", (int(cur.lastrowid),)).fetchone()
    return _person_cluster_dict(r)


def delete_person_cluster(conn: sqlite3.Connection, cluster_id: int) -> None:
    with _db._write_lock:
        conn.execute("DELETE FROM people_clusters WHERE id=?", (cluster_id,))
        conn.commit()


def _person_cluster_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    """A ``people_clusters`` row, safe to hand straight to ``jsonify``.

    ``centroid`` is a packed float32 BLOB (see :func:`save_person_centroid`)
    that nothing outside this module ever reads back — matching always
    rebuilds a person's centroid live from their confirmed faces (see
    :meth:`FaceIndexer._load_people`). Left in, it is a raw ``bytes`` object
    Flask's JSON encoder cannot serialise, so any route that returns one of
    these rows straight from ``SELECT *`` — naming a cluster with a name that
    already belongs to somebody with confirmed faces, or renaming into one —
    500s the moment that person actually has a centroid, which is to say the
    moment they matter.
    """
    if row is None:
        return {}
    person = dict(row)
    person.pop("centroid", None)
    return person


def get_person_cluster(conn: sqlite3.Connection, person_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM people_clusters WHERE id=?", (int(person_id),)
    ).fetchone()
    return _person_cluster_dict(row) if row else None


def rename_person_cluster(conn: sqlite3.Connection, person_id: int,
                          new_name: str) -> dict[str, Any]:
    """Fix a name — or, if it is already somebody else's, fold this person
    into them instead.

    A rename and a merge are the same admission looked at from two sides:
    "I spelled that wrong" and "these are the same person" both mean the
    library should end up with one row, named once. Typing a name that
    already belongs to somebody else is how an admin says the second thing,
    so it is honoured as a merge rather than left to produce two people who
    happen to share a name. The match is case-insensitive for the same
    reason naming an unnamed cluster already is (see
    :func:`create_or_update_person_cluster`): "Priya" and "priya" are a typo
    of case, not two different people.
    """
    new_name = new_name.strip()
    target_id: int | None = None
    with _db._write_lock:
        existing = conn.execute(
            "SELECT id FROM people_clusters WHERE name=? COLLATE NOCASE AND id != ?",
            (new_name, int(person_id)),
        ).fetchone()
        if existing:
            target_id = int(existing["id"])
        else:
            conn.execute(
                "UPDATE people_clusters SET name=?, updated_at=? WHERE id=?",
                (new_name, time.time(), int(person_id)),
            )
        conn.commit()

    if target_id is not None:
        # merge_people takes the lock itself, so it is called after the one
        # above is released rather than nested inside it.
        merge_people(conn, int(person_id), target_id)
        return {"person": get_person_cluster(conn, target_id), "merged": True,
                "person_id": target_id}

    return {"person": get_person_cluster(conn, int(person_id)), "merged": False,
            "person_id": int(person_id)}
