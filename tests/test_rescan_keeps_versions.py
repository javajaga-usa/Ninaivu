"""A rescan that changes nothing leaves the thumbnails' identity alone.

`indexed_at` is the version in every thumbnail URL, and those URLs are cached
for a year. A full rescan rewrites every row, and stamping a new time on all of
them made the whole library a cache miss at once — for photographs whose pixels
had not changed. The stamp now moves only when something about the picture did.
"""

import time
from pathlib import Path

from ninaivu.media.scanner import Scanner


def versions(conn):
    return {r["rel_path"]: (r["indexed_at"], r["rotation"])
            for r in conn.execute("SELECT rel_path, indexed_at, rotation FROM assets")}


def test_a_full_rescan_keeps_every_version(scanned):
    cfg, conn, _ = scanned
    before = versions(conn)
    assert before, "the fixture library indexed nothing"

    time.sleep(0.01)
    Scanner(cfg)._run(Path(cfg.active_root), full=True)

    after = versions(conn)
    assert after == before, "a rescan moved a version without the picture changing"


def test_a_changed_file_gets_a_new_version(scanned):
    cfg, conn, _ = scanned
    before = versions(conn)
    rel, _ = next(iter(before.items()))
    target = Path(cfg.active_root) / rel

    # Same bytes, later clock: the signature is (mtime, size), and this moves it.
    later = time.time() + 60
    import os
    os.utime(target, (later, later))

    Scanner(cfg)._run(Path(cfg.active_root), full=True)

    after = versions(conn)
    assert after[rel] != before[rel], "an edited file kept a stale thumbnail URL"
    others = {k: v for k, v in after.items() if k != rel}
    assert others == {k: v for k, v in before.items() if k != rel}


def test_the_recipe_rides_in_the_version(scanned):
    """Changing how thumbnails are made rewrites every file on disk without
    touching a photograph, so the URL has to say so."""
    from ninaivu.media import media

    one = media.thumb_recipe((256, 640), "WEBP", 82)
    two = media.thumb_recipe((256, 1024), "WEBP", 82)
    assert one != two

    row = {"indexed_at": 1700000000, "rotation": 90}
    assert media.thumb_version(row, one) != media.thumb_version(row, two)
    assert media.thumb_version(row, one).startswith("1700000000r90")
    # No recipe, no suffix — the shape the older clients expect.
    assert media.thumb_version(row) == "1700000000r90"
