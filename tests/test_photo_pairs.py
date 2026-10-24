"""One shot, two files: RAW+JPEG pairs, and live photos filed apart.

A camera set to RAW+JPEG writes IMG_0001.CR2 and IMG_0001.JPG for one press of
the shutter, and both were indexed as photographs: every shot was in the grid
twice. Now the JPEG is shown and the RAW is kept beside it, to download.

A live photo's still and clip are paired by name within a folder. A index
filed by date splits many of them, because an iPhone dates the clip in UTC
and the still in local time: an evening's clips went into the next day's
folder and sat in the grid as three-second videos of their own. Those are
found again by name and by being the same moment, give or take a time zone.
"""

from __future__ import annotations

import os

import pytest

from ninaivu.archive import scanner as archive_scanner
from ninaivu.media.scanner import Scanner
from ninaivu.storage import db
from conftest import ADMIN, login

#: 2022-12-20 19:30:00 UTC, near enough; only the differences matter here.
STILL_AT = 1671564600.0


def add(conn, root, rel, kind, **extra):
    name = os.path.basename(rel)
    if "captured_at" in extra:
        # A time the camera wrote, unless a test says otherwise.
        extra.setdefault("date_source", "exif")
    values = {"root": root, "rel_path": rel, "filename": name,
              "folder": os.path.dirname(rel), "kind": kind,
              "ext": os.path.splitext(name)[1].lstrip(".").lower(), **extra}
    cols = ", ".join(values)
    conn.execute(f"INSERT INTO assets({cols}) VALUES ({', '.join('?' * len(values))})",
                 tuple(values.values()))
    conn.commit()
    return conn.execute("SELECT id FROM assets WHERE rel_path=?", (rel,)).fetchone()["id"]


def row(conn, rel):
    return conn.execute("SELECT * FROM assets WHERE rel_path=?", (rel,)).fetchone()


def listed(conn, root):
    rows, _ = db.query_assets(conn, [root], max_visibility=2, limit=100)
    return {r["rel_path"] for r in rows}


@pytest.fixture()
def index(scanned):
    cfg, conn, _ = scanned
    conn.execute("DELETE FROM assets")
    conn.commit()
    return cfg, conn, cfg.active_root


# --- RAW + JPEG --------------------------------------------------------------

def test_a_raw_and_its_jpeg_are_one_photograph(index):
    cfg, conn, root = index
    add(conn, root, "2023/05/01/IMG_0001.JPG", "picture")
    add(conn, root, "2023/05/01/IMG_0001.CR2", "picture")
    Scanner(cfg)._link_raw_pairs(conn, root)

    assert row(conn, "2023/05/01/IMG_0001.JPG")["raw_path"] == "2023/05/01/IMG_0001.CR2"
    assert row(conn, "2023/05/01/IMG_0001.CR2")["raw_twin"] == 1
    assert listed(conn, root) == {"2023/05/01/IMG_0001.JPG"}
    stats = db.library_stats(conn, [root], max_visibility=2)
    assert stats["count"] == 1 and stats["pictures"] == 1


def test_a_raw_on_its_own_is_an_ordinary_photograph(index):
    cfg, conn, root = index
    add(conn, root, "2023/05/01/IMG_0002.NEF", "picture")
    add(conn, root, "2023/05/02/IMG_0001.JPG", "picture")   # another day's
    Scanner(cfg)._link_raw_pairs(conn, root)
    assert row(conn, "2023/05/01/IMG_0002.NEF")["raw_twin"] == 0
    assert len(listed(conn, root)) == 2


def test_the_raw_goes_wherever_the_jpeg_goes(index):
    cfg, conn, root = index
    jpeg = add(conn, root, "a/IMG_7.jpg", "picture")
    raw = add(conn, root, "a/IMG_7.arw", "picture")
    Scanner(cfg)._link_raw_pairs(conn, root)
    assert db.with_companions(conn, [jpeg]) == [jpeg, raw]


def test_a_raw_whose_jpeg_has_gone_is_shown_again(index):
    cfg, conn, root = index
    add(conn, root, "a/IMG_8.JPG", "picture")
    add(conn, root, "a/IMG_8.CR3", "picture")
    scanner = Scanner(cfg)
    scanner._link_raw_pairs(conn, root)
    conn.execute("DELETE FROM assets WHERE rel_path='a/IMG_8.JPG'")
    conn.commit()
    scanner._link_raw_pairs(conn, root)
    assert row(conn, "a/IMG_8.CR3")["raw_twin"] == 0
    assert listed(conn, root) == {"a/IMG_8.CR3"}


def test_a_jpeg_whose_raw_has_gone_forgets_it(index):
    cfg, conn, root = index
    add(conn, root, "a/IMG_9.JPG", "picture")
    add(conn, root, "a/IMG_9.CR2", "picture")
    scanner = Scanner(cfg)
    scanner._link_raw_pairs(conn, root)
    conn.execute("UPDATE assets SET trashed=1 WHERE rel_path='a/IMG_9.CR2'")
    conn.commit()
    scanner._link_raw_pairs(conn, root)
    assert row(conn, "a/IMG_9.JPG")["raw_path"] is None


