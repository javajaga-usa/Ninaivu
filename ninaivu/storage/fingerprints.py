"""Content fingerprints: what a file *is*, beside where it lives.

A photograph in the index has always been known by its place on disk, the
``(root, rel_path)`` of its row. Move it to another day's folder in Finder,
rename the folder it is in, and the next scan saw one file gone and one file
new: the row that carried its faces, its story, its albums and its edits was
deleted, and a bare row put in its place.

With ``content_fingerprints`` on (Settings → Advanced, 2.0 proposal B, the
first stage), every file in the library also gets a fingerprint of its bytes:
BLAKE3, 256 bits, kept in ``content_fingerprints`` beside the size and
modified time it was taken at. The library stays exactly as it is on disk,
year/month/day folders and all; this is an index over the files, never a new
place to keep them. With it:

* a scan that finds a file gone and a file new with the same fingerprint
  knows it is one file that moved (:func:`match_moves`), and the row keeps
  everything people did with it, the way a name respelled by another
  computer already did (``db.respell_asset``);
* exact copies are known across every library folder and drive, by their
  bytes rather than by how they look (:func:`duplicate_groups`).

Filling them in reads every byte once, the cost of a storage check, as the
last pass of a scan (``Scanner._fingerprint``); it carries on where it was
after a stop or a restart, since a fingerprint is only owed while it is
missing or the file has changed since it was taken.

The hashing itself is :func:`fingerprint_file`: a plain function of a path,
with nothing of the server's in it, so the job engine's worker processes
(1.13) can run it as one more kind of task.

Without the ``blake3`` package the fingerprints are BLAKE2b, written with
their own name: the two are never compared with each other, and the ones made
the slower way are made again once BLAKE3 is there.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import db

log = logging.getLogger(__name__)

try:                                                     # pragma: no cover - which one is installed
    import blake3 as _blake3
except ImportError:                                      # pragma: no cover
    _blake3 = None

#: The name written with every fingerprint made here.
ALGO = "blake3" if _blake3 is not None else "blake2b"

#: Bytes read at a time. Large enough that a video is a few hundred reads, small
#: enough that a stop is noticed within one of them.
CHUNK = 4 * 1024 * 1024

#: Files at least this big are hashed on several threads (BLAKE3 only): a
#: two-gigabyte video is otherwise one core's work while the drive waits.
THREADED_FROM = 64 * 1024 * 1024

#: Fingerprints written to the index in one go.
GROUP = 200

#: What a fingerprint owes nothing for, as SQL on ``assets``: one in this
#: algorithm, taken at the file's present size and modified time. The ``?`` is
#: :data:`ALGO`. Shared with ``scanner.owed`` so start-up asks the same question.
UP_TO_DATE = ("EXISTS (SELECT 1 FROM content_fingerprints f WHERE f.asset_id=assets.id "
              "AND f.algo=? AND f.size=assets.size AND f.mtime=assets.mtime)")


class Unchanged(Exception):
    """Raised by :func:`fingerprint_file` for a file that changed while it was
    read: whatever came out describes no version of it."""


def enabled(cfg: Any) -> bool:
    """Whether this server keeps fingerprints."""
    return bool(getattr(cfg, "content_fingerprints", False))


def fingerprint_file(path: str | os.PathLike[str],
                     stop: Callable[[], bool] | None = None) -> tuple[str, int, float] | None:
    """``(hex digest, size, mtime)`` of the file at *path*, as it was read.

    ``None`` if *stop* said to stop part-way. Raises :class:`FileNotFoundError`
    when it is not there, :class:`Unchanged` when it changed during the read
    (a copy still arriving), and any other :class:`OSError` the drive gives.
    """
    before = os.stat(path)
    if _blake3 is not None:
        threads = _blake3.blake3.AUTO if before.st_size >= THREADED_FROM else 1
        digest: Any = _blake3.blake3(max_threads=threads)
    else:
        digest = hashlib.blake2b(digest_size=32)
    view = memoryview(bytearray(CHUNK))
    with open(path, "rb", buffering=0) as handle:
        while True:
            if stop is not None and stop():
                return None
            got = handle.readinto(view)
            if not got:
                break
            digest.update(view[:got])
    after = os.stat(path)
    if (after.st_size, after.st_mtime) != (before.st_size, before.st_mtime):
        raise Unchanged(str(path))
    return digest.hexdigest(), int(before.st_size), float(before.st_mtime)


# -- the index ---------------------------------------------------------------

def owed_rows(conn: sqlite3.Connection, root: str) -> list[sqlite3.Row]:
    """The files of *root* whose fingerprint is missing or out of date, oldest
    row first."""
    return conn.execute(
        "SELECT id, rel_path, size, mtime FROM assets WHERE root=? AND trashed=0 "
        f"AND NOT {UP_TO_DATE} ORDER BY id", (str(root), ALGO)).fetchall()


def store(conn: sqlite3.Connection, found: Sequence[tuple[int, str, int, float]]) -> None:
    """Write ``(asset id, digest, size, mtime)`` fingerprints, in one go."""
    if not found:
        return
    now = time.time()
    with db._write_lock:                                  # noqa: SLF001
        # Only for rows still there: a file removed by a scan running beside
        # this has no row to hang a fingerprint on.
        conn.executemany(
            "INSERT INTO content_fingerprints(asset_id, algo, digest, size, mtime, taken_at) "
            "SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM assets WHERE id=?) "
            "ON CONFLICT(asset_id) DO UPDATE SET algo=excluded.algo, "
            "digest=excluded.digest, size=excluded.size, mtime=excluded.mtime, "
            "taken_at=excluded.taken_at",
            [(item, ALGO, digest, size, mtime, now, item)
             for item, digest, size, mtime in found])
        conn.commit()


def fill(conn: sqlite3.Connection, root: str, *,
         stop: Callable[[], bool] = lambda: False,
         threads: int = 2,
         carry_on: Callable[[], bool] = lambda: True,
         progress: Callable[[int, int], None] | None = None) -> dict[str, int]:
    """Fingerprint every file of *root* that owes one. Returns counts.

    Files are read on *threads* threads (the drive is the limit, not the
    processor: two keep an external SSD busy, more only queue at it). Written
    every :data:`GROUP` files, so a stop or a crash loses at most that many,
    and a file whose size or time no longer matches its row is left for the
    scan that will index it again. *carry_on* is asked before each group: the
    scan's turn-taking with the household, which may wait, and False to stop.
    """
    rows = owed_rows(conn, root)
    done = {"owed": len(rows), "taken": 0, "gone": 0, "changed": 0, "unreadable": 0}
    if not rows:
        return done
    base = Path(root)

    def one(row: sqlite3.Row) -> tuple[sqlite3.Row, Any]:
        try:
            return row, fingerprint_file(base / row["rel_path"], stop)
        except FileNotFoundError:
            return row, "gone"
        except Unchanged:
            return row, "changed"
        except OSError as exc:
            log.debug("could not read %s for its fingerprint: %s", row["rel_path"], exc)
            return row, "unreadable"

    pending: list[tuple[int, str, int, float]] = []
    seen = 0
    with ThreadPoolExecutor(max_workers=max(1, int(threads)),
                            thread_name_prefix="ninaivu-fingerprint") as pool:
        for start in range(0, len(rows), GROUP):
            if stop() or not carry_on():
                break
            for row, result in pool.map(one, rows[start:start + GROUP]):
                seen += 1
                if result is None:
                    continue                              # stopped part-way
                if isinstance(result, str):
                    done[result] += 1
                    continue
                digest, size, mtime = result
                if size != row["size"] or abs(mtime - row["mtime"]) > 1e-6:
                    done["changed"] += 1                  # the next scan reads it again
                    continue
                # Stored at the row's own values, so the owed test compares
                # like with like whatever the file system rounds times to.
                pending.append((int(row["id"]), digest, int(row["size"]), float(row["mtime"])))
            store(conn, pending)
            done["taken"] += len(pending)
            pending.clear()
            if progress is not None:
                progress(seen, len(rows))
    return done


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """How far the fingerprints have got, per library folder, for the console."""
    roots = []
    for row in conn.execute(
            "SELECT root, COUNT(*) AS files, "
            f"SUM(CASE WHEN {UP_TO_DATE} THEN 1 ELSE 0 END) AS done "
            "FROM assets WHERE trashed=0 GROUP BY root ORDER BY root", (ALGO,)):
        roots.append({"root": row["root"], "files": int(row["files"]),
                      "fingerprinted": int(row["done"] or 0)})
    return {"algorithm": ALGO, "roots": roots,
            "files": sum(r["files"] for r in roots),
            "fingerprinted": sum(r["fingerprinted"] for r in roots)}


def duplicate_groups(conn: sqlite3.Connection, *, limit: int = 500,
                     min_size: int = 1) -> list[dict[str, Any]]:
    """Files whose bytes are exactly the same, across every library folder.

    Largest wasted space first. Only fingerprints that still describe their
    file count, so a copy edited since it was fingerprinted is not called the
    same as the original.
    """
    groups: list[dict[str, Any]] = []
    for row in conn.execute(
            "SELECT f.digest, f.size, COUNT(*) AS copies FROM content_fingerprints f "
            "JOIN assets a ON a.id=f.asset_id "
            "WHERE f.algo=? AND a.trashed=0 AND f.size=a.size AND f.mtime=a.mtime "
            "AND f.size >= ? GROUP BY f.digest, f.size HAVING COUNT(*) > 1 "
            "ORDER BY (COUNT(*) - 1) * f.size DESC, f.digest LIMIT ?",
            (ALGO, int(min_size), int(limit))).fetchall():
        files = [dict(r) for r in conn.execute(
            "SELECT a.id, a.root, a.rel_path FROM content_fingerprints f "
            "JOIN assets a ON a.id=f.asset_id WHERE f.digest=? AND f.algo=? "
            "AND a.trashed=0 AND f.size=a.size AND f.mtime=a.mtime ORDER BY a.id",
            (row["digest"], ALGO))]
        groups.append({"fingerprint": row["digest"], "size": int(row["size"]),
                       "copies": int(row["copies"]),
                       "spare_bytes": int(row["size"]) * (int(row["copies"]) - 1),
                       "files": files})
    return groups


# -- moves -------------------------------------------------------------------

def fingerprinted(conn: sqlite3.Connection, root: str,
                  rels: Iterable[str]) -> dict[str, tuple[str, int]]:
    """``rel_path -> (digest, size)`` for those of *rels* in *root* whose
    fingerprint still describes the file their row was last indexed from."""
    wanted = list(rels)
    out: dict[str, tuple[str, int]] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
                "SELECT a.rel_path, f.digest, f.size FROM assets a "
                "JOIN content_fingerprints f ON f.asset_id=a.id "
                f"WHERE a.root=? AND a.rel_path IN ({marks}) AND f.algo=? "
                "AND f.size=a.size AND f.mtime=a.mtime",
                (str(root), *chunk, ALGO)):
            out[row["rel_path"]] = (row["digest"], int(row["size"]))
    return out


def match_moves(conn: sqlite3.Connection, root: str, gone: Iterable[str],
                arrived: Iterable[tuple[str, Any]], *,
                stop: Callable[[], bool] = lambda: False) -> list[tuple[str, str]]:
    """Pair files the index has that are gone with files new to it that are
    the same bytes: ``[(old rel_path, new rel_path)]``.

    *arrived* is ``(rel_path, stat)`` of files found that the index has no row
    for. Only those the same size as a gone file with a fingerprint are read,
    so a scan that finds a thousand new photographs and nothing missing reads
    none of them here. Each gone file pairs with at most one new one: two
    identical copies moved together pair one each.
    """
    known = fingerprinted(conn, root, gone)
    if not known:
        return []
    by_size: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for rel, (digest, size) in sorted(known.items()):
        by_size[size][digest].append(rel)
    base = Path(root)
    pairs: list[tuple[str, str]] = []
    for rel, st in sorted(arrived, key=lambda item: item[0]):
        if stop():
            break
        waiting = by_size.get(int(getattr(st, "st_size", -1)))
        if not waiting:
            continue
        try:
            read = fingerprint_file(base / rel, stop)
        except (OSError, Unchanged):
            continue
        if read is None:
            break
        olds = waiting.get(read[0])
        if olds:
            pairs.append((olds.pop(0), rel))
    return pairs
