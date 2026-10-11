"""External audit 2026-10-11, DATA-01: how hard commits are pushed to disk is
a stated choice, NORMAL by default, FULL when a household asks for it."""
from __future__ import annotations

import threading

import pytest

from ninaivu.storage import db

LEVEL = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}


def _level_in_a_fresh_thread(path):
    out = {}

    def run():
        conn = db.connect(path)
        out["level"] = LEVEL[conn.execute("PRAGMA synchronous").fetchone()[0]]
        out["journal"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return out


@pytest.mark.parametrize("told, level", [(None, "NORMAL"), ("full", "FULL"), ("FULL", "FULL"),
                                         ("normal", "NORMAL"), ("off", "NORMAL")])
def test_the_setting_reaches_every_new_connection(tmp_path, monkeypatch, told, level):
    if told is None:
        monkeypatch.delenv("NINAIVU_DB_SYNCHRONOUS", raising=False)
    else:
        monkeypatch.setenv("NINAIVU_DB_SYNCHRONOUS", told)
    got = _level_in_a_fresh_thread(tmp_path / "x.db")
    assert got == {"level": level, "journal": "wal"}