def test_a_raw_binned_with_its_jpeg_comes_back_with_it_after_a_scan(client, people, index):
    """Binned together through the API, a scan in between released the pair
    (a binned row counted as gone), and taking the JPEG out again left its
    RAW in the bin, unpaired, for good."""
    cfg, conn, root = index
    jpeg = add(conn, root, "c/IMG_4.JPG", "picture")
    raw = add(conn, root, "c/IMG_4.CR2", "picture")
    scanner = Scanner(cfg)
    scanner._link_raw_pairs(conn, root)
    login(client, *ADMIN)
    assert client.post(f"/api/asset/{jpeg}", json={"trashed": True}).status_code == 200
    assert row(conn, "c/IMG_4.CR2")["trashed"] == 1
    scanner._link_raw_pairs(conn, root)
    assert client.post(f"/api/asset/{jpeg}", json={"trashed": False}).status_code == 200
    back = row(conn, "c/IMG_4.CR2")
    assert back["trashed"] == 0 and back["raw_twin"] == 1
    assert row(conn, "c/IMG_4.JPG")["raw_path"] == "c/IMG_4.CR2"
    assert db.with_companions(conn, [jpeg]) == [jpeg, raw]


def test_a_clip_binned_with_its_still_comes_back_with_it_after_a_scan(client, people, index):
    cfg, conn, root = index
    still = add(conn, root, "d/IMG_5.HEIC", "picture", is_live=1,
                live_video_path="d/IMG_5.MOV")
    clip = add(conn, root, "d/IMG_5.MOV", "video", duration=2.5)
    scanner = Scanner(cfg)
    scanner._link_live_photos(conn, root)
    assert row(conn, "d/IMG_5.MOV")["live_clip"] == 1
    login(client, *ADMIN)
    client.post(f"/api/asset/{still}", json={"trashed": True})
    assert row(conn, "d/IMG_5.MOV")["trashed"] == 1
    scanner._link_live_photos(conn, root)
    scanner._link_live_photos(conn, root)
    client.post(f"/api/asset/{still}", json={"trashed": False})
    back = row(conn, "d/IMG_5.MOV")
    assert back["trashed"] == 0 and back["live_clip"] == 1
    assert db.with_companions(conn, [still]) == [still, clip]


def test_the_raw_is_offered_for_download_from_the_jpeg(client, people, index):
    cfg, conn, root = index
    jpeg = add(conn, root, "b/IMG_3.JPG", "picture")
    raw = add(conn, root, "b/IMG_3.CR2", "picture", size=25_000_000)
    Scanner(cfg)._link_raw_pairs(conn, root)
    login(client, *ADMIN)
    item = client.get(f"/api/asset/{jpeg}").get_json()
    assert item["raw"]["id"] == raw
    assert item["raw"]["ext"] == "CR2"
    assert item["raw"]["download"] == f"/api/download/{raw}"


# --- live photos filed apart -------------------------------------------------

def test_a_clip_filed_under_the_next_day_finds_its_still(index):
    cfg, conn, root = index
    add(conn, root, "2022/12/20/IMG_4410.HEIC", "picture", captured_at=STILL_AT)
    # The clip's date in UTC: five and a half hours on, and a second.
    add(conn, root, "2022/12/21/IMG_4410.MOV", "video",
        captured_at=STILL_AT + 5.5 * 3600 + 1, duration=2.8)
    Scanner(cfg)._link_live_photos(conn, root)

    still = row(conn, "2022/12/20/IMG_4410.HEIC")
    assert still["is_live"] == 1
    assert still["live_video_path"] == "2022/12/21/IMG_4410.MOV"
    assert row(conn, "2022/12/21/IMG_4410.MOV")["live_clip"] == 1
    assert listed(conn, root) == {"2022/12/20/IMG_4410.HEIC"}


def test_two_phones_with_the_same_number_are_not_paired(index):
    """IMG_1234 comes round every ten thousand photographs, and a family has
    several phones. The same name an hour and seventeen minutes apart is
    not the same moment in any time zone."""
    cfg, conn, root = index
    add(conn, root, "2022/12/20/IMG_1234.JPG", "picture", captured_at=STILL_AT)
    add(conn, root, "2022/12/21/IMG_1234.MOV", "video",
        captured_at=STILL_AT + 3600 + 17 * 60, duration=2.5)
    Scanner(cfg)._link_live_photos(conn, root)
    assert row(conn, "2022/12/20/IMG_1234.JPG")["is_live"] == 0
    assert row(conn, "2022/12/21/IMG_1234.MOV")["live_clip"] == 0


def test_a_real_video_is_never_taken_for_a_clip(index):
    cfg, conn, root = index
    add(conn, root, "2022/12/20/IMG_5.JPG", "picture", captured_at=STILL_AT)
    add(conn, root, "2022/12/21/IMG_5.MOV", "video",
        captured_at=STILL_AT + 3600, duration=95.0)
    Scanner(cfg)._link_live_photos(conn, root)
    assert row(conn, "2022/12/21/IMG_5.MOV")["live_clip"] == 0


