"""tools/db_maintenance.py --prune-thumbs: it removes only what nothing uses."""
import os
import time
from types import SimpleNamespace

from ninaivu.media.media import thumb_base
from ninaivu.storage import db

from tools import db_maintenance


def _thumbs(state, base, *, age=7200):
    shard, _, digest = base.rpartition("/")
    folder = state / "thumbs" / shard
    folder.mkdir(parents=True, exist_ok=True)
    files = [folder / f"{digest}_{size}.webp" for size in (256, 640)]
    then = time.time() - age
    for path in files:
        path.write_bytes(b"x" * 10)
        os.utime(path, (then, then))
    return files


def test_prune_removes_orphans_and_keeps_every_thumbnail_in_use(tmp_path):
    state = tmp_path / "state"
    (state / "thumbs").mkdir(parents=True)
    conn = db.init_db(state / "index.db")
    live = thumb_base("/photos", "a.jpg")
    binned = thumb_base("/photos", "b.jpg")
    waiting = thumb_base("/photos", "c.jpg")
    orphan = thumb_base("/photos", "gone.jpg")
    fresh = thumb_base("/photos", "being-scanned.jpg")
    conn.execute("INSERT INTO assets(root, rel_path, filename, kind, thumb) "
                 "VALUES ('/photos', 'a.jpg', 'a.jpg', 'picture', ?)", (live,))
    conn.execute("INSERT INTO recycled(root, rel_path, filename, thumb, bin_path, deleted_at) "
                 "VALUES ('/photos', 'b.jpg', 'b.jpg', ?, 'bin/b.jpg', 1)", (binned,))
    conn.execute("INSERT INTO pending_uploads(storage_key, filename, root, uploaded_at, record) "
                 "VALUES ('k', 'c.jpg', '/photos', 1, ?)", (f'{{"thumb": "{waiting}"}}',))
    conn.commit()
    conn.close()
    kept = _thumbs(state, live) + _thumbs(state, binned) + _thumbs(state, waiting)
    kept += _thumbs(state, fresh, age=0)
    removed = _thumbs(state, orphan)
    stranger = state / "thumbs" / "notes.txt"
    stranger.write_text("not a thumbnail")

    cfg = SimpleNamespace(thumbs_dir=state / "thumbs", db_path=state / "index.db")
    count, reclaimed = db_maintenance.prune_orphan_thumbnails(cfg)

    assert count == 2 and reclaimed == 20
    assert not any(path.exists() for path in removed)
    assert all(path.exists() for path in kept)
    assert stranger.exists()
