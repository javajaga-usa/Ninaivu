"""Reading the gallery: the sort orders, the clauses every query is narrowed
by (roots, scope, kind, visibility, an item kept private), ``query_assets``
itself and one item by id. Part of :mod:`.db`, which re-exports every name;
import them through ``db``, never on their own.

Three names are read through ``_db`` at call time rather than imported:
``search_shield_clause`` and ``album_date`` live in modules that import this
one's names in turn, and ``WORDS_TOTAL_SECONDS`` is a setting a test or a
caller changes on ``db``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Sequence

from . import db as _db
from .db import (  # noqa: F401
    ALBUMS_GENERATION_KEY,
    FACES_GENERATION_KEY,
    GUEST_FTS_COLUMNS,
    OCCASIONS_GENERATION_KEY,
    PLACES_GENERATION_KEY,
    TAGS_GENERATION_KEY,
    cached_aggregate,
    folder_clause,
    fts_enabled,
)

_SORTS = {
    "date_desc": "COALESCE(a.captured_at, a.mtime) DESC, a.id DESC",
    "date_asc": "COALESCE(a.captured_at, a.mtime) ASC, a.id ASC",
    "name_asc": "a.filename COLLATE NOCASE ASC, a.id ASC",
    "name_desc": "a.filename COLLATE NOCASE DESC, a.id DESC",
    "size_desc": "a.size DESC, a.id DESC",
    "size_asc": "a.size ASC, a.id ASC",
    # The id last, as every other order has: without it, photographs with the
    # same stars and the same time tied, and SQLite was free to put them in a
    # different order on each page — one repeated, another never shown.
    "rating_desc": "COALESCE(ua.rating, 0) DESC, COALESCE(a.captured_at, a.mtime) DESC, "
                   "a.id DESC",
    # Replaced in query_assets by a seeded shuffle when a seed is given (see
    # _random_order); without one each query draws a fresh order.
    "random": "RANDOM()",
}

#: A prime just under 2**31: the shuffle below is a permutation modulo it.
_SHUFFLE_PRIME = 2147483647


def _random_order(seed: int) -> str:
    """A shuffled order that stays the same from one page to the next.

    ``ORDER BY RANDOM()`` drew a new order for every query, so paging through
    it with OFFSET repeated some photographs and never showed others. This
    scrambles the id with a multiply-and-add modulo a prime, which is a
    permutation for any non-zero multiplier: the same seed gives the same order
    on every page. It is used only when the caller passes a seed: without one
    the query keeps ``RANDOM()``, so pressing shuffle again gives a new order
    rather than the same one all day. The numbers are integers worked out
    here, never user text, so they are inlined.
    """
    seed = abs(int(seed))
    multiplier = (seed * 2654435761) % (_SHUFFLE_PRIME - 1) + 1
    shift = seed % _SHUFFLE_PRIME
    return f"((a.id * {multiplier} + {shift}) % {_SHUFFLE_PRIME}), a.id"


def _like_escape(text: str) -> str:
    """*text* with LIKE's wildcards made literal, for ``LIKE ? ESCAPE '\\'``."""
    return (str(text).replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))


def _escape_fts(query: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = []
    for raw in query.replace('"', " ").split():
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "_-")
        if cleaned:
            terms.append(f'"{cleaned}"*')
    return " AND ".join(terms)


def _fts_phrase(words: str) -> str:
    """*words* as one full-text phrase, or "" when it holds nothing to match.

    The index splits text into words at anything not a letter or digit, so a
    phrase here matches the same words in the same order wherever the exact
    text is — a superset of an exact match, never less.
    """
    if not any(ch.isalnum() for ch in words):
        return ""
    return '"' + words.replace('"', '""') + '"'