def test_a_still_takes_only_one_clip(index):
    cfg, conn, root = index
    add(conn, root, "2022/12/20/IMG_6.HEIC", "picture", captured_at=STILL_AT)
    add(conn, root, "2022/12/21/IMG_6.MOV", "video", captured_at=STILL_AT + 3600, duration=3)
    add(conn, root, "2022/12/19/IMG_6.MOV", "video", captured_at=STILL_AT, duration=3)
    Scanner(cfg)._link_live_photos(conn, root)
    clips = conn.execute("SELECT rel_path FROM assets WHERE live_clip=1").fetchall()
    assert [c["rel_path"] for c in clips] == ["2022/12/19/IMG_6.MOV"], "the closer one"


def test_a_still_whose_clip_has_gone_stops_promising_one(index):
    cfg, conn, root = index
    add(conn, root, "a/IMG_7.HEIC", "picture", is_live=1, live_video_path="a/IMG_7.MOV")
    Scanner(cfg)._link_live_photos(conn, root)
    still = row(conn, "a/IMG_7.HEIC")
    assert still["is_live"] == 0 and still["live_video_path"] is None


def test_the_grid_is_told_which_tiles_are_live_and_which_have_a_raw(client, people, index):
    cfg, conn, root = index
    add(conn, root, "2022/12/20/IMG_1.HEIC", "picture", captured_at=STILL_AT,
        date_key="2022-12-20")
    add(conn, root, "2022/12/20/IMG_1.MOV", "video", captured_at=STILL_AT,
        duration=2.0, date_key="2022-12-20")
    add(conn, root, "2022/12/20/IMG_2.JPG", "picture", date_key="2022-12-20")
    add(conn, root, "2022/12/20/IMG_2.CR2", "picture", date_key="2022-12-20")
    scanner = Scanner(cfg)
    conn.execute("UPDATE assets SET is_live=1, live_video_path='2022/12/20/IMG_1.MOV' "
                 "WHERE rel_path='2022/12/20/IMG_1.HEIC'")
    conn.commit()
    scanner._link_live_photos(conn, root)
    scanner._link_raw_pairs(conn, root)
    login(client, *ADMIN)
    data = client.get("/api/segments").get_json()
    items = [i for seg in data["segments"] for i in seg["items"]]
    assert len(items) == 2, "the clip and the RAW are not tiles of their own"
    flags = sorted(i[3] & (256 | 512) for i in items)
    assert flags == [256, 512]


# --- the archive keeps a clip with its still ---------------------------------

def test_the_archive_files_a_clip_with_its_still(tmp_path):
    source = tmp_path / "phone"
    source.mkdir()
    (source / "IMG_4410.HEIC").write_bytes(b"still")
    (source / "IMG_4410.MOV").write_bytes(b"clip")
    (source / "IMG_4411.MOV").write_bytes(b"a video")
    archive = archive_scanner.ArchiveJob.__new__(archive_scanner.ArchiveJob)
    archive._companion_cache = {}
    assert archive._still_of_clip(str(source / "IMG_4410.MOV"), "IMG_4410.MOV") \
        == str(source / "IMG_4410.HEIC")
    assert archive._still_of_clip(str(source / "IMG_4411.MOV"), "IMG_4411.MOV") is None
    assert archive._still_of_clip(str(source / "IMG_4410.HEIC"), "IMG_4410.HEIC") is None


# --- an existing library ---------------------------------------------------------

def test_an_existing_index_gets_the_columns_and_the_indexes(tmp_path):
    """A library indexed before this has neither column, and the gallery's
    indexes without them: the columns are added on start, before the indexes
    that carry them are made again."""
    path = tmp_path / "old.db"
    conn = db.init_db(path)
    for (index,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                 "AND (sql LIKE '%raw_twin%' OR sql LIKE '%raw_path%')").fetchall():
        conn.execute(f"DROP INDEX {index}")
    conn.execute("DROP TRIGGER IF EXISTS assets_generation_update")
    conn.execute("DROP TRIGGER IF EXISTS assets_scan_generation_update")
    conn.execute("DROP TRIGGER IF EXISTS stacks_loosen_on_change")
    conn.execute("ALTER TABLE assets DROP COLUMN raw_twin")
    conn.execute("ALTER TABLE assets DROP COLUMN raw_path")
    conn.commit()
    db.close_all()

    again = db.init_db(path)
    assert {"raw_twin", "raw_path"} <= db.columns(again, "assets")
    sql = again.execute("SELECT sql FROM sqlite_master WHERE name='idx_assets_filter'"
                        ).fetchone()[0]
    assert "raw_twin" in sql and "live_clip" in sql


def test_the_gallery_count_still_reads_only_the_index(index):
    cfg, conn, root = index
    sql = ("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM assets a WHERE a.root = ? "
           "AND a.trashed = 0 AND a.nsfw = 0 AND a.visibility <= 1 "
           "AND a.live_clip = 0 AND a.raw_twin = 0")
    plan = " ".join(str(tuple(r)) for r in conn.execute(sql, (root,)))
    assert "COVERING INDEX" in plan, plan
