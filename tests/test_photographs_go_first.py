"""Photographs are sent before video.

A family library is mostly photographs by count and mostly video by size. On
this one: 1.6 TB still owed across 125,732 files, and the video is the large
majority of those bytes. Sent in the order they were queued, one 4 GB holiday
film holds up every photograph behind it — and the photographs are both the
irreplaceable half and the half that gets safe soonest.

So the queue works through photographs, then everything else, then video. That
is an order of *sending*, not of owing: these also pin that nothing is dropped,
nothing is skipped, and a video already part-way to Drive still finishes first.
"""

from __future__ import annotations

import sqlite3

import pytest

from ninaivu.cloud import store


@pytest.fixture()
def conn(tmp_path):
    db = sqlite3.connect(tmp_path / "cloud.db")
    db.row_factory = sqlite3.Row
    store.init_schema(db)
    yield db
    db.close()


def queue(conn, *names, root="/lib", when=None):
    """Queue files in the order given, each a second after the last."""
    for i, name in enumerate(names):
        conn.execute(
            "INSERT INTO cloud_uploads(root, rel_path, filename, size, state, "
            "queued_at, kind) VALUES(?,?,?,?,?,?,?)",
            (root, name, name, 1000, store.PENDING,
             (1000.0 if when is None else when) + i, store.kind_of_name(name)))
    conn.commit()


def sent(conn, **kwargs):
    return [row["rel_path"] for row in store.pending_batch(conn, **kwargs)]


# -- the order ---------------------------------------------------------------

def test_a_photograph_queued_last_still_goes_first(conn):
    queue(conn, "film.mp4", "clip.mov", "gran.jpg")
    assert sent(conn) == ["gran.jpg", "film.mp4", "clip.mov"]


def test_video_is_last_and_everything_else_is_in_between(conn):
    queue(conn, "film.mp4", "song.mp3", "gran.jpg", "notes.pdf")
    assert sent(conn) == ["gran.jpg", "song.mp3", "notes.pdf", "film.mp4"]


def test_within_a_kind_the_oldest_still_goes_first(conn):
    """The order things were asked for is honoured, one kind at a time."""
    queue(conn, "first.jpg", "second.jpg", "third.jpg")
    assert sent(conn) == ["first.jpg", "second.jpg", "third.jpg"]


def test_raw_files_are_photographs(conn):
    queue(conn, "film.mp4", "shot.cr2", "shot.dng")
    assert sent(conn) == ["shot.cr2", "shot.dng", "film.mp4"]


def test_a_batch_smaller_than_the_photographs_is_all_photographs(conn):
    queue(conn, "film.mp4", *[f"{i}.jpg" for i in range(5)])
    assert sent(conn, limit=3) == ["0.jpg", "1.jpg", "2.jpg"]


def test_a_queue_of_nothing_but_video_still_sends(conn):
    """Priority is not a reason for a run to sit idle."""
    queue(conn, "a.mp4", "b.mov")
    assert sent(conn) == ["a.mp4", "b.mov"]


# -- what must not change ---------------------------------------------------

def test_nothing_is_dropped(conn):
    queue(conn, "film.mp4", "gran.jpg", "song.mp3", "notes.pdf", "b.mkv")
    assert sorted(sent(conn)) == sorted(
        ["film.mp4", "gran.jpg", "song.mp3", "notes.pdf", "b.mkv"])


def test_an_upload_already_under_way_finishes_first(conn):
    """Its bytes have gone. Setting it aside to start a photograph spends
    them again — on a 4 GB film, that is the whole point of resuming."""
    queue(conn, "film.mp4", "gran.jpg")
    conn.execute("UPDATE cloud_uploads SET state=?, resume_url='https://x' "
                 "WHERE rel_path='film.mp4'", (store.UPLOADING,))
    conn.commit()
    assert sent(conn) == ["film.mp4", "gran.jpg"]


def test_an_interrupted_upload_with_nothing_to_resume_waits_its_turn(conn):
    """Nothing to carry on from means nothing to lose by waiting."""
    queue(conn, "film.mp4", "gran.jpg")
    conn.execute("UPDATE cloud_uploads SET state=? WHERE rel_path='film.mp4'",
                 (store.UPLOADING,))
    conn.commit()
    assert sent(conn) == ["gran.jpg", "film.mp4"]


def test_an_unplugged_drive_is_still_stepped_over(conn):
    queue(conn, "gran.jpg", "film.mp4")
    queue(conn, "away.jpg", root="/away")
    assert sent(conn, exclude_roots={"/away"}) == ["gran.jpg", "film.mp4"]


def test_no_row_is_offered_twice(conn):
    """Each tier is its own query; a row must not come back in two of them."""
    queue(conn, "gran.jpg")
    conn.execute("UPDATE cloud_uploads SET state=?, resume_url='https://x'",
                 (store.UPLOADING,))
    conn.commit()
    assert sent(conn) == ["gran.jpg"]


# -- the queue that was already there ---------------------------------------