def roots_clause(alias: str, roots: Sequence[str] | str) -> tuple[str, list[Any]]:
    """SQL restricting a query to one or more library folders."""
    if isinstance(roots, str):
        roots = [roots]
    roots = list(roots)
    if not roots:
        # No roots at all means no rows — never "everything".
        return "0 = 1", []
    if len(roots) == 1:
        return f"{alias}.root = ?", [roots[0]]
    return f"{alias}.root IN ({','.join('?' * len(roots))})", roots


def scope_clause(alias: str, scope: str | None) -> tuple[str, list[Any]]:
    """SQL confining results to a folder subtree (``""``/None = no limit)."""
    if not scope:
        return "", []
    return folder_clause(f"{alias}.folder", scope)


#: Kinds nobody but an administrator sees, whatever an item's own visibility
#: says.
#:
#: Audio is here because of what ends up in a family's media folders. A library
#: swept up from old drives and phone backups collects voice notes, voicemail,
#: call recordings, dictation, therapy sessions, meeting audio — things nobody
#: chose to put in a gallery and would not think to go looking for to hide. A
#: photograph is looked at on purpose; a sound file plays the moment it is
#: tapped, in a room with other people in it. So audio is administrator-only,
#: and it is a rule rather than a default: it does not depend on an item's
#: stored visibility, and there is no setting on the family app that lifts it.
#:
#: Photographs and video are unaffected — they are what the gallery is for.
ADMIN_ONLY_KINDS = ("audio",)


def kind_guard(alias: str, max_visibility: int) -> str:
    """SQL keeping administrator-only kinds, and explicit content, away from
    everybody else.

    Returns an empty string for an administrator, who sees everything. The
    kind names are inlined rather than parameterised because they are module
    constants, never user input — which keeps this a drop-in for the
    visibility clause without changing any caller's parameter list.

    Explicit content is here as well as in each listing's own ``nsfw = 0``:
    a family member sees only clean content (the household's rule,
    2026-10-26), and one fragment every family-facing query already carries
    is what stops a new one from forgetting it.
    """
    if int(max_visibility) >= 2:
        return ""
    dot = f"{alias}." if alias else ""
    guard = f"{dot}nsfw = 0"
    if ADMIN_ONLY_KINDS:
        names = ",".join("'%s'" % k for k in ADMIN_ONLY_KINDS)
        guard = f"{dot}kind NOT IN ({names}) AND {guard}"
    return guard


def visibility_clause(alias: str, max_visibility: int) -> str:
    """The whole of what a viewer may see, as one SQL fragment, one parameter.

    Every query that shows media to somebody goes through this. Two rules live
    in it — the per-item visibility ceiling, and the kinds that are
    administrator-only — and having one fragment is what stops a later query
    from remembering the first rule and forgetting the second.
    """
    dot = f"{alias}." if alias else ""
    from ..server import date_policy
    clause = f"{dot}visibility <= ? AND {date_policy.sql(alias)}"
    guard = kind_guard(alias, max_visibility)
    return f"{clause} AND {guard}" if guard else clause


def private_owner_clause(alias: str, max_visibility: int,
                         viewer_id: int | None) -> tuple[str, list[Any]]:
    """SQL for the viewer's own "Only to me" items, with its parameters.

    The date limit and the administrator-only kinds still apply, as they do
    to everything this viewer sees. With no viewer it matches nothing.
    """
    from ..server import date_policy
    if not viewer_id:
        return "0=1", []
    dot = f"{alias}." if alias else ""
    clause = f"{dot}private_to = ? AND {date_policy.sql(alias)}"
    guard = kind_guard(alias, max_visibility)
    return (f"{clause} AND {guard}" if guard else clause), [int(viewer_id)]


def hidden_from(asset: dict[str, Any] | None, max_visibility: int) -> bool:
    """Is this item one of the administrator-only kinds, or explicit, for
    this viewer? (:func:`kind_guard` for one item.)"""
    if not asset:
        return False
    if int(max_visibility) >= 2:
        return False
    return str(asset.get("kind") or "") in ADMIN_ONLY_KINDS or bool(asset.get("nsfw"))




