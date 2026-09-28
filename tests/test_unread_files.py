"""A file the indexer never read, claiming a thumbnail it does not have.

The move from Windows gave 156 damaged photographs thumbnail names. Each was
then put to the image model on every scan and passed over, and the gallery
asked for pictures that did not exist. Read again once, a damaged file is
written back with no thumbnail, and a repaired one gets its picture.
"""
import os
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.media.scanner import Scanner
from ninaivu.storage import db


@pytest.fixture()
def library(tmp_path):
    from ninaivu.server import auth
    from ninaivu.server.config import Config

    root = tmp_path / "lib"
    (root / "2014/11/17").mkdir(parents=True)
    (root / "2014/11/17/IMG_2016.JPG").write_bytes(b"\x62\x10\x1e\x73" * 4000)   # damaged
    Image.new("RGB", (80, 60), (10, 120, 200)).save(root / "2014/11/17/good.jpg", "JPEG")
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(root)]
    cfg.active_root = str(root)
    cfg.ai_enabled = False
    cfg.ai_engine = "off"
    cfg.ai_models_dir = str(tmp_path / "ai-models")
    cfg.watch = False
    cfg.min_media_bytes = 0
    cfg.ensure_dirs()
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    scanner = Scanner(cfg)
    scanner._run(root, full=True)
    return cfg, conn, scanner, root


def row(conn, name):
    return conn.execute("SELECT * FROM assets WHERE filename=?", (name,)).fetchone()


def test_a_damaged_file_given_a_thumbnail_name_loses_it(library):
    cfg, conn, scanner, root = library
    assert row(conn, "IMG_2016.JPG")["thumb"] is None, "never had one"
    conn.execute("UPDATE assets SET thumb='ab/abcdef' WHERE filename='IMG_2016.JPG'")
    conn.commit()

    scanner._run(root, full=False)

    assert row(conn, "IMG_2016.JPG")["thumb"] is None
    # Not put to the image model again: tagging asks only for rows with one.
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE thumb IS NOT NULL AND "
                        "filename='IMG_2016.JPG'").fetchone()[0] == 0


def test_a_file_repaired_since_gets_its_picture(library):
    cfg, conn, scanner, root = library
    fixed = root / "2014/11/17/IMG_2016.JPG"
    before = fixed.stat()
    Image.new("RGB", (80, 60), (200, 60, 20)).save(fixed, "JPEG")
    # The damaged copy's time, and the index told the new size, so the file
    # looks unchanged and only this pass would notice it.
    os.utime(fixed, (before.st_atime, before.st_mtime))
    conn.execute("UPDATE assets SET thumb='ab/abcdef', size=? WHERE filename='IMG_2016.JPG'",
                 (fixed.stat().st_size,))
    conn.commit()

    scanner._run(root, full=False)

    repaired = row(conn, "IMG_2016.JPG")
    assert repaired["thumb"] and repaired["width"] == 80
    assert Path(cfg.thumbs_dir, f"{repaired['thumb']}_{max(cfg.thumb_sizes)}.webp").is_file()


def test_a_file_with_its_thumbnail_is_not_read_again(library, monkeypatch):
    cfg, conn, scanner, root = library
    seen = []
    real = scanner._index
    monkeypatch.setattr(scanner, "_index", lambda c, r, found, known: (seen.extend(found),
                                                                       real(c, r, found, known)))
    scanner._run(root, full=False)
    assert seen == []
