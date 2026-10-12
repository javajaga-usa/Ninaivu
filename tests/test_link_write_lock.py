"""The scan's linking passes write in short transactions under the in-process
write lock, and leave no transaction open behind them.

They wrote as they read, so their write transaction stayed open from the
first change to the end of the pass (0.6 to 1.9 seconds at 208,000 rows)
without the lock every other writer takes. And the pass that makes pairs
alike ended on an INSERT that usually adds nothing and then did not commit,
so the scan's connection held the database's write lock into the next pass.
"""

from __future__ import annotations

import sqlite3

import pytest

from ninaivu.media.scanner import Scanner
from ninaivu.storage import db


class Watched:
    """A connection that notes every write made without the write lock."""

    def __init__(self, conn):
        self._conn = conn
        self.unlocked: list[str] = []

    def _check(self, sql):
        if sql.lstrip().split(None, 1)[0].upper() in ("UPDATE", "INSERT", "DELETE"):
            if not db._write_lock.locked():
                self.unlocked.append(sql.strip().split("\n")[0][:60])

    def execute(self, sql, *args):
        self._check(sql)
        return self._conn.execute(sql, *args)

    def executemany(self, sql, *args):
        self._check(sql)
        return self._conn.executemany(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _add(conn, root, rel, kind, **extra):
    import os
    name = os.path.basename(rel)
    values = {"root": root, "rel_path": rel, "filename": name,
              "folder": os.path.dirname(rel), "kind": kind,
              "ext": os.path.splitext(name)[1].lstrip(".").lower(), **extra}
    conn.execute(f"INSERT INTO assets({', '.join(values)}) "
                 f"VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
    conn.commit()


@pytest.fixture()
def index(scanned):
    cfg, conn, _ = scanned
    root = cfg.active_root
    _add(conn, root, "p/IMG_1.JPG", "picture", visibility=1)
    _add(conn, root, "p/IMG_1.CR2", "picture")
    _add(conn, root, "p/IMG_2.HEIC", "picture", is_live=1, live_video_path="p/img_2.mov")
    _add(conn, root, "p/IMG_2.MOV", "video", duration=2.0)
    _add(conn, root, "p/IMG_3.HEIC", "picture", is_live=1, live_video_path="p/IMG_3.MOV")
    # Pointing at a RAW that has gone, with another beside it now.
    _add(conn, root, "p/IMG_4.JPG", "picture", raw_path="p/IMG_4.NEF")
    _add(conn, root, "p/IMG_4.CR2", "picture")
    return cfg, conn, root


def test_the_linking_passes_write_under_the_lock(index):
    cfg, conn, root = index
    watched = Watched(conn)
    scanner = Scanner(cfg)
    scanner._link_live_photos(watched, root)
    scanner._link_raw_pairs(watched, root)
    scanner._align_pairs(watched, root)
    assert watched.unlocked == []
    # And what they wrote is what they wrote before.
    def row(rel):
        return conn.execute("SELECT * FROM assets WHERE rel_path=?", (rel,)).fetchone()

    assert row("p/IMG_1.JPG")["raw_path"] == "p/IMG_1.CR2"
    assert row("p/IMG_1.CR2")["raw_twin"] == 1 and row("p/IMG_1.CR2")["visibility"] == 1
    assert row("p/IMG_2.HEIC")["live_video_path"] == "p/IMG_2.MOV"
    assert row("p/IMG_2.MOV")["live_clip"] == 1
    assert row("p/IMG_3.HEIC")["is_live"] == 0
    assert row("p/IMG_4.JPG")["raw_path"] == "p/IMG_4.CR2"


def test_making_pairs_alike_leaves_no_transaction_open(index):
    cfg, conn, root = index
    scanner = Scanner(cfg)
    scanner._link_live_photos(conn, root)
    scanner._link_raw_pairs(conn, root)
    scanner._align_pairs(conn, root)
    scanner._align_pairs(conn, root)          # nothing left to change
    assert not conn.in_transaction
    other = sqlite3.connect(cfg.db_path, timeout=0)
    try:
        other.execute("UPDATE assets SET caption=caption WHERE id=-1")
        other.commit()
    finally:
        other.close()