def query_assets(
    conn: sqlite3.Connection,
    roots: Sequence[str] | str | None = None,
    *,
    gate: Any = None,
    viewer_id: int = 0,
    max_visibility: int = 1,
    scope: str | None = None,
    text: str = "",
    kinds: Sequence[str] | None = None,
    favorites: bool = False,
    min_rating: int = 0,
    date_from: str = "",
    date_to: str = "",
    folder: str = "",
    tag: str = "",
    camera: str = "",
    visibility: int | None = None,
    duplicates_only: bool = False,
    is_live: bool = False,
    min_size: int = 0,
    exclude_reviewed_large: bool = False,
    quality: str = "",
    occasion: int | None = None,
    album: int | None = None,
    near: tuple[float, float] | None = None,
    radius_km: float = 5.0,
    person: int | None = None,
    people: Sequence[int] | None = None,
    place: str = "",
    include_nsfw: bool = False,
    include_trashed: bool = False,
    ids: Sequence[int] | None = None,
    sort: str = "date_desc",
    limit: int = 200,
    offset: int = 0,
    columns: Sequence[str] | None = None,
    positional: bool = False,
    seed: int | None = None,
    with_total: bool = True,
    guest_search: bool = False,
    remember_total: bool = False,
    include_companions: bool | None = None,
    private_mine: bool = False,
    stacks: bool = False,
    search_shield: bool = False,
    near_explicit: float = 0.0,
    revealing: float = 0.0,
) -> tuple[list[dict[str, Any]], int]:
    """Filtered, paginated asset query.

    ``seed`` fixes the shuffle for ``sort="random"`` (see :func:`_random_order`).

    ``remember_total=True`` keeps the count until the library changes, where
    every filter in play reads only what the change counters watch (see
    :func:`cached_aggregate`). The gallery asks for a large library in pieces,
    and each piece counted the whole library again.

    ``with_total=False`` skips the count and returns -1 for it. The callers
    that narrow a list of ids to what the viewer may see never read the
    total, and the count ran the whole WHERE a second time for each of them.

    ``stacks=True`` lists a photo stack as its top photograph alone (see
    features/stacks/storage.py); the timeline asks for it while ``photo_stacks`` is on.

    ``search_shield=True`` is a family member's search (see
    :func:`search_shield_clause`): no screenshot or document, even one shown
    to the family by hand, and nothing the explicit check scored at
    ``near_explicit`` or over, nor any picture of people in only their
    underwear scored at ``revealing`` or over. It does nothing for an
    administrator.

    ``columns`` narrows each row to those asset columns plus ``favorite`` and
    ``rating``, returned as read with no JSON decoding. It is for callers that
    take tens of thousands of rows at once — the gallery layout — where decoding
    every row's tags and quality flags costs more than the query itself. With
    ``positional=True`` too, each row is a list in that order, then
    ``favorite`` and ``rating`` as stored: no dict is made for each of them.

    Two access controls are applied on *every* call and cannot be bypassed by
    a request parameter: ``max_visibility`` (derived from the caller's role)
    and ``scope`` (the folder the admin confined that profile to). Favourites
    and ratings come from the caller's own ``user_assets`` rows.

    ``gate`` is the caller's ``server.visibility.Viewer``: given, it is the
    whole of who may see what — its folders, visibility, dates, kinds and
    sub-folder, and its own private items when asked for by id or for the
    private view — and *roots*, ``viewer_id``, ``max_visibility`` and
    ``scope`` are taken from it.
    """
    if gate is not None:
        viewer_id, max_visibility, scope = gate.viewer_id, gate.max_visibility, None
        gate_sql, gate_params = gate.sql(
            "a", mine="only" if private_mine else "also" if ids is not None else "")
        where = [gate_sql]
        params: list[Any] = list(gate_params)
    elif private_mine:
        roots_sql, roots_params = roots_clause("a", roots if roots is not None else [])
        # The viewer's own "Only to me" view: what they took out of everybody
        # else's, and nothing else. No viewer, nothing.
        owner_sql, owner_params = private_owner_clause("a", max_visibility, viewer_id)
        where = [roots_sql, owner_sql]
        params = [*roots_params, *owner_params]
    elif ids is not None and viewer_id and int(max_visibility) < 2:
        # Asked for by id — the viewer opening, downloading or favouriting
        # something — their own private items are theirs to reach as well.
        roots_sql, roots_params = roots_clause("a", roots if roots is not None else [])
        owner_sql, owner_params = private_owner_clause("a", max_visibility, viewer_id)
        where = [roots_sql,
                 f"(({visibility_clause('a', max_visibility)}) OR ({owner_sql}))"]
        params = [*roots_params, int(max_visibility), *owner_params]
    else:
        roots_sql, roots_params = roots_clause("a", roots if roots is not None else [])
        where = [roots_sql, visibility_clause("a", max_visibility)]
        params = [*roots_params, int(max_visibility)]

    scope_sql, scope_params = scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params.extend(scope_params)

    if not include_trashed:
        where.append("a.trashed = 0")
    if not include_nsfw:
        where.append("a.nsfw = 0")
    if search_shield and int(max_visibility) < 2:
        shield_sql, shield_params = _db.search_shield_clause(conn, "a", near_explicit,
                                                          revealing)
        where.append(shield_sql)
        params.extend(shield_params)
    if visibility is not None:
        where.append("a.visibility = ?")
        params.append(int(visibility))
    if kinds:
        where.append("a.kind IN (%s)" % ",".join("?" * len(kinds)))
        params.extend(kinds)
    # The second half of a pair is not listed on its own: a live photo's clip
    # is played by holding its still, and a RAW shot with a JPEG is reached
    # from the JPEG. Listed as well, every live photo was a photograph and a
    # three-second video side by side, and every RAW+JPEG shot was there
    # twice. Asked for by id, they are still found: whoever asks for an id
    # already has it. A caller listing by ids (a search ranking) says False.
    if include_companions is False or (include_companions is None and ids is None):
        where.append("a.live_clip = 0 AND a.raw_twin = 0")
    if stacks:
        where.append("a.stacked = 0")
    # Said so that a missing row cannot match (not through COALESCE): SQLite
    # then treats the per-viewer join as an inner one and starts from this
    # viewer's few thousand favourites, rather than walking the whole library
    # and probing for each row — 252 ms to 77 for 8,600 of 208,000.
    if favorites:
        where.append("ua.favorite = 1")
    if min_rating > 0:
        where.append("ua.rating >= ?")
        params.append(min_rating)
    elif min_rating:
        where.append("COALESCE(ua.rating, 0) >= ?")
        params.append(min_rating)
    if date_from:
        where.append("a.date_key >= ?")
        params.append(date_from)
    if date_to:
        where.append("a.date_key <= ?")
        params.append(date_to)
    if folder:
        folder_sql, folder_params = folder_clause("a.folder", folder)
        where.append(folder_sql)
        params.extend(folder_params)
    if tag:
        phrase = _fts_phrase(tag)
        if phrase and fts_enabled(conn):
            # The LIKE below reads the tags of every row in the library; the
            # full-text index already knows which rows have these words in
            # their tags, so it picks those first. The LIKE stays as the exact
            # test — "sunset beach" as words also matches a photo tagged
            # "beach" and "sunset" — so the rows are the same as before.
            where.append("a.id IN (SELECT rowid FROM assets_fts "
                         "WHERE assets_fts MATCH ?)")
            params.append("{tags} : " + phrase)
        where.append("a.tags LIKE ? ESCAPE '\\'")
        params.append(f'%"{_like_escape(tag)}"%')
    if camera:
        where.append("a.camera = ?")
        params.append(camera)
    if duplicates_only:
        where.append("a.dup_group IS NOT NULL")
    if is_live:
        where.append("a.is_live = 1")
    if min_size:
        # Few items are this large: said so, the count reads the size index
        # instead of every row the viewer may see (0.6 s at 210,000 items).
        where.append("likelihood(a.size >= ?, 0.01)")
        params.append(int(min_size))
    if exclude_reviewed_large:
        # "Keep" on the largest-files screen, not a statement about anything
        # else — see the column's comment on the assets table.
        where.append("a.large_file_reviewed_at IS NULL")
    if quality:
        # Stored the way tags are, and matched the same way: the flag
        # vocabulary is fixed and short, so a LIKE on the JSON beats a second
        # table nobody would ever query on its own.
        where.append("a.quality LIKE ? ESCAPE '\\'")
        params.append(f'%"{_like_escape(quality)}"%')
    if occasion:
        where.append("a.occasion_id = ?")
        params.append(int(occasion))
    if album:
        from ..server import date_policy
        # Only under a date policy: the album's date is a MIN over all of it.
        if date_policy.restricted() and not date_policy.allows(
                {"date_key": _db.album_date(conn, album)}):
            where.append("0=1")
        # Started from the album's own rows, as a person is (below): a
        # correlated EXISTS walked the whole library in date order and looked
        # every photograph up in the album — 360 ms for an album of 2,000 in
        # a library of 200,000, every time it was opened.
        where.append("a.id IN (SELECT asset_id FROM album_items WHERE album_id = ?)")
        params.append(int(album))
    if near:
        lat, lon = float(near[0]), float(near[1])
        radius = max(0.01, float(radius_km))
        # A bounding box first, so the callback below only ever runs on
        # plausible neighbours rather than on every row in the library. One
        # degree of latitude is ~111 km everywhere; a degree of longitude
        # shrinks towards the poles, hence the cosine.
        lat_pad = radius / 111.0
        lon_pad = radius / max(1.0, 111.0 * math.cos(math.radians(lat)))
        where.append("a.gps_lat BETWEEN ? AND ?")
        params.extend([lat - lat_pad, lat + lat_pad])
        where.append("a.gps_lon BETWEEN ? AND ?")
        params.extend([lon - lon_pad, lon + lon_pad])
        where.append("ninaivu_km(a.gps_lat, a.gps_lon, ?, ?) <= ?")
        params.extend([lat, lon, radius])
    if person:
        # A subquery rather than a join: a photograph with three faces of the
        # same person must appear once, not three times, and IN says that
        # without a DISTINCT that would also collapse the count query. IN
        # rather than a correlated EXISTS: SQLite reads this person's few
        # thousand faces once from (person_id, asset_id), instead of probing
        # faces once for every photograph in the library — 10 ms rather than
        # 135 on 300,000 items.
        where.append("a.id IN (SELECT asset_id FROM faces WHERE person_id = ?)")
        params.append(int(person))
    for other in people or ():
        # Everyone named must be in it: "Maya and Arjun" is the photographs
        # with both of them, not either.
        where.append("a.id IN (SELECT asset_id FROM faces WHERE person_id = ?)")
        params.append(int(other))
    if place:
        where.append("(a.city = ? COLLATE NOCASE OR a.country = ? COLLATE NOCASE)")
        params.extend([place, place])
    if ids is not None:
        if not ids:
            return [], 0
        where.append("a.id IN (%s)" % ",".join("?" * len(ids)))
        params.extend(ids)

    # The per-viewer join goes first so its bound parameter leads the list.
    mine = ("LEFT JOIN user_assets ua ON ua.asset_id = a.id AND ua.user_id = ?")
    join_params: list[Any] = [int(viewer_id)]

    join = ""
    order = _SORTS.get(sort, _SORTS["date_desc"])
    if sort == "random" and seed is not None:
        order = _random_order(seed)

    if text:
        if fts_enabled(conn):
            match = _escape_fts(text)
            if match and guest_search:
                # A guest is told neither where a photograph was taken nor
                # with what, so the words are not matched against either.
                match = "{%s} : (%s)" % (" ".join(GUEST_FTS_COLUMNS), match)
            also = _also_said(conn, text)
            if match and also:
                # Words somebody typed beside a voice story count as the
                # item's own (features/stories/storage.py), and so does the engine's
                # description of it (storage/descriptions.py). Only when one
                # has them: the join below is what ranks the full-text
                # answer, and an OR cannot be put beside a MATCH that is joined.
                # One list of ids, joined by UNION: SQLite builds it once,
                # merged. Written as a.id IN (…) OR a.id IN (…) it looked
                # every row up once per list it was in, a quarter slower on a
                # common word.
                where.append("a.id IN (SELECT rowid FROM assets_fts WHERE assets_fts MATCH ?"
                             + "".join(f" UNION {c[0]}" for c in also) + ")")
                params.append(match)
                for clause in also:
                    params.extend(clause[1])
            elif match:
                join = "JOIN assets_fts f ON f.rowid = a.id"
                where.append("assets_fts MATCH ?")
                params.append(match)
                if sort == "date_desc":
                    order = "f.rank, " + order
        else:
            # Escaped: "100%" or "my_trip" are things people type, and
            # unescaped the % and _ in them matched anything at all.
            like = f"%{_like_escape(text.lower())}%"
            where.append(
                "(LOWER(a.filename) LIKE ? ESCAPE '\\' OR LOWER(a.tags) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(a.caption,'')) LIKE ? ESCAPE '\\' "
                "OR LOWER(a.folder) LIKE ? ESCAPE '\\' "
                + ("OR 0)" if guest_search else
                   "OR LOWER(COALESCE(a.city,'')) LIKE ? ESCAPE '\\')")
            )
            params.extend([like] * (4 if guest_search else 5))
            for clause in _also_said(conn, text):
                where[-1] = f"({where[-1]} OR a.id IN ({clause[0]}))"
                params.extend(clause[1])

    where_sql = " AND ".join(where)
    if columns is not None:
        unknown = [c for c in columns if not c.isidentifier()]
        if unknown:
            raise ValueError(f"not column names: {unknown}")
    selected = ", ".join(f"a.{c}" for c in columns) if columns is not None else "a.*"
    projection = (f"{selected}, COALESCE(ua.favorite, 0) AS mine_favorite, "
                  "COALESCE(ua.rating, 0) AS mine_rating")
    from_sql = f"FROM assets a {mine} {join}"
    all_params = [*join_params, *params]

    # The per-viewer join adds no rows (it is a LEFT JOIN on its primary key),
    # so the count only needs it when a filter reads from it. Without it the
    # count can be answered from an index alone.
    if favorites or min_rating:
        count_sql, count_params = from_sql, all_params
    else:
        count_sql, count_params = f"FROM assets a {join}", params
    count_query = f"SELECT COUNT(*) AS n {count_sql} WHERE {where_sql}"

    def count() -> int:
        return conn.execute(count_query, count_params).fetchone()["n"]

    # Not for words, favourites or ratings, quality flags or the
    # largest-files review, or a list of ids: those read what no counter
    # watches, or are one person's own. An album is kept until a photograph
    # joins or leaves one (its own counter, below).
    rememberable = remember_total and not (
        text or favorites or min_rating or quality
        or exclude_reviewed_large or ids is not None)
    # Words, otherwise like the above: what they match is also in the
    # descriptions and voice stories, which no counter follows. Kept briefly,
    # so that scrolling through what a search found does not count it all
    # again for every page; a change to the library still ends it at once.
    words = remember_total and bool(text) and not (
        favorites or min_rating or quality or exclude_reviewed_large or ids is not None)

    def total_of(returned: int) -> int:
        if not with_total:
            return -1
        # A page that came back short of its limit says the total itself:
        # what came before it and what it holds. Most searches fit in one
        # piece of the gallery, and every one of them counted every match a
        # second time for that same number. Exact, so never stale.
        if 0 < limit and returned < limit and (returned or not offset):
            return offset + returned
        if rememberable or words:
            also = ((TAGS_GENERATION_KEY,) if tag else ()) \
                + ((FACES_GENERATION_KEY,) if person or people else ()) \
                + ((OCCASIONS_GENERATION_KEY,) if occasion else ()) \
                + ((PLACES_GENERATION_KEY,) if place or near else ()) \
                + ((ALBUMS_GENERATION_KEY,) if album else ())
            return cached_aggregate(conn, ("total", count_query, tuple(count_params)),
                                    count, also=also,
                                    max_age=_db.WORDS_TOTAL_SECONDS if words else None)
        return count()

    page_sql = (f"SELECT {projection} {from_sql} WHERE {where_sql} "
                f"ORDER BY {order} LIMIT ? OFFSET ?")
    page_params = (*all_params, limit, offset)
    if columns is not None:
        values = _page_in_one_step(conn, page_sql, page_params,
                                   [*columns, "mine_favorite", "mine_rating"])
        total = total_of(len(values))
        if positional:
            return values, int(total)
        names = [*columns, "favorite", "rating"]
        lean = [dict(zip(names, row)) for row in values]
        for data in lean:
            data["favorite"] = bool(data["favorite"])
            data["rating"] = int(data["rating"] or 0)
        return lean, int(total)
    rows = conn.execute(page_sql, page_params).fetchall()
    return [row_to_dict(r) for r in rows], int(total_of(len(rows)))


