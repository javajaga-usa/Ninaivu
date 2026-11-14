"""Fixes for the seventh audit's Low findings (19 October 2026).

Each test names its finding.
"""
from __future__ import annotations

import json
from pathlib import Path

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

def test_the_damage_cache_starts_again_when_full(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    from ninaivu.media import media, video_damage
    monkeypatch.setattr(video_damage, "SEEN_MOST", 3)
    monkeypatch.setattr(video_damage, "_damage_seen", {})
    monkeypatch.setattr(media, "FFPROBE", "ffprobe")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stderr=b"moov atom not found"))
    for n in range(7):
        clip = tmp_path / f"{n}.mp4"
        clip.write_bytes(b"x")
        assert video_damage.damaged(clip) is True
    assert len(video_damage._damage_seen) <= 3


def test_half_written_compressed_copies_from_before_are_removed_once(scanned):
    """Compress & Replace is gone; what a restart in the middle of one left
    beside a video goes at the next start, and nothing else in the folder."""
    from ninaivu.media import video_damage
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    clip = root / "films" / "wedding.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"\x00\x00\x00\x18ftypisom" + bytes(64))
    conn.execute("UPDATE assets SET kind='video', rel_path='films/wedding.mp4', "
                 "folder='films', filename='wedding.mp4' WHERE id=(SELECT MIN(id) FROM assets)")
    conn.commit()
    left = clip.parent / ".wedding.0123456789ab.compress.tmp"
    left.write_bytes(bytes(4096))
    mine = clip.parent / ".wedding.notes.tmp"                 # somebody's own hidden file
    mine.write_bytes(b"keep")
    named_alike = clip.parent / "holiday.0123456789ab.compress.tmp"   # not hidden: not ours
    named_alike.write_bytes(b"keep")

    assert video_damage.clear_compress_leftovers(conn) == 1
    assert not left.exists()
    assert clip.exists() and mine.exists() and named_alike.exists()
    # Once: a file of that name written afterwards is nobody's leftover.
    left.write_bytes(bytes(16))
    assert video_damage.clear_compress_leftovers(conn) == 0
    assert left.exists()


def test_the_leftovers_are_looked_for_again_while_a_drive_is_away(scanned):
    from ninaivu.media import video_damage
    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET kind='video', root='/no/such/drive', rel_path='a/b.mp4' "
                 "WHERE id=(SELECT MIN(id) FROM assets)")
    conn.commit()
    assert video_damage.clear_compress_leftovers(conn) == 0
    assert conn.execute("SELECT 1 FROM meta WHERE key=?",
                        (video_damage.LEFTOVERS_CLEARED,)).fetchone() is None


# -- C-52: a manifest cell never runs as a formula -----------------------------

def test_a_manifest_cell_that_looks_like_a_formula_is_shown_as_text():
    from ninaivu.archive.status_kit import cell
    assert cell('=HYPERLINK("http://x","y")') == '\'=HYPERLINK("http://x","y")'
    assert [cell(v) for v in ("+1", "-2", "@SUM(A1)", "\tx")] == ["'+1", "'-2", "'@SUM(A1)", "'\tx"]
    assert cell("2024/05/a=b.jpg") == "2024/05/a=b.jpg" and cell(12) == 12


# -- C-56: an ended session is told so, not shown an empty library -------------

def test_an_ended_session_is_asked_to_sign_in_again(scanned, as_family):
    _, conn, _ = scanned
    assert as_family.get("/api/assets?limit=5").status_code == 200
    with db._write_lock:
        conn.execute("DELETE FROM sessions")
        conn.commit()
    ended = as_family.get("/api/assets?limit=5")
    assert ended.status_code == 401 and ended.get_json()["session_ended"] is True
    assert any("ninaivu" in h.lower() and "expires=" in h.lower()
               for h in ended.headers.getlist("Set-Cookie"))
    # The cookie is gone, so open browsing carries on as before.
    assert as_family.get("/api/assets?limit=5").status_code == 200
