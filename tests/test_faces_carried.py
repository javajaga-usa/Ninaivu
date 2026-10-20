"""What people said about faces survives a new search for them (audit C-13).

A new face model, a turned photograph or a fresh pass finds every face again.
The names, the "this is not that person" answers and a person's cover go
with the face they were about, one to one.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from ninaivu.storage import db, recycle


def _faces(*boxes):
    return [{"bbox": list(b), "landmarks": [], "det_score": 0.9, "sharpness": 1, "quality": 1,
             "embedding": np.zeros(128, "float32").tobytes()} for b in boxes]


@pytest.fixture()
def photo(scanned):
    _, conn, _ = scanned
    asset = conn.execute("SELECT id FROM assets WHERE kind='picture' ORDER BY id").fetchone()["id"]
    amma = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Amma', 0)").lastrowid
    appa = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Appa', 0)").lastrowid
    conn.commit()
    return conn, asset, amma, appa


def _rows(conn, asset):
    return [dict(r) for r in conn.execute(
        "SELECT id, person_id, source, bbox FROM faces WHERE asset_id=? ORDER BY id", (asset,))]


def _name(conn, face_id, person):
    with db._write_lock:
        conn.execute("UPDATE faces SET person_id=?, source='confirmed', confidence=1 WHERE id=?",
                     (person, face_id))
        conn.commit()


def test_one_name_goes_to_one_face_the_best_overlap(photo):
    conn, asset, amma, _ = photo
    db.replace_asset_faces(conn, asset, _faces((100, 100, 100, 100)), "sface")
    _name(conn, _rows(conn, asset)[0]["id"], amma)
    # Two new faces both overlap Amma's old box by more than half: only the
    # closer one is her (before the fix, both were, and both "confirmed").
    db.replace_asset_faces(conn, asset, _faces((130, 100, 100, 100), (105, 100, 100, 100)), "arcface")
    rows = _rows(conn, asset)
    assert [r["person_id"] for r in rows] == [None, amma]
    assert [r["source"] for r in rows] == ["none", "confirmed"]


def test_a_name_that_fits_no_new_face_is_kept_and_carried_later(photo):
    conn, asset, amma, _ = photo
    db.replace_asset_faces(conn, asset, _faces((100, 100, 100, 100)), "sface")
    _name(conn, _rows(conn, asset)[0]["id"], amma)
    db.replace_asset_faces(conn, asset, _faces((400, 400, 80, 80)), "arcface")
    assert [r["person_id"] for r in _rows(conn, asset)] == [None]
    waiting = db.faces_not_carried(conn, [r[0] for r in conn.execute("SELECT DISTINCT root FROM assets")])
    assert waiting["count"] == 1 and waiting["items"][0]["name"] == "Amma"
    # Found again where she was, by the next search: her name goes back.
    db.replace_asset_faces(conn, asset, _faces((400, 400, 80, 80), (98, 102, 104, 98)), "arcface")
    assert [r["person_id"] for r in _rows(conn, asset)] == [None, amma]
    assert conn.execute("SELECT COUNT(*) FROM faces_not_carried").fetchone()[0] == 0


def test_a_tighter_box_round_the_same_face_still_takes_the_name(photo):
    conn, asset, amma, _ = photo
    db.replace_asset_faces(conn, asset, _faces((100, 100, 120, 140)), "sface")
    _name(conn, _rows(conn, asset)[0]["id"], amma)
    # Under half overlap, but the old face's centre is inside the new box.
    db.replace_asset_faces(conn, asset, _faces((125, 135, 75, 85)), "arcface")
    assert [r["person_id"] for r in _rows(conn, asset)] == [amma]


def test_rejections_and_the_cover_go_with_the_face(photo):
    conn, asset, amma, appa = photo
    db.replace_asset_faces(conn, asset, _faces((100, 100, 100, 100), (300, 100, 100, 100)), "sface")
    first, second = (r["id"] for r in _rows(conn, asset))
    _name(conn, first, amma)
    db.reject_face_for_person(conn, second, amma)
    with db._write_lock:
        conn.execute("UPDATE people_clusters SET cover_face_id=? WHERE id=?", (first, amma))
        conn.execute("UPDATE people_clusters SET cover_face_id=? WHERE id=?", (second, appa))
        conn.commit()
    db.replace_asset_faces(conn, asset, _faces((302, 98, 100, 100), (101, 101, 100, 100)), "arcface")
    now_second, now_first = (r["id"] for r in _rows(conn, asset))
    assert [tuple(r) for r in conn.execute("SELECT face_id, person_id FROM face_rejections")] == [(now_second, amma)]
    covers = dict(conn.execute("SELECT id, cover_face_id FROM people_clusters").fetchall())
    assert covers[amma] == now_first and covers[appa] == now_second


def test_names_waiting_follow_a_merge_and_come_back_from_the_bin(photo):
    conn, asset, amma, appa = photo
    db.replace_asset_faces(conn, asset, _faces((100, 100, 100, 100)), "sface")
    _name(conn, _rows(conn, asset)[0]["id"], amma)
    db.replace_asset_faces(conn, asset, _faces(), "arcface")
    db.merge_people(conn, amma, appa)
    assert [tuple(r) for r in conn.execute("SELECT person_id FROM faces_not_carried")] == [(appa,)]
    recycle.recycle(conn, [asset])
    assert conn.execute("SELECT COUNT(*) FROM faces_not_carried").fetchone()[0] == 0
    entry = recycle.listing(conn)[0]
    assert recycle.restore(conn, [entry["id"]])["restored"] == 1
    assert [tuple(r) for r in conn.execute("SELECT person_id FROM faces_not_carried")] == [(appa,)]


def test_an_older_index_gains_the_table_and_keeps_its_faces(cfg):
    conn = db.init_db(cfg.db_path)
    with db._write_lock:
        conn.execute("DROP TABLE faces_not_carried")
        conn.commit()
    again = db.init_db(cfg.db_path)
    assert again.execute("SELECT name FROM sqlite_master WHERE name='faces_not_carried'").fetchone()


def test_the_console_lists_names_waiting(scanned, as_admin):
    _, conn, _ = scanned
    asset = conn.execute("SELECT id FROM assets WHERE kind='picture' ORDER BY id").fetchone()["id"]
    amma = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Amma', 0)").lastrowid
    conn.execute("INSERT INTO faces_not_carried(asset_id, person_id, bbox, model, created_at) "
                 "VALUES (?, ?, ?, 'sface', 1)", (asset, amma, json.dumps([1, 2, 3, 4])))
    conn.commit()
    body = as_admin.get("/api/faces/not-carried").get_json()
    assert body["count"] == 1 and body["items"][0]["name"] == "Amma"