def _json_value(column: str) -> str:
    """A column as JSON that reads back as it is stored: a REAL in 17 digits.

    json_array writes a REAL with 15 significant digits, which would move a
    latitude or a duration by its last bits.
    """
    return (f"CASE typeof(p.{column}) WHEN 'real' THEN json(printf('%!.17g', p.{column})) "
            f"ELSE p.{column} END")


def _page_in_one_step(conn: sqlite3.Connection, page_sql: str, page_params: Sequence[Any],
                      names: list[str]) -> list[Any]:
    """The rows of a page, read as one JSON value rather than one step a row.

    sqlite3 lets go of the GIL on every row it steps, and waits to take it
    back behind whichever thread holds it. With the scanner busy in Python
    in the same process, fetching the gallery's 25,000 rows one at a time
    took seconds (155 s against one spinning thread, measured); built as
    one value inside SQLite it is a single step, and takes as long either
    way. The order is the subquery's: SQLite feeds an aggregate the rows of
    an ordered subquery in that order.
    """
    wanted = ", ".join(_json_value(n) for n in names)
    try:
        text = conn.execute(f"SELECT json_group_array(json_array({wanted})) "
                            f"FROM ({page_sql}) p", page_params).fetchone()[0]
    except sqlite3.OperationalError:
        # A column holding bytes has no JSON form: read it row by row.
        cursor = conn.cursor()
        cursor.row_factory = None
        return cursor.execute(page_sql, page_params).fetchall()
    return json.loads(text) if text else []


