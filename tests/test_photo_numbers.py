"""A photograph's number is never given to another file (audit C-22).

SQLite gives a new row one past the largest id *now*. With the newest
photograph erased, the next file indexed took its number, and a film made
with the old one played the new one, or the job engine's "failed" mark for
the old file fell on the new.
"""
from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

from ninaivu.media.scanner import Scanner
from ninaivu.storage import db, recycle


def _rescan(cfg) -> None:
    Scanner(cfg)._run(Path(cfg.active_root), full=False)


def test_an_erased_photograph_s_number_is_not_given_again(scanned):
    cfg, conn, _ = scanned
    newest = conn.execute("SELECT id, rel_path FROM assets ORDER BY id DESC").fetchone()
    with db._write_lock:
        conn.execute("DELETE FROM assets WHERE id=?", (newest["id"],))
        conn.commit()
    Image.new("RGB", (64, 48), (10, 120, 30)).save(Path(cfg.active_root) / "arrived.jpg")
    _rescan(cfg)
    arrived = conn.execute("SELECT id FROM assets WHERE rel_path='arrived.jpg'").fetchone()["id"]
    assert arrived > newest["id"]


def test_one_binned_and_restored_comes_back_under_a_new_number(scanned):
    cfg, conn, _ = scanned
    newest = conn.execute("SELECT id FROM assets WHERE kind='picture' ORDER BY id DESC").fetchone()["id"]
    top = conn.execute("SELECT MAX(id) FROM assets").fetchone()[0]
    recycle.recycle(conn, [newest])
    entry = recycle.listing(conn)[0]
    assert recycle.restore(conn, [entry["id"]])["restored"] == 1
    back = conn.execute("SELECT MAX(id) FROM assets").fetchone()[0]
    assert back > top


def test_numbers_given_together_stay_distinct(scanned):
    cfg, conn, _ = scanned
    top = conn.execute("SELECT MAX(id) FROM assets").fetchone()[0]
    with db._write_lock:
        conn.execute("DELETE FROM assets WHERE id=?", (top,))
        conn.commit()
    root = Path(cfg.active_root)
    for n in range(3):
        Image.new("RGB", (40, 30), (n * 60, 0, 0)).save(root / f"batch{n}.jpg")
    time.sleep(0.01)
    _rescan(cfg)
    ids = [r[0] for r in conn.execute("SELECT id FROM assets WHERE rel_path LIKE 'batch%' ORDER BY id")]
    assert len(set(ids)) == 3 and min(ids) > top


def test_an_older_index_gains_the_trigger(cfg):
    conn = db.init_db(cfg.db_path)
    with db._write_lock:
        conn.execute("DROP TRIGGER assets_high_water")
        conn.commit()
    again = db.init_db(cfg.db_path)
    assert again.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                         "AND name='assets_high_water'").fetchone()
