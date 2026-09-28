"""A library that moves takes the archive's record of it along.

`reroot` rewrote nine tables in `index.db` and left `archive.db` alone — a
second database, beside the first, in which the library is the *destination*
every completed copy was written to. After a move it still named the old folder
in every row, so:

* 9,800 files recorded as verified pointed at a path that did not exist, and
  the archive's own answer to "has this already been copied?" was about a place
  nobody could look;
* the next run's leftover-file sweep would have searched `job_folders` that
  were not there, which is the sweep that exists to avoid listing a whole
  archive on a USB disk;
* the console offered the old folder as the destination to use again.

The other half of the guarantee is what must *not* move. Everything else
`archive.db` records is on the source disks somebody copies *from* — different
hardware, nothing to do with where the library lives. Rewriting `source_path`
would tell the archive it had already copied files from a disk it has never
seen, and those files would never be copied.
"""

from __future__ import annotations

import sqlite3

import pytest

from ninaivu.storage import reroot


OLD = r"E:\MasterArchive"
NEW = r"F:\MasterArchive"


@pytest.fixture()
def archive(tmp_path):
    """An archive.db shaped like the real one, with a job already done."""
    path = tmp_path / "archive.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT UNIQUE, destination_path TEXT, status TEXT);
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sources TEXT, destination TEXT, state TEXT);
        CREATE TABLE job_folders (job_id INTEGER, folder TEXT);
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.executemany(
        "INSERT INTO files(source_path, destination_path, status) VALUES(?,?,?)",
        [(r"D:\Phone\IMG_1.jpg", OLD + r"\2019\IMG_1.jpg", "verified"),
         (r"D:\Phone\IMG_2.jpg", OLD + r"\2019\IMG_2.jpg", "verified"),
         (r"D:\Phone\clip.mp4", None, "pending")])
    conn.execute("INSERT INTO jobs(sources, destination, state) VALUES(?,?,?)",
                 (r"D:\Phone", OLD, "finished"))
    conn.execute("INSERT INTO job_folders VALUES(1, ?)", (OLD + r"\2019",))
    conn.execute("INSERT INTO config VALUES('last_destination', ?)", (OLD,))
    conn.execute("INSERT INTO config VALUES('last_sources', ?)", (r"D:\Phone",))
    conn.commit()
    conn.close()
    return path


def rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute(sql)]
    finally:
        conn.close()


# -- what moves -------------------------------------------------------------

def test_a_verified_file_points_at_where_the_library_is_now(archive):
    reroot.rewrite_archive(archive, OLD, NEW, dry_run=False)
    assert rows(archive, "SELECT destination_path FROM files WHERE status='verified'") == [
        NEW + r"\2019\IMG_1.jpg", NEW + r"\2019\IMG_2.jpg"]


def test_the_job_destination_moves(archive):
    """The root itself, not a path inside it — a separate case, and the one
    that decides where the next run writes."""
    reroot.rewrite_archive(archive, OLD, NEW, dry_run=False)
    assert rows(archive, "SELECT destination FROM jobs") == [NEW]


def test_the_folders_a_run_touched_move(archive):
    """This is the list that lets a resume look in a handful of folders
    instead of listing a whole archive on a USB disk."""
    reroot.rewrite_archive(archive, OLD, NEW, dry_run=False)
    assert rows(archive, "SELECT folder FROM job_folders") == [NEW + r"\2019"]


def test_the_console_offers_the_new_folder(archive):
    reroot.rewrite_archive(archive, OLD, NEW, dry_run=False)
    assert rows(archive, "SELECT value FROM config WHERE key='last_destination'") == [NEW]


def test_it_says_what_it_changed(archive):
    counts = reroot.rewrite_archive(archive, OLD, NEW, dry_run=False)
    assert counts == {
        "files.destination_path": 2,
        "jobs.destination": 1,
        "job_folders.folder": 1,
        "config.last_destination": 1,
    }


# -- what must not move -----------------------------------------------------

def test_the_source_disk_is_left_alone(archive):
    """The whole other half. These files are on another disk; saying they came
    from the library would mean never copying them."""
    reroot.rewrite_archive(archive, OLD, NEW, dry_run=False)
    assert rows(archive, "SELECT source_path FROM files ORDER BY id") == [
        r"D:\Phone\IMG_1.jpg", r"D:\Phone\IMG_2.jpg", r"D:\Phone\clip.mp4"]
    assert rows(archive, "SELECT sources FROM jobs") == [r"D:\Phone"]
    assert rows(archive, "SELECT value FROM config WHERE key='last_sources'") == [r"D:\Phone"]


