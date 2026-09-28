"""Search vectors: storing them, and the matrix every AI search reads.

Split out of ``db.py``, which re-exports every public name here, so callers
still use ``db.store_embedding``, ``db.embedding_store`` and the rest.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Sequence

from . import db

def store_embedding(conn: sqlite3.Connection, asset_id: int, model: str,
                    dim: int, vector: bytes) -> None:
    with db._write_lock:
        existed = conn.execute(
            "SELECT 1 FROM embeddings WHERE asset_id=?", (asset_id,)).fetchone()
        conn.execute(
            "INSERT INTO embeddings(asset_id, model, dim, vector) VALUES(?,?,?,?) "
            "ON CONFLICT(asset_id) DO UPDATE SET model=excluded.model, "
            "dim=excluded.dim, vector=excluded.vector",
            (asset_id, model, dim, vector),
        )
        conn.commit()
    # A new vector changes the row count, which the held matrix notices and
    # appends. A replaced one changes nothing it can see, so it is queued here
    # and written over the old one on the next search.
    if existed:
        path = conn.execute("PRAGMA database_list").fetchone()[2] or ""
        with _embed_lock:
            _EMBED_REPLACED[(path, int(asset_id))] = bytes(vector)


# The whole embedding set, held in memory.
#
# Searching means multiplying one query vector against every stored vector, and
# reading them back out of SQLite for each search was most of the cost: a
# fifty-thousand-photograph library is about a hundred megabytes of vectors,
# read and then copied into one contiguous block, per keystroke-triggered
# search. It is the same hundred megabytes every time.
#
# So it is built once and kept. `key` is what makes that safe: the row count
# and the highest asset id catch anything added or removed, and `_embed_gen`
# catches a vector being replaced in place for an asset that already had one,
# which those two numbers cannot see. Both queries read the primary-key index
# and never touch a vector, so checking is cheap even when rebuilding is not.
#
# Tagging a library stores one vector at a time for hours, and a search in
# between used to throw the whole matrix away and read it back — about 2 GB at
# a million photos, for one new row. So a change is applied, not rebuilt from:
# new vectors (tagging goes in asset-id order) are written into room left at
# the end of the block, and replaced ones over their old bytes. Only something
# else — a deletion, a vector filed out of order, a different width — reads the
# table again. `block` has spare capacity; `buffer` is the filled part.
_EMBED_CACHE: dict[str, Any] = {"key": None, "ids": [], "buffer": b"", "dim": 0,
                                "index": {}, "block": bytearray(), "filled": 0}
_EMBED_REPLACED: dict[tuple[str, int], bytes] = {}
_embed_lock = threading.Lock()
_embed_gen = 0
#: Spare room a fresh matrix is built with, and how much it grows when full.
_EMBED_HEADROOM = 1.1
_EMBED_GROWTH = 1.5


def _embed_key(conn: sqlite3.Connection) -> tuple[str, int, int, int]:
    # The file comes first: the matrix is held per process, and two databases
    # can hold the same number of vectors under the same highest id.
    path = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(MAX(asset_id),0) hi FROM embeddings").fetchone()
    return (path, int(row["n"]), int(row["hi"]), _embed_gen)


def embedding_store(conn: sqlite3.Connection
                    ) -> tuple[list[int], memoryview, int, dict[int, int]]:
    """Every embedding as one block: ids, the vectors end to end, the width,
    and where each asset sits in it.

    The buffer is contiguous on purpose — numpy can view it as a matrix without
    copying a byte, which is the whole point of holding it.

    What a caller receives never changes length underneath it: an append makes
    new ``ids`` and ``index`` objects and a longer view, and the bytes an
    earlier view covers stay where they are. A replaced vector is the one
    exception — it is written in place, so a search already running may score
    that single photo against its old or new vector.
    """
    key = _embed_key(conn)
    with _embed_lock:
        cache = _EMBED_CACHE
        held = cache["key"]
        if held is not None and held[0] == key[0] and held[3] == key[3]:
            if _apply_embedding_changes(conn, key):
                return cache["ids"], cache["buffer"], cache["dim"], cache["index"]
        _rebuild_embeddings(conn, key)
        return cache["ids"], cache["buffer"], cache["dim"], cache["index"]


def _apply_embedding_changes(conn: sqlite3.Connection,
                             key: tuple[str, int, int, int]) -> bool:
    """Bring the held matrix up to *key* in place. False means rebuild instead."""
    cache = _EMBED_CACHE
    width = cache["dim"] * 4
    index = cache["index"]
    for (path, asset_id), vector in list(_EMBED_REPLACED.items()):
        if path != key[0]:
            continue            # another database's matrix; rebuilt if it is used
        position = index.get(asset_id)
        if position is None:
            continue            # not in the matrix: an append below, or dropped
        if len(vector) != width:
            return False
        start = position * width
        cache["block"][start:start + width] = vector
    _EMBED_REPLACED.clear()

    _, held_n, held_hi, _ = cache["key"]
    _, n, hi, _ = key
    if n == held_n and hi == held_hi:
        cache["key"] = key
        return True
    if n < held_n or hi <= held_hi:
        return False            # something removed, or filed below the end
    rows = conn.execute(
        "SELECT asset_id, vector FROM embeddings WHERE asset_id > ? ORDER BY asset_id",
        (held_hi,)).fetchall()
    if len(rows) != n - held_n or any(len(r["vector"]) != width for r in rows):
        return False

    filled = cache["filled"]
    needed = filled + len(rows) * width
    block = cache["block"]
    if needed > len(block):
        # Views handed out earlier keep the old block alive, so growing means
        # a new block rather than resizing this one.
        grown = bytearray(max(needed, int(len(block) * _EMBED_GROWTH)))
        grown[:filled] = block[:filled]
        block = grown
    for r in rows:
        block[filled:filled + width] = r["vector"]
        filled += width
    ids = cache["ids"] + [int(r["asset_id"]) for r in rows]
    fresh = dict(index)
    fresh.update({int(r["asset_id"]): held_n + i for i, r in enumerate(rows)})
    cache.update(key=key, block=block, filled=filled, ids=ids, index=fresh,
                 buffer=memoryview(block)[:filled].toreadonly())
    return True


def _rebuild_embeddings(conn: sqlite3.Connection, key: tuple[str, int, int, int]) -> None:
    """Read every vector into a new block with some room to grow."""
    # Let go of the stale matrix before building its replacement, and stream
    # the vectors straight into one buffer sized up front. Fetching every row
    # and then joining them held the vectors twice over, and the old matrix
    # made it three times: at a million photos and 512 dimensions, about 6 GB
    # at the peak for a 2 GB result.
    _EMBED_CACHE.update(key=None, ids=[], buffer=b"", dim=0, index={},
                        block=bytearray(), filled=0)
    _EMBED_REPLACED.clear()
    head = conn.execute(
        "SELECT COUNT(*) n, (SELECT dim FROM embeddings ORDER BY asset_id LIMIT 1) d "
        "FROM embeddings").fetchone()
    dim = int(head["d"] or 0)
    # A vector of the wrong width would misalign every row after it, so only
    # vectors as wide as the first are kept (a model changed underneath us
    # leaves a mixed table until it is re-tagged).
    width = dim * 4
    block = bytearray(int(int(head["n"]) * _EMBED_HEADROOM) * width)
    ids: list[int] = []
    filled = seen = highest = 0
    # The key is taken from the rows this one statement read, not from the
    # count above: a vector stored in between would otherwise be both in the
    # matrix and, by the key's account, still to be appended.
    for r in conn.execute("SELECT asset_id, vector FROM embeddings ORDER BY asset_id"):
        seen += 1
        highest = int(r["asset_id"])
        vector = r["vector"]
        if len(vector) != width:
            continue
        if filled + width > len(block):
            grown = bytearray(max(filled + width, int(len(block) * _EMBED_GROWTH)))
            grown[:filled] = block[:filled]
            block = grown
        block[filled:filled + width] = vector
        filled += width
        ids.append(highest)
    # Read-only, so nothing holding the shared matrix can change it for every
    # other search.
    _EMBED_CACHE.update(key=(key[0], seen, highest, key[3]), ids=ids, dim=dim, block=block,
                        filled=filled,
                        buffer=memoryview(block)[:filled].toreadonly(),
                        index={a: i for i, a in enumerate(ids)})


def forget_embeddings() -> None:
    """Drop the held matrix — for tests, and for anything that rewrites vectors
    without going through :func:`store_embedding`."""
    global _embed_gen
    with _embed_lock:
        _embed_gen += 1
        _EMBED_CACHE.update(key=None, ids=[], buffer=b"", dim=0, index={},
                            block=bytearray(), filled=0)
        _EMBED_REPLACED.clear()


def visible_embedding_ids(conn: sqlite3.Connection,
                          roots: Sequence[str] | str | None = None,
                          max_visibility: int | None = None,
                          scope: str | None = None) -> list[int]:
    """Which embedded assets this viewer may see — ids only, no vectors.

    The ranking these ids narrow produces both the results *and* the reported
    total. Filtering only by root would leave the total counting hidden and
    out-of-scope photos — a per-query count of media the caller is not allowed
    to know exists.
    """
    if not roots:
        return [int(r["asset_id"]) for r in
                conn.execute("SELECT asset_id FROM embeddings")]
    roots_sql, roots_params = db.roots_clause("a", roots)
    where = [roots_sql, "a.trashed=0"]
    params = list(roots_params)
    if max_visibility is not None:
        where.append(db.visibility_clause("a", max_visibility))
        params.append(int(max_visibility))
    scope_sql, scope_params = db.scope_clause("a", scope)
    if scope_sql:
        where.append(scope_sql)
        params += scope_params
    return [int(r["asset_id"]) for r in conn.execute(
        f"SELECT e.asset_id FROM embeddings e JOIN assets a ON a.id = e.asset_id "
        f"WHERE {' AND '.join(where)}", params)]


def load_embeddings(conn: sqlite3.Connection,
                    roots: Sequence[str] | str | None = None,
                    max_visibility: int | None = None,
                    scope: str | None = None,
                    ) -> tuple[list[int], list[bytes], int]:
    """Vectors for semantic search, narrowed to what this viewer may see.

    The ranking these ids feed produces both the results *and* the reported
    total. Filtering only by root would leave the total counting hidden and
    out-of-scope photos — a per-query count of media the caller is not allowed
    to know exists.
    """
    if roots:
        roots_sql, roots_params = db.roots_clause("a", roots)
        where = [roots_sql, "a.trashed=0"]
        params = list(roots_params)
        if max_visibility is not None:
            where.append(db.visibility_clause("a", max_visibility))
            params.append(int(max_visibility))
        scope_sql, scope_params = db.scope_clause("a", scope)
        if scope_sql:
            where.append(scope_sql)
            params += scope_params
        rows = conn.execute(
            f"SELECT e.asset_id, e.vector, e.dim FROM embeddings e "
            f"JOIN assets a ON a.id = e.asset_id WHERE {' AND '.join(where)}",
            params,
        ).fetchall()
    else:
        rows = conn.execute("SELECT asset_id, vector, dim FROM embeddings").fetchall()
    if not rows:
        return [], [], 0
    return ([r["asset_id"] for r in rows],
            [r["vector"] for r in rows],
            int(rows[0]["dim"]))
