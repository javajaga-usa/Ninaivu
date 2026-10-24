"""Thumbnails that do not depend on where the library is (3.0, P1).

Before 3.0 a thumbnail was named by a hash of the library folder's absolute
path, so a library moved to another disk, mounted under another name or drive
letter, or opened on another computer had every thumbnail renamed (366,000
files on the family's library). Now each library folder is given a key at
start, kept in the state folder, and carried along when the library moves:
thumbnails named by it are never renamed again, those still named by the old
path are renamed once, and a copy of a library added as a library of its own
never shares its thumbnails.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ninaivu.media import media
from ninaivu.media.media import thumb_base
from ninaivu.media.scanner import Scanner
from ninaivu.storage import db, library_id, reroot, roots as roots_kit


def test_a_folder_without_a_key_is_named_by_its_path_as_before(tmp_path):
    assert roots_kit.thumb_key("/photos", state_dir=tmp_path) == "/photos"
    assert media.thumb_name("/photos", "a.jpg", tmp_path) == thumb_base("/photos", "a.jpg")


def test_each_folder_gets_its_own_key_once(tmp_path):
    roots_kit.give_thumb_keys(tmp_path, ["/a", "/b"])
    a = roots_kit.thumb_key("/a", state_dir=tmp_path)
    b = roots_kit.thumb_key("/b", state_dir=tmp_path)
    assert a.startswith("library:") and b.startswith("library:") and a != b
    roots_kit.give_thumb_keys(tmp_path, ["/a", "/b", "/c"])
    assert roots_kit.thumb_key("/a", state_dir=tmp_path) == a, "a key is never changed"
    roots_kit.move_thumb_key(tmp_path, "/a", "/moved/a")
    assert roots_kit.thumb_key("/moved/a", state_dir=tmp_path) == a
    assert roots_kit.thumb_key("/a", state_dir=tmp_path) == "/a"


def test_a_copy_of_a_library_added_as_a_library_has_thumbnails_of_its_own(tmp_path, library):
    """A copy carries the same .ninaivu-library marker; it must not share."""
    library_id.ensure_marker(library)
    copy = tmp_path / "copy"
    shutil.copytree(library, copy)
    state = tmp_path / "keys"
    state.mkdir()
    roots_kit.give_thumb_keys(state, [str(library), str(copy)])
    assert media.thumb_name(str(library), "x.jpg", state) != media.thumb_name(str(copy), "x.jpg", state)


def _scan(cfg):
    from ninaivu.server import auth
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    Scanner(cfg)._run(Path(cfg.active_root), full=True)
    return conn


def test_a_moved_library_keeps_its_thumbnails_where_they_are(cfg, tmp_path):
    old = cfg.active_root
    roots_kit.give_thumb_keys(cfg.state_dir, cfg.roots)
    key = roots_kit.thumb_key(old, state_dir=cfg.state_dir)
    conn = _scan(cfg)
    rows = {r["rel_path"]: r["thumb"] for r in conn.execute(
        "SELECT rel_path, thumb FROM assets WHERE thumb IS NOT NULL AND thumb != ''")}
    assert rows and all(base == thumb_base(key, rel) for rel, base in rows.items())
    files = sorted(p.relative_to(cfg.thumbs_dir) for p in Path(cfg.thumbs_dir).rglob("*") if p.is_file())
    conn.close()

    new = str(tmp_path / "other-disk" / "library")
    Path(new).parent.mkdir()
    shutil.move(old, new)
    report = reroot.reroot(cfg.state_dir, old, new, dry_run=False)
    assert report.thumbnails == 0 and report.renamed == 0, "nothing to rename"
    assert sorted(p.relative_to(cfg.thumbs_dir) for p in Path(cfg.thumbs_dir).rglob("*")
                  if p.is_file()) == files
    assert roots_kit.thumb_key(new, state_dir=cfg.state_dir) == key
    conn = db.init_db(cfg.db_path)
    assert {r["rel_path"]: r["thumb"] for r in conn.execute(
        "SELECT rel_path, thumb FROM assets WHERE thumb IS NOT NULL AND thumb != ''")} == rows


def test_thumbnails_named_by_the_old_path_are_renamed_once(cfg, tmp_path):
    old = cfg.active_root
    conn = _scan(cfg)                                  # no key yet: named by the path
    row = conn.execute("SELECT rel_path, thumb FROM assets WHERE thumb != '' LIMIT 1").fetchone()
    assert row["thumb"] == thumb_base(old, row["rel_path"])
    conn.close()
    roots_kit.give_thumb_keys(cfg.state_dir, cfg.roots)  # the first start of 3.0
    key = roots_kit.thumb_key(old, state_dir=cfg.state_dir)

    new = str(tmp_path / "moved")
    shutil.move(old, new)
    first = reroot.reroot(cfg.state_dir, old, new, dry_run=False)
    assert first.renamed > 0
    conn = db.init_db(cfg.db_path)
    thumb = conn.execute("SELECT thumb FROM assets WHERE rel_path=?", (row["rel_path"],)).fetchone()[0]
    assert thumb == thumb_base(key, row["rel_path"])
    shard, _, digest = thumb.rpartition("/")
    assert list((Path(cfg.thumbs_dir) / shard).glob(f"{digest}_*"))
    conn.close()

    back = str(tmp_path / "back")
    shutil.move(new, back)
    again = reroot.reroot(cfg.state_dir, new, back, dry_run=False)
    assert again.renamed == 0 and again.thumbnails == 0


def test_the_start_gives_every_library_folder_a_key(cfg):
    library_id.relocate(cfg, look=lambda root: [], mounts=lambda: [])
    assert roots_kit.thumb_key(cfg.active_root, state_dir=cfg.state_dir).startswith("library:")