def test_a_file_not_copied_yet_keeps_its_empty_destination(archive):
    reroot.rewrite_archive(archive, OLD, NEW, dry_run=False)
    assert rows(archive, "SELECT destination_path FROM files WHERE status='pending'") == [None]


def test_a_sibling_folder_with_the_same_beginning_is_not_swept_up(archive):
    """`E:\\MasterArchive` must not match `E:\\MasterArchive-old`. A prefix
    match without the separator would have moved somebody else's folder."""
    conn = sqlite3.connect(archive)
    conn.execute("INSERT INTO job_folders VALUES(2, ?)", (OLD + r"-old\2019",))
    conn.commit()
    conn.close()

    reroot.rewrite_archive(archive, OLD, NEW, dry_run=False)
    assert rows(archive, "SELECT folder FROM job_folders ORDER BY job_id") == [
        NEW + r"\2019", OLD + r"-old\2019"]


# -- and the shape of the thing ---------------------------------------------

def test_a_dry_run_changes_nothing(archive):
    before = rows(archive, "SELECT destination_path FROM files")
    assert reroot.rewrite_archive(archive, OLD, NEW, dry_run=True) == {}
    assert rows(archive, "SELECT destination_path FROM files") == before


def test_no_archive_is_not_an_error(tmp_path):
    """Plenty of libraries have never run the archive engine at all."""
    assert reroot.rewrite_archive(tmp_path / "nothing.db", OLD, NEW,
                                  dry_run=False) == {}


def test_an_archive_without_the_newer_tables_still_moves(tmp_path):
    """An old archive.db predates `job_folders`. Missing one table must not
    stop the others being put right."""
    path = tmp_path / "archive.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE files (destination_path TEXT)")
    conn.execute("INSERT INTO files VALUES(?)", (OLD + r"\a.jpg",))
    conn.commit()
    conn.close()

    counts = reroot.rewrite_archive(path, OLD, NEW, dry_run=False)
    assert counts == {"files.destination_path": 1}
    assert rows(path, "SELECT destination_path FROM files") == [NEW + r"\a.jpg"]


def test_the_full_reroot_carries_the_archive_with_it(tmp_path, archive):
    """Not just the helper — the thing Migration actually calls."""
    index = tmp_path / "index.db"
    conn = sqlite3.connect(index)
    conn.executescript(
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, root TEXT, "
        "rel_path TEXT, thumb TEXT);")
    conn.execute("INSERT INTO assets VALUES(1, ?, 'a.jpg', 'ab/cd')", (OLD,))
    conn.commit()
    conn.close()

    report = reroot.reroot(tmp_path, OLD, NEW, dry_run=False)
    assert report.rows.get("jobs.destination") == 1
    assert rows(archive, "SELECT destination FROM jobs") == [NEW]


# -- from Windows to a Mac ---------------------------------------------------

MAC = "/Volumes/Family Red Drive/MasterArchive"


@pytest.mark.parametrize("value, expected", [
    (OLD, MAC),
    (OLD + "\\", MAC),
    (OLD + r"\2019\IMG_1.jpg", MAC + "/2019/IMG_1.jpg"),
    (r"E:\MasterArchive-old\2019", None),        # a sibling, not inside
    (r"D:\Phone\IMG_1.jpg", None),
    (None, None),
])
def test_a_path_is_respelled_for_the_computer_it_moved_to(value, expected):
    """The separator comes from each path, not from the computer running the
    move: on a Mac, os.sep is "/", and the archive's Windows rows never matched."""
    assert reroot.moved(value, OLD, MAC) == expected


def test_and_back_again():
    assert reroot.moved(MAC + "/2019/a.jpg", MAC, OLD) == OLD + r"\2019\a.jpg"


def test_an_archive_moved_to_a_mac_names_real_files(archive):
    """Found on the Mac this was written from: 15,569 copied files and 2,050
    folders still said E:\\MasterArchive after the library had moved, because
    only the rows that were exactly the old root had matched."""
    counts = reroot.rewrite_archive(archive, OLD, MAC, dry_run=False)
    assert rows(archive, "SELECT destination_path FROM files WHERE status='verified'") == [
        MAC + "/2019/IMG_1.jpg", MAC + "/2019/IMG_2.jpg"]
    assert rows(archive, "SELECT folder FROM job_folders") == [MAC + "/2019"]
    assert rows(archive, "SELECT source_path FROM files") == [
        r"D:\Phone\IMG_1.jpg", r"D:\Phone\IMG_2.jpg", r"D:\Phone\clip.mp4"]
    assert counts["files.destination_path"] == 2


def test_running_it_again_finds_nothing_left_to_move(archive):
    reroot.rewrite_archive(archive, OLD, MAC, dry_run=False)
    assert reroot.rewrite_archive(archive, OLD, MAC, dry_run=False) == {}
