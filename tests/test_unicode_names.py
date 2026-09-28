"""A file whose name is spelled in another Unicode form is the same file.

Windows keeps a name as it was typed, often with é as one character; a Mac can
list the same file with the e and its accent apart, and on an NTFS drive cannot
open it by the old spelling. After a move from Windows the index held 126 such
names, each looking missing, and the first scan on the Mac had indexed every one
again: 126 pairs, one with the history and one without.
"""
import time
import unicodedata
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.media.scanner import Scanner
from ninaivu.storage import db

MAC = unicodedata.normalize("NFD", "2004/04/24/VADIVÉL.jpg")       # as a Mac lists it
WINDOWS = unicodedata.normalize("NFC", "2004/04/24/VADIVÉL.jpg")   # as Windows kept it


@pytest.fixture()
def library(tmp_path):
    from ninaivu.server import auth
    from ninaivu.server.config import Config

    root = tmp_path / "lib"
    (root / "2004/04/24").mkdir(parents=True)
    Image.new("RGB", (64, 48), (40, 90, 160)).save(root / MAC, "JPEG")
    Image.new("RGB", (64, 48), (160, 90, 40)).save(root / "2004/04/24/plain.jpg", "JPEG")
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
    listed = {p.relative_to(root).as_posix() for p in root.rglob("*.jpg")}
    if MAC not in listed:
        pytest.skip("this file system changes the spelling of names it stores")
    return cfg, conn, scanner, str(root)


def rows(conn, root):
    return {r["rel_path"]: dict(r) for r in conn.execute(
        "SELECT id, rel_path, filename, folder FROM assets WHERE root=?", (root,))}


def test_a_row_under_the_old_spelling_takes_the_new_one(library):
    cfg, conn, scanner, root = library
    conn.execute("UPDATE assets SET rel_path=?, filename=? WHERE rel_path=?",
                 (WINDOWS, WINDOWS.rsplit("/", 1)[1], MAC))
    conn.commit()
    before = rows(conn, root)[WINDOWS]["id"]

    scanner._run(Path(root), full=False)

    after = rows(conn, root)
    assert MAC in after and WINDOWS not in after
    assert after[MAC]["id"] == before, "the same row, history and all"
    assert after[MAC]["filename"] == MAC.rsplit("/", 1)[1]
    assert len(after) == 2


def test_a_pair_left_by_an_earlier_scan_becomes_one(library):
    cfg, conn, scanner, root = library
    duplicate = rows(conn, root)[MAC]["id"]
    # The row from Windows, with its history: in an album.
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, kind, size, mtime, thumb, "
        "indexed_at) SELECT root, ?, filename, folder, kind, size, mtime, 'aa/older', "
        "indexed_at FROM assets WHERE id=?", (WINDOWS, duplicate))
    older = conn.execute("SELECT id FROM assets WHERE rel_path=?", (WINDOWS,)).fetchone()[0]
    conn.execute("INSERT INTO albums(name, created_at) VALUES ('Family', ?)", (time.time(),))
    album = conn.execute("SELECT id FROM albums").fetchone()[0]
    conn.execute("INSERT INTO album_items(album_id, asset_id, added_at) VALUES (?,?,?)",
                 (album, older, time.time()))
    # The copy the Mac made was favourited today.
    conn.execute("INSERT INTO users(username, display_name, role, created_at) "
                 "VALUES ('dad', 'Dad', 'admin', 0)")
    user = conn.execute("SELECT id FROM users").fetchone()[0]
    conn.execute("INSERT INTO user_assets(user_id, asset_id, favorite) VALUES (?,?,1)",
                 (user, duplicate))
    # The cloud backup has sent the old spelling and queued the new one.
    from ninaivu.cloud import store
    store.init_schema(conn)
    for rel, state in ((WINDOWS, "done"), (MAC, "pending")):
        conn.execute("INSERT INTO cloud_uploads(root, rel_path, filename, size, state) "
                     "VALUES (?,?,?,1,?)", (root, rel, rel.rsplit("/", 1)[1], state))
    conn.commit()

    scanner._run(Path(root), full=False)

    after = rows(conn, root)
    assert len(after) == 2 and WINDOWS not in after
    assert after[MAC]["id"] == older, "the row with the history is kept"
    assert conn.execute("SELECT asset_id FROM album_items").fetchone()[0] == older
    assert conn.execute("SELECT asset_id FROM user_assets").fetchone()[0] == older
    cloud = conn.execute("SELECT rel_path, state FROM cloud_uploads").fetchall()
    assert [tuple(r) for r in cloud] == [(MAC, "done")], "one record, the one already sent"


def test_a_file_that_is_really_gone_is_not_matched_to_another(library):
    cfg, conn, scanner, root = library
    conn.execute("UPDATE assets SET rel_path='2004/04/24/other.jpg' WHERE rel_path=?", (MAC,))
    conn.commit()
    scanner._run(Path(root), full=False)
    assert "2004/04/24/other.jpg" not in rows(conn, root)