def _also_said(conn: sqlite3.Connection, text: str) -> list[tuple[str, list[Any]]]:
    """What else a word search reads beside the gallery's index: voice
    stories and the engine's descriptions, each only when it has the words."""
    from . import descriptions                            # noqa: PLC0415
    found = [_story_words(conn, text), descriptions.words_clause(conn, text)]
    return [clause for clause in found if clause is not None]


def _story_words(conn: sqlite3.Connection, text: str) -> tuple[str, list[Any]] | None:
    """The voice-story half of a word search, when any story has the words.

    Asked first so that a search no story answers is built exactly as it was
    before stories existed, ranking and all. The rows it adds are still held
    to every limit of the query they join.
    """
    from ..features.stories import storage as stories                                 # noqa: PLC0415
    clause = stories.words_clause(text)
    if clause is None:
        return None
    try:
        if not conn.execute(f"SELECT EXISTS({clause[0]})", clause[1]).fetchone()[0]:
            return None
    except sqlite3.OperationalError:
        return None                                       # no stories table
    return clause


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["tags"] = json.loads(data.get("tags") or "[]")
    except (TypeError, ValueError):
        data["tags"] = []
    try:
        data["quality"] = json.loads(data.get("quality") or "[]")
    except (TypeError, ValueError):
        data["quality"] = []
    data["favorite"] = bool(data.pop("mine_favorite", 0))
    data["rating"] = int(data.pop("mine_rating", 0) or 0)
    data["nsfw"] = bool(data.get("nsfw"))
    data["trashed"] = bool(data.get("trashed"))
    data["visibility"] = int(data.get("visibility", 1))
    data["is_live"] = bool(data.get("is_live", 0))
    data["live_video_path"] = data.get("live_video_path")
    return data


