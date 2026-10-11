"""Sixth audit, speed and data safety at scale (11 October 2026).

Measured on a synthetic index of 208,000 items (14,184 Live pairs, 2,886
RAW+JPEG pairs, 41 albums one of 50,000 items, 400,000 faces, 9,000
favourites). These pin what was found there with plans, counts and states
rather than timings, which a shared runner cannot keep.
"""

from __future__ import annotations

import threading

from ninaivu.api import admin_api
from ninaivu.media import video_compress as vc
from ninaivu.storage import db


def _on_a_thread(work):
    """Run *work* on a fresh thread, as start-up runs on its own, and hand back
    what it returned or raised."""
    out: dict = {}

    def run():
        try:
            out["value"] = work()
        except BaseException as exc:                     # noqa: BLE001
            out["error"] = exc
        finally:
            db.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(30)
    if "error" in out:
        raise out["error"]
    return out.get("value")


# --- P-01: start-up ran on a closed connection ------------------------------

def test_carrying_on_compressions_leaves_the_start_up_threads_connection_open(cfg):
    """resume_compressions closed the thread's own connection, the one every
    later step of start-up was handed: no interrupted job was carried on, the
    screenshot rule and the warm-up never ran, and every start walked the
    whole library."""
    db.init_db(cfg.db_path)
    vc._reset_for_tests()

    def boot():
        db.connect(cfg.db_path).execute("SELECT 1")
        admin_api.resume_compressions(cfg)
        return db.connect(cfg.db_path).execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    try:
        assert _on_a_thread(boot) == 0
    finally:
        vc._reset_for_tests()


def test_a_connection_somebody_closed_is_opened_again(tmp_path):
    path = tmp_path / "index.db"
    db.init_db(path).close()
    again = db.connect(path)
    assert again.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert db.connect(path) is again