def test_rows_queued_before_there_was_a_kind_are_classified(tmp_path):
    """156,000 rows predate the column. They must not all land in one tier."""
    db = sqlite3.connect(tmp_path / "old.db")
    db.row_factory = sqlite3.Row
    store.init_schema(db)
    db.execute("DROP INDEX idx_cloud_kind_queue")                # as it was
    db.execute("ALTER TABLE cloud_uploads DROP COLUMN kind")
    for i, name in enumerate(["film.mp4", "gran.jpg"]):
        db.execute("INSERT INTO cloud_uploads(root, rel_path, filename, size, "
                   "state, queued_at) VALUES(?,?,?,?,?,?)",
                   ("/lib", name, name, 1000, store.PENDING, 1000.0 + i))
    db.commit()

    store.init_schema(db)                                        # the upgrade
    assert sent(db) == ["gran.jpg", "film.mp4"]
    db.close()


def test_the_backfill_does_not_touch_the_disk(conn, monkeypatch):
    """Rows whose drive is elsewhere are classified from the name alone."""
    from ninaivu.server import config as config_module

    def opens_nothing(*_args, **_kwargs):
        raise AssertionError("the backfill read a file")

    monkeypatch.setattr(config_module, "looks_like_transport_stream",
                        opens_nothing)
    queue(conn, "recording.ts", "gran.jpg")
    conn.execute("UPDATE cloud_uploads SET kind=''")
    conn.commit()
    store._backfill_kinds(conn)
    assert sent(conn) == ["gran.jpg", "recording.ts"]


# -- and it can be seen on the page -----------------------------------------

def test_the_summary_says_what_is_waiting_of_each(conn):
    queue(conn, "a.jpg", "b.jpg", "film.mp4", "song.mp3")
    got = store.summary(conn)
    assert (got["waiting_pictures"], got["waiting_videos"],
            got["waiting_other"]) == (2, 1, 1)
    assert got["bytes_waiting_pictures"] == 2000
    assert got["bytes_waiting_videos"] == 1000


# -- the path the 156,000 actually came in by --------------------------------

def test_the_bulk_queueing_records_the_kind_too(conn):
    """`queue_missing` writes its own INSERT. It was the one that mattered —
    every row the real queue is made of comes in this way, and a row with no
    kind lands in the middle tier, which would have quietly undone all of it.
    """
    store.queue_missing(conn, [
        {"root": "/lib", "rel_path": "film.mp4", "size": 9, "mtime": 1.0},
        {"root": "/lib", "rel_path": "gran.jpg", "size": 9, "mtime": 2.0},
    ])
    assert sent(conn) == ["gran.jpg", "film.mp4"]


def test_it_believes_the_index_over_the_extension(conn):
    """The index settled the ambiguous ones by opening them. Where it has an
    answer, that answer is better than anything a path can say."""
    store.queue_missing(conn, [
        {"root": "/lib", "rel_path": "holiday.ts", "size": 9, "mtime": 1.0,
         "kind": "video"},
        {"root": "/lib", "rel_path": "gran.jpg", "size": 9, "mtime": 2.0,
         "kind": "picture"},
    ])
    assert sent(conn) == ["gran.jpg", "holiday.ts"]


def test_requeueing_a_row_gives_it_a_kind_as_well(conn):
    """A row queued before the column existed is put right by the next pass
    over it, whether or not the backfill has run."""
    queue(conn, "gran.jpg")
    conn.execute("UPDATE cloud_uploads SET kind='', state=?", (store.FAILED,))
    conn.commit()
    store.queue_missing(conn, [
        {"root": "/lib", "rel_path": "gran.jpg", "size": 2000, "mtime": 5.0},
    ])
    assert conn.execute(
        "SELECT kind FROM cloud_uploads").fetchone()["kind"] == store.PICTURE


# -- the backfill prefers the index's answer ---------------------------------

def test_the_backfill_asks_the_index_first(tmp_path):
    """`cloud_uploads` is a table in index.db, so `assets` is right there. The
    index opened the ambiguous files; a path cannot. Measured on the real
    queue, the two agree on 125,329 of 125,609 rows and the 280 they differ on
    are all this case.
    """
    db = sqlite3.connect(tmp_path / "index.db")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE assets (root TEXT, rel_path TEXT, kind TEXT)")
    db.execute("INSERT INTO assets VALUES ('/lib', 'holiday.ts', 'video')")
    store.init_schema(db)
    queue(db, "holiday.ts", "gran.jpg")
    db.execute("UPDATE cloud_uploads SET kind=''")
    db.commit()

    store._backfill_kinds(db)
    kinds = {row["rel_path"]: row["kind"]
             for row in db.execute("SELECT rel_path, kind FROM cloud_uploads")}
    assert kinds == {"holiday.ts": "video", "gran.jpg": store.PICTURE}
    db.close()


def test_a_file_the_index_never_saw_falls_back_to_its_name(tmp_path):
    db = sqlite3.connect(tmp_path / "index.db")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE assets (root TEXT, rel_path TEXT, kind TEXT)")
    store.init_schema(db)
    queue(db, "film.mp4", "gran.jpg")
    db.execute("UPDATE cloud_uploads SET kind=''")
    db.commit()

    store._backfill_kinds(db)
    assert sent(db) == ["gran.jpg", "film.mp4"]
    db.close()


def test_a_queue_on_its_own_still_works(conn):
    """This module is handed a connection. One without an index beside it is
    not a reason to fail — it is a reason to read the names."""
    assert store._the_index_is_here(conn) is False
    queue(conn, "film.mp4", "gran.jpg")
    conn.execute("UPDATE cloud_uploads SET kind=''")
    conn.commit()
    store._backfill_kinds(conn)
    assert sent(conn) == ["gran.jpg", "film.mp4"]
