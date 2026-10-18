"""A rescan that finds nothing new does next to nothing, and still notices.

The library on the Mac mini is 208,000 files, and the watcher rescans a
folder every time a photograph lands in it. Every check that read the whole
index on each of those rescans now either reads a few rows through an index
made for it, or is skipped when nothing it reads has changed since it last
ran. These tests hold both halves: the quick way gives the answer the long
way gave, and a change still reaches every check.
"""

from pathlib import Path

import pytest
from PIL import Image

from ninaivu.media import scanner as scanner_mod
from ninaivu.media.scanner import Scanner, walk_media
from ninaivu.server import auth
from ninaivu.server.config import media_kind, media_kind_named
from ninaivu.storage import db


def open_db(cfg):
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    return conn


@pytest.mark.parametrize("name", [
    "IMG_0001.jpg", "IMG_0001.JPG", "clip.MOV", "song.mp3", "notes.txt",
    "archive.tar.gz", "no_extension", ".jpg", "..jpg", "photo.", "photo.jpg.",
    ".hidden.png", "a.b.c.heic", "RAW.CR2", "x.webp",
])
def test_the_kind_from_a_name_is_the_kind_from_its_path(tmp_path, name):
    path = tmp_path / name
    assert media_kind_named(name, str(path)) == media_kind(path)


def test_the_walk_names_files_as_relpath_would(cfg, library):
    import os

    nested = library / "2024" / "deep" / "er" / "still" / "a b" / "c.jpg"
    nested.parent.mkdir(parents=True)
    Image.new("RGB", (40, 30)).save(nested)
    walked = sorted(rel for rel, _ in walk_media(Path(library), cfg))
    expected = sorted(
        os.path.relpath(os.path.join(folder, name), library).replace("\\", "/")
        for folder, _, names in os.walk(library) for name in names
        if media_kind(os.path.join(folder, name)) != "unknown")
    assert walked == expected
    assert "2024/deep/er/still/a b/c.jpg" in walked
    # And from a folder inside the library, relative to that folder.
    assert [rel for rel, _ in walk_media(Path(library) / "2024", cfg)] == [
        "deep/er/still/a b/c.jpg"]


def test_a_rescan_with_nothing_missing_does_not_look_for_what_is_missing(
        cfg, library, monkeypatch):
    conn = open_db(cfg)
    scanner = Scanner(cfg)
    scanner._run(library, full=True)
    asked = []
    monkeypatch.setattr(db, "delete_missing",
                        lambda *a, **k: asked.append("delete") or [])
    monkeypatch.setattr(db, "live_paths", lambda *a, **k: asked.append("live") or set())
    scanner._run(library, full=False)
    assert asked == []
    assert scanner.progress.status == "done"
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] > 0


def test_a_file_that_went_missing_is_still_removed(cfg, library):
    conn = open_db(cfg)
    scanner = Scanner(cfg)
    scanner._run(library, full=True)
    before = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    (library / "misc" / "plain.png").unlink()
    scanner._run(library, full=False)
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == before - 1
    assert conn.execute(
        "SELECT 1 FROM assets WHERE rel_path='misc/plain.png'").fetchone() is None


@pytest.mark.parametrize("index, sql, params", [
    ("idx_assets_audio",
     "SELECT id, rel_path, thumb, duration FROM assets "
     "WHERE root=? AND kind='audio' AND trashed=0 ORDER BY id", 1),
    ("idx_assets_unread",
     "SELECT rel_path, thumb FROM assets WHERE root=? AND trashed=0 "
     "AND thumb IS NOT NULL AND width IS NULL AND kind IN ('picture', 'video')", 1),
    ("idx_assets_stills",
     "SELECT s.id FROM assets s JOIN assets c "
     "ON c.root = s.root AND c.rel_path = s.live_video_path "
     "WHERE s.root=? AND s.is_live=1 AND c.live_clip=1", 1),
    ("idx_assets_raws",
     "SELECT s.id FROM assets s JOIN assets c ON c.root = s.root AND c.rel_path = s.raw_path "
     "WHERE s.root=? AND COALESCE(s.raw_path,'')<>'' AND c.raw_twin=1", 1),
])
def test_the_recurring_checks_are_answered_by_their_index(cfg, library, index, sql, params):
    """The partial indexes only work while the query says, word for word, what
    the index's WHERE says. This fails if either drifts from the other."""
    conn = open_db(cfg)
    Scanner(cfg)._run(library, full=True)
    rows = [{"root": str(library), "rel_path": f"many/{i:05d}.jpg", "kind": "picture",
             "filename": f"{i:05d}.jpg", "folder": "many", "ext": "jpg",
             "size": 1, "mtime": 1.0, "thumb": f"t{i}", "width": 10, "height": 10}
            for i in range(3000)]
    db.bulk_upsert(conn, rows)
    db.refresh_statistics(conn, force=True)
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN " + sql, (str(library),) * params))
    assert index in plan


