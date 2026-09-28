"""A live photo is one thing the household took, and two files on disk.

Both halves were indexed as ordinary items, so the pair counted once among the
photographs and again among the videos. On the library this was found on that
was 5,674 of 19,636 "videos" — twenty-eight per cent of the count was
three-second fragments of photographs already counted as photographs. The same
clips were queued for the keyframe pass, which spends five decodes and five
passes through the model describing a moment of a still it has already read:
4,571 of them, about five hours of a fourteen-hour pass.

Underneath that was a second fault. The still's `live_video_path` was built by
trying `.mov` and then `.MOV` against `Path.exists()`, which on Windows answers
yes to either — so what was written down was the spelling that had been
guessed, not the one on disk. All 5,869 were wrong. NTFS does not care and the
clips played anyway; a case-sensitive filesystem would have 404ed every one,
and `date_edit`, which matches on that column, missed them everywhere.
"""


import pytest

from ninaivu.media import media
from ninaivu.media.scanner import Scanner, _clips_beside
from ninaivu.storage import db


@pytest.fixture()
def live_pair(scanned):
    """A still and its clip, indexed as the scanner leaves them."""
    cfg, conn, _ = scanned
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, is_live, "
        "live_video_path) VALUES (?,?,?,?,1,?)",
        (cfg.active_root, "2022/12/IMG_1.JPG", "IMG_1.JPG", "picture",
         "2022/12/IMG_1.mov"),                   # the guessed spelling
    )
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, duration) "
        "VALUES (?,?,?,?,3.0)",
        (cfg.active_root, "2022/12/IMG_1.MOV", "IMG_1.MOV", "video"),
    )
    conn.commit()
    return cfg, conn


def ids(conn, rel):
    return conn.execute("SELECT * FROM assets WHERE rel_path=?", (rel,)).fetchone()


# --- the join between the halves -------------------------------------------

def test_the_clip_is_found_despite_the_spelling(live_pair):
    cfg, conn = live_pair
    Scanner(cfg)._link_live_photos(conn, cfg.active_root)
    clip = ids(conn, "2022/12/IMG_1.MOV")
    assert clip["live_clip"] == 1


def test_the_path_is_repaired_to_the_one_on_disk(live_pair):
    """SQLite compares text exactly even where the filesystem does not, so a
    path that only works by the filesystem's good manners is a bug waiting for
    the first case-sensitive disk — or for any query that joins on it."""
    cfg, conn = live_pair
    Scanner(cfg)._link_live_photos(conn, cfg.active_root)
    still = ids(conn, "2022/12/IMG_1.JPG")
    assert still["live_video_path"] == "2022/12/IMG_1.MOV"


def test_an_ordinary_video_is_left_alone(live_pair):
    cfg, conn = live_pair
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, duration) "
        "VALUES (?,?,?,?,90.0)",
        (cfg.active_root, "2022/12/VID_2.mp4", "VID_2.mp4", "video"),
    )
    conn.commit()
    Scanner(cfg)._link_live_photos(conn, cfg.active_root)
    assert ids(conn, "2022/12/VID_2.mp4")["live_clip"] == 0


def test_a_clip_whose_still_has_gone_becomes_a_video_again(live_pair):
    """Written as a difference rather than a one-way mark, so a clip does not
    stay hidden from the count for ever because of a still that was deleted."""
    cfg, conn = live_pair
    scanner = Scanner(cfg)
    scanner._link_live_photos(conn, cfg.active_root)
    assert ids(conn, "2022/12/IMG_1.MOV")["live_clip"] == 1

    conn.execute("DELETE FROM assets WHERE rel_path='2022/12/IMG_1.JPG'")
    conn.commit()
    scanner._link_live_photos(conn, cfg.active_root)
    assert ids(conn, "2022/12/IMG_1.MOV")["live_clip"] == 0


def test_running_it_twice_changes_nothing(live_pair):
    cfg, conn = live_pair
    scanner = Scanner(cfg)
    scanner._link_live_photos(conn, cfg.active_root)
    before = dict(ids(conn, "2022/12/IMG_1.JPG"))
    scanner._link_live_photos(conn, cfg.active_root)
    assert dict(ids(conn, "2022/12/IMG_1.JPG")) == before


# --- reading the folder rather than guessing at it -------------------------

def test_the_real_spelling_comes_from_the_folder(tmp_path):
    _clips_beside.cache_clear()
    (tmp_path / "IMG_9.MOV").write_bytes(b"clip")
    (tmp_path / "IMG_9.JPG").write_bytes(b"still")
    assert _clips_beside(str(tmp_path))["img_9.mov"] == "IMG_9.MOV"