def get_asset(conn: sqlite3.Connection, asset_id: int,
              viewer_id: int = 0) -> dict[str, Any] | None:
    """Raw fetch by id. Callers must still check visibility and scope —
    :func:`visible_to` does that."""
    row = conn.execute(
        "SELECT a.*, COALESCE(ua.favorite,0) AS mine_favorite, "
        "COALESCE(ua.rating,0) AS mine_rating FROM assets a "
        "LEFT JOIN user_assets ua ON ua.asset_id=a.id AND ua.user_id=? "
        "WHERE a.id=?",
        (int(viewer_id), asset_id),
    ).fetchone()
    return row_to_dict(row) if row else None


#: What a route needs to decide whether an item may be served at all (see
#: ``api._guard``) plus what the thumbnail route reads. No captions, OCR
#: text or JSON columns: the gallery asks for thumbnails hundreds at a time.
BRIEF_COLUMNS = ("id", "root", "rel_path", "folder", "kind", "visibility", "date_key",
                 "trashed", "nsfw", "thumb", "mtime", "rotation", "indexed_at",
                 "private_to")


def get_asset_brief(conn: sqlite3.Connection, asset_id: int) -> dict[str, Any] | None:
    """The guard columns of one asset, as read, with no JSON decoding."""
    row = conn.execute(
        f"SELECT {', '.join(BRIEF_COLUMNS)} FROM assets WHERE id=?", (asset_id,)).fetchone()
    return dict(row) if row else None


def visible_to(asset: dict[str, Any] | None, max_visibility: int,
               scope: str | None, viewer_id: int | None = None) -> bool:
    """Whether one asset may be shown to a viewer with these limits.

    *viewer_id* is who is asking, for the one item a ceiling alone cannot
    decide: their own "Only to me" item, which is held hidden from everybody
    but them. Left out, nobody is the owner — a link's visitor, a question's
    — so a private item is never let through by default.
    """
    if not asset:
        return False
    from ..server import date_policy
    if not date_policy.allows(asset):
        return False
    owner = asset.get("private_to")
    if viewer_id and owner is not None and int(owner) == int(viewer_id):
        ceiling = max(int(max_visibility), 2)
    else:
        ceiling = int(max_visibility)
    if int(asset.get("visibility", 1)) > ceiling:
        return False
    if hidden_from(asset, max_visibility):
        # Administrator-only by kind, or explicit. This is the check that covers every route
        # fetching one item by id — the media stream, the download, the
        # metadata — where a list query's guard never runs.
        return False
    if scope:
        folder = asset.get("folder") or ""
        if folder != scope and not folder.startswith(f"{scope}/"):
            return False
    return True