def test_the_quality_check_reads_its_index(cfg, library):
    conn = open_db(cfg)
    Scanner(cfg)._run(library, full=True)
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT id, thumb, width, height FROM assets "
        "WHERE root=? AND kind='picture' AND trashed=0 "
        "AND thumb IS NOT NULL AND quality_version < ?", (str(library), 1)))
    assert "SCAN assets" not in plan


# -- checks skipped when nothing they read has changed ------------------------

def _count_calls(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_screens_by_name_are_not_looked_for_again_when_nothing_changed(
        cfg, library, monkeypatch):
    cfg.hide_screens = True
    conn = open_db(cfg)
    scanner = Scanner(cfg)
    scanner._run(library, full=True)
    calls = _count_calls(monkeypatch, db, "hide_screens")
    scanner._run(library, full=False)
    scanner._run(library, full=False)
    assert calls == []

    # A row that is now the rule's to judge is judged on the next scan: a
    # screenshot whose folder rule was cleared, so its level is nobody's choice.
    shot = library / "Screenshots" / "Screenshot_1.png"
    shot.parent.mkdir()
    Image.new("RGB", (60, 40), (1, 2, 3)).save(shot)
    scanner._run(library, full=False)
    assert len(calls) == 1
    with db._write_lock:
        conn.execute("UPDATE assets SET visibility=1, vis_source='manual' "
                     "WHERE filename='Screenshot_1.png'")
        conn.commit()
    scanner._run(library, full=False)
    assert len(calls) == 2
    assert conn.execute("SELECT visibility FROM assets WHERE filename='Screenshot_1.png'"
                        ).fetchone()[0] == 1          # an admin's own level is kept
    with db._write_lock:
        conn.execute("UPDATE assets SET vis_source='default' "
                     "WHERE filename='Screenshot_1.png'")
        conn.commit()
    scanner._run(library, full=False)
    assert len(calls) == 3
    assert conn.execute(
        "SELECT visibility, vis_source FROM assets WHERE filename='Screenshot_1.png'"
    ).fetchone()[:] == (2, "screen")


def test_occasions_are_not_fingerprinted_again_when_nothing_changed(
        cfg, library, monkeypatch):
    conn = open_db(cfg)
    scanner = Scanner(cfg)
    scanner._run(library, full=True)
    # The passes after the grouping (place names, the screenshot rule) write
    # to rows too, so the first rescan reads the inputs once more and finds
    # them as they were.
    scanner._run(library, full=False)
    inputs = _count_calls(monkeypatch, db, "occasion_inputs")
    rebuilt = _count_calls(monkeypatch, db, "rebuild_occasions")
    scanner._run(library, full=False)
    scanner._run(library, full=False)
    assert inputs == [] and rebuilt == []

    # A photograph's time changes: the inputs are read and the groups made again.
    with db._write_lock:
        conn.execute("UPDATE assets SET captured_at = COALESCE(captured_at, 0) + 86400 * 400 "
                     "WHERE filename='shot0.jpg'")
        conn.commit()
    scanner._run(library, full=False)
    assert len(inputs) == 1 and len(rebuilt) == 1
    # And a changed setting is a reason on its own.
    cfg.occasion_gap_hours = float(cfg.occasion_gap_hours) + 1
    scanner._run(library, full=False)
    assert len(inputs) == 2 and len(rebuilt) == 2
    scanner._run(library, full=False)
    assert len(inputs) == 2


def test_without_the_counter_every_check_runs(cfg, library, monkeypatch):
    open_db(cfg)
    scanner = Scanner(cfg)
    scanner._run(library, full=True)
    monkeypatch.setattr(db, "meta_counter", lambda conn, key: None)
    inputs = _count_calls(monkeypatch, db, "occasion_inputs")
    scanner._run(library, full=False)
    assert inputs == [1]
    assert scanner_mod.Scanner._as_left(db.connect(cfg.db_path), "x") is None