def test_a_folder_with_no_clips_in_it_is_not_an_error(tmp_path):
    _clips_beside.cache_clear()
    assert _clips_beside(str(tmp_path)) == {}


def test_a_folder_that_cannot_be_read_is_not_an_error():
    _clips_beside.cache_clear()
    assert _clips_beside("/no/such/folder/anywhere") == {}


# --- what the household is shown -------------------------------------------

def test_the_pair_counts_as_one_photograph(live_pair):
    cfg, conn = live_pair
    Scanner(cfg)._link_live_photos(conn, cfg.active_root)
    stats = db.library_stats(conn, [cfg.active_root], max_visibility=2)
    assert stats["live"] == 1
    assert stats["videos"] == 0, "the clip is not a video of its own"


def test_the_live_count_is_not_added_to_the_totals(live_pair):
    """Each one is already among the pictures. Adding it again would make the
    parts add up to more than the library holds."""
    cfg, conn = live_pair
    Scanner(cfg)._link_live_photos(conn, cfg.active_root)
    stats = db.library_stats(conn, [cfg.active_root], max_visibility=2)
    assert stats["pictures"] + stats["videos"] + stats["audio"] <= stats["count"]


def test_the_videos_view_does_not_list_the_clip(live_pair):
    """Otherwise the view disagrees with the count printed beside it."""
    cfg, conn = live_pair
    Scanner(cfg)._link_live_photos(conn, cfg.active_root)
    rows, _ = db.query_assets(conn, [cfg.active_root], kinds=["video"],
                              max_visibility=2)
    assert not [r for r in rows if r["rel_path"] == "2022/12/IMG_1.MOV"]


def test_asking_for_both_kinds_still_reaches_everything(live_pair):
    """A household browsing photos and videos together is not asking to have
    anything hidden from them."""
    cfg, conn = live_pair
    Scanner(cfg)._link_live_photos(conn, cfg.active_root)
    rows, _ = db.query_assets(conn, [cfg.active_root],
                              kinds=["picture", "video"], max_visibility=2)
    assert [r for r in rows if r["rel_path"] == "2022/12/IMG_1.MOV"]


# --- and the hours it saves -------------------------------------------------

def test_the_clip_is_not_described_by_its_keyframes(live_pair, monkeypatch):
    """Three seconds of a photograph the tagging pass has already read."""
    cfg, conn = live_pair
    cfg.ai_enabled = True
    Scanner(cfg)._link_live_photos(conn, cfg.active_root)

    scanner = Scanner(cfg)
    seen = []
    scanner.ai = type("E", (), {"analyse": lambda s, p, **k: seen.append(p) or [],
                                "name": "stub", "model_id": "s", "semantic": False})()
    monkeypatch.setattr(media, "extract_video_frame", lambda *a, **k: None)
    scanner._tag_video_keyframes(conn, cfg.active_root)

    assert seen == []
    clip = ids(conn, "2022/12/IMG_1.MOV")
    assert clip["keyframe_version"] == 0, "not described, and not written off"


# --- a new column has to reach databases that already exist -----------------

def test_the_column_reaches_a_database_that_predates_it(tmp_path):
    """The trap this fell into once already.

    `_migrate` only runs when the stored schema version is behind, so a column
    added after a version shipped never reaches the databases that need it —
    theirs already claim to be current. `LATE_COLUMNS` is the half that runs on
    every start, and a column added to the `CREATE TABLE` and nowhere else
    looks completely fine on a fresh install and does nothing on a real one.
    Found by running the pass against a copy of a real library, which is the
    only place the difference shows.
    """
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    assert "live_clip" in db.columns(conn, "assets")

    # Wind it back to how an older install looks: the column gone, the version
    # still claiming to be current.
    conn.execute("ALTER TABLE assets DROP COLUMN live_clip")
    db.set_meta(conn, "schema_version", str(db.SCHEMA_VERSION))
    conn.commit()
    assert "live_clip" not in db.columns(conn, "assets")

    # What start-up does, and the only thing that can put it back.
    db.heal_schema(conn)
    assert "live_clip" in db.columns(conn, "assets")


@pytest.mark.parametrize("column", ["live_clip", "is_live", "live_video_path"])
def test_every_live_column_is_registered_as_a_late_one(column):
    assert column in db.LATE_COLUMNS["assets"], (
        f"{column} is in the CREATE TABLE but not in LATE_COLUMNS, so a "
        f"database made before it will never get it")
