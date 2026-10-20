"""Fixes for the seventh audit's Low findings (19 October 2026).

Each test names its finding.
"""
from __future__ import annotations

import json

from ninaivu.storage import db


def _face(conn, asset_id: int, box: list[int]) -> int:
    with db._write_lock:
        cursor = conn.execute(
            "INSERT INTO faces(asset_id, source, confidence, bbox, landmarks, det_score, "
            "sharpness, quality, embedding, model, created_at) "
            "VALUES(?, 'auto', 0.9, ?, ?, 0.9, 1, 1, x'00', 'sface', 0)",
            (asset_id, json.dumps(box), json.dumps([[box[0], box[1]]])))
        conn.execute("UPDATE assets SET face_version=5 WHERE id=?", (asset_id,))
        conn.commit()
    return int(cursor.lastrowid)


# -- C-41: turning a photograph turns its face boxes and finds faces again ----

def test_a_face_box_turns_with_its_photograph(scanned):
    _, conn, _ = scanned
    asset = conn.execute("SELECT id FROM assets ORDER BY id").fetchone()["id"]
    face = _face(conn, asset, [10, 20, 30, 40])
    # A 200 x 100 picture turned a quarter clockwise is 100 x 200: the box
    # that started 10 in and 20 down now starts 100-20-40 in and 10 down.
    db.turn_faces(conn, asset, 90, (200, 100))
    row = conn.execute("SELECT bbox, landmarks FROM faces WHERE id=?", (face,)).fetchone()
    assert json.loads(row["bbox"]) == [40, 10, 40, 30]
    assert json.loads(row["landmarks"]) == [[80.0, 10.0]]
    assert conn.execute("SELECT face_version FROM assets WHERE id=?",
                        (asset,)).fetchone()[0] == 0
    # Three more quarter turns bring it back where it was.
    db.turn_faces(conn, asset, 270, (100, 200))
    assert json.loads(conn.execute("SELECT bbox FROM faces WHERE id=?",
                                   (face,)).fetchone()[0]) == [10, 20, 30, 40]
    db.turn_faces(conn, asset, 180, (200, 100))
    assert json.loads(conn.execute("SELECT bbox FROM faces WHERE id=?",
                                   (face,)).fetchone()[0]) == [160, 40, 30, 40]


def test_the_rotate_button_turns_the_faces(scanned, as_family):
    _, conn, _ = scanned
    row = conn.execute("SELECT id, width, height FROM assets WHERE kind='picture' "
                       "AND width<>height ORDER BY id").fetchone()
    face = _face(conn, row["id"], [0, 0, 10, 10])
    assert as_family.post(f"/api/asset/{row['id']}/rotate",
                          json={"rotation": 90}).status_code == 200
    box = json.loads(conn.execute("SELECT bbox FROM faces WHERE id=?",
                                  (face,)).fetchone()[0])
    assert box == [row["height"] - 10, 0, 10, 10]
    assert conn.execute("SELECT face_version FROM assets WHERE id=?",
                        (row["id"],)).fetchone()[0] == 0


# -- C-51: the probe caches stay bounded ---------------------------------------

def test_the_video_caches_start_again_when_full(monkeypatch):
    from ninaivu.media import video_compress
    seen: dict = {}
    monkeypatch.setattr(video_compress, "SEEN_MOST", 3)
    for n in range(7):
        video_compress._remember(seen, (str(n), 0, 0.0), True)
    assert len(seen) <= 3 and ("6", 0, 0.0) in seen


# -- C-52: a manifest cell never runs as a formula -----------------------------

def test_a_manifest_cell_that_looks_like_a_formula_is_shown_as_text():
    from ninaivu.archive.status_kit import cell
    assert cell('=HYPERLINK("http://x","y")') == '\'=HYPERLINK("http://x","y")'
    assert [cell(v) for v in ("+1", "-2", "@SUM(A1)", "\tx")] == ["'+1", "'-2", "'@SUM(A1)", "'\tx"]
    assert cell("2024/05/a=b.jpg") == "2024/05/a=b.jpg" and cell(12) == 12
