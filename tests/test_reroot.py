"""Moving a library to another machine without redoing a day of work.

A first scan of a large library is most of a day, and all of it is reusable —
as long as the library lands at the same absolute path. It usually does not: a
drive that was `E:` on one machine comes up as `D:` on the next, because
Windows hands out letters in the order things are plugged in.

Then every thumbnail is orphaned, because a thumbnail's filename is a hash of
"<absolute root>|<relative path>". On the library this was written for that is
343,170 files and 8.1 GB — the expensive half of a first scan, thrown away over
a drive letter. Nothing about the pictures changed and no relative path
changed, so the new name of each one can be worked out from the old.
"""

import json

import pytest

from ninaivu.media.media import thumb_base
from ninaivu.server import auth
from ninaivu.storage import db

from tools import reroot_library as reroot


OLD = r"E:\MasterArchive"
NEW = r"D:\MasterArchive"


@pytest.fixture()
def library(tmp_path):
    r"""An index and a thumbnail tree, as a scan of E:\MasterArchive leaves."""
    state = tmp_path / "state"
    (state / "thumbs").mkdir(parents=True)
    conn = db.init_db(state / "index.db")
    auth.init_auth_schema(conn)          # the sign-in tables, for `users`

    rels = ["2019/07/IMG_1.JPG", "2019/07/IMG_2.JPG", "2022/12/VID_3.mp4"]
    for rel in rels:
        base = thumb_base(OLD, rel)
        conn.execute(
            "INSERT INTO assets(root, rel_path, filename, kind, thumb) "
            "VALUES (?,?,?,?,?)",
            (OLD, rel, rel.rpartition("/")[2],
             "video" if rel.endswith(".mp4") else "picture", base))
        shard, _, digest = base.rpartition("/")
        (state / "thumbs" / shard).mkdir(parents=True, exist_ok=True)
        for size in (256, 640):
            (state / "thumbs" / shard / f"{digest}_{size}.webp").write_bytes(
                f"{rel}@{size}".encode())
    conn.execute(
        "INSERT INTO occasions(root, key, title, started_at, ended_at, days, "
        "count) VALUES (?, '2019-07', 'Holiday', 0, 0, 1, 2)", (OLD,))
    conn.commit()
    (state / "config.json").write_text(
        json.dumps({"roots": [OLD], "active_root": OLD, "watch": True}),
        encoding="utf-8")
    return state, conn, rels


def run(state, *extra):
    return reroot.main(["--from", OLD, "--to", NEW,
                        "--state-dir", str(state), *extra])


# --- the thumbnails, which are the expensive part ---------------------------

def test_every_thumbnail_is_renamed_rather_than_regenerated(library):
    state, conn, rels = library
    run(state)
    for rel in rels:
        shard, _, digest = thumb_base(NEW, rel).rpartition("/")
        for size in (256, 640):
            moved = state / "thumbs" / shard / f"{digest}_{size}.webp"
            assert moved.is_file(), f"{rel} at {size} was left behind"


def test_the_pictures_inside_them_are_untouched(library):
    """A rename, not a re-encode. The bytes have to be the same bytes."""
    state, conn, rels = library
    run(state)
    shard, _, digest = thumb_base(NEW, rels[0]).rpartition("/")
    kept = state / "thumbs" / shard / f"{digest}_256.webp"
    assert kept.read_bytes() == f"{rels[0]}@256".encode()


def test_nothing_is_left_at_the_old_name(library):
    state, conn, rels = library
    run(state)
    for rel in rels:
        shard, _, digest = thumb_base(OLD, rel).rpartition("/")
        assert not (state / "thumbs" / shard / f"{digest}_256.webp").exists()


# --- and the index that points at them ---------------------------------------

def test_the_index_points_at_the_new_folder(library):
    state, conn, _ = library
    run(state)
    roots = [r[0] for r in conn.execute("SELECT DISTINCT root FROM assets")]
    assert roots == [NEW]


def test_every_row_that_names_a_folder_moves_with_it(library):
    """A table missed here does not fail loudly. It leaves rows pointing at a
    path that is not there, which reads as things quietly disappearing."""
    state, conn, _ = library
    run(state)
    assert conn.execute(
        "SELECT root FROM occasions").fetchone()[0] == NEW


def test_the_thumb_column_agrees_with_the_files_on_disk(library):
    state, conn, rels = library
    run(state)
    for rel in rels:
        stored = conn.execute("SELECT thumb FROM assets WHERE rel_path=?",
                              (rel,)).fetchone()[0]
        assert stored == thumb_base(NEW, rel)


def test_an_item_that_never_had_a_thumbnail_is_not_given_one(library):
    """A sound file, or a photograph too damaged to read, has no thumbnail.

    Every row was given the new name, so after a move each of them pointed at
    a picture that did not exist: the gallery drew a blank tile where it had
    drawn the sign for the item's kind — 12,861 of them on the library this
    was found on.
    """
    state, conn, rels = library
    conn.execute("INSERT INTO assets(root, rel_path, filename, kind) "
                 "VALUES (?, '2016/10/song.mp3', 'song.mp3', 'audio')", (OLD,))
    conn.commit()
    run(state)
    row = conn.execute("SELECT root, thumb FROM assets WHERE rel_path='2016/10/song.mp3'"
                       ).fetchone()
    assert row[0] == NEW, "it still moves with its library"
    assert row[1] is None


def test_the_console_remembers_the_new_folder(library):
    state, _, _ = library
    run(state)
    stored = json.loads((state / "config.json").read_text("utf-8"))
    assert stored["roots"] == [NEW]
    assert stored["active_root"] == NEW
    assert stored["watch"] is True, "it rewrote more than it was asked to"


def test_a_member_confined_to_a_folder_is_still_confined_to_it(library):
    state, conn, _ = library
    conn.execute("INSERT INTO users(username, display_name, role, library, "
                 "created_at) VALUES ('maya','Maya','family',?,0)",
                 (OLD + r"\2019",))
    conn.commit()
    run(state)
    assert conn.execute(
        "SELECT library FROM users WHERE username='maya'"
    ).fetchone()[0] == NEW + r"\2019"


def test_a_member_in_a_neighbouring_folder_stays_where_they_are(library):
    """"E:\\MasterArchive-old" starts with "E:\\MasterArchive" but is not inside
    it. Matched as a string prefix, it moved with the library and confined
    the member to a folder that does not exist."""
    state, conn, _ = library
    conn.execute("INSERT INTO users(username, display_name, role, library, "
                 "created_at) VALUES ('sam','Sam','family',?,0)",
                 (OLD + r"-old\2019",))
    conn.commit()
    run(state)
    assert conn.execute(
        "SELECT library FROM users WHERE username='sam'"
    ).fetchone()[0] == OLD + r"-old\2019"


# --- and the ways it can be asked to do the wrong thing ----------------------

def test_a_dry_run_changes_nothing(library):
    state, conn, rels = library
    before = json.loads((state / "config.json").read_text("utf-8"))
    run(state, "--dry-run")
    assert [r[0] for r in conn.execute(
        "SELECT DISTINCT root FROM assets")] == [OLD]
    assert json.loads((state / "config.json").read_text("utf-8")) == before
    shard, _, digest = thumb_base(OLD, rels[0]).rpartition("/")
    assert (state / "thumbs" / shard / f"{digest}_256.webp").is_file()


def test_an_interrupted_move_can_be_finished(library):
    """Power cut halfway through 343,000 renames. What must not happen is that
    the half already moved cannot be told from the half that has not."""
    state, conn, rels = library
    # As an interruption leaves it: one item's files already renamed, the
    # index still saying every one of them is under the old root.
    moved = rels[0]
    old_shard, _, old_digest = thumb_base(OLD, moved).rpartition("/")
    new_shard, _, new_digest = thumb_base(NEW, moved).rpartition("/")
    (state / "thumbs" / new_shard).mkdir(parents=True, exist_ok=True)
    for size in (256, 640):
        (state / "thumbs" / old_shard / f"{old_digest}_{size}.webp").replace(
            state / "thumbs" / new_shard / f"{new_digest}_{size}.webp")

    assert run(state) == 0

    for rel in rels:
        shard, _, digest = thumb_base(NEW, rel).rpartition("/")
        for size in (256, 640):
            assert (state / "thumbs" / shard /
                    f"{digest}_{size}.webp").is_file(), f"{rel} at {size}"
    assert [r[0] for r in conn.execute(
        "SELECT DISTINCT root FROM assets")] == [NEW]


def test_a_second_run_after_a_finished_one_is_refused(library):
    """There is nothing left under the old root, and saying so is better than
    renaming a set of files that have already been renamed."""
    state, _, _ = library
    run(state)
    with pytest.raises(SystemExit):
        run(state)


def test_a_folder_the_index_has_never_heard_of_is_refused(library):
    state, _, _ = library
    with pytest.raises(SystemExit) as raised:
        reroot.main(["--from", r"Z:\Nope", "--to", NEW,
                     "--state-dir", str(state)])
    assert "Nothing in the index" in str(raised.value)


def test_moving_somewhere_it_already_is_does_nothing(library):
    state, _, _ = library
    assert reroot.main(["--from", OLD, "--to", OLD,
                        "--state-dir", str(state)]) == 0


def test_the_same_folder_spelled_differently_is_still_the_same_folder(library):
    """Windows does not care about the case of a drive letter and neither
    should this: re-rooting a library onto itself would rename every thumbnail
    to the name it already has."""
    state, _, _ = library
    assert reroot.main(["--from", OLD, "--to", OLD.lower(),
                        "--state-dir", str(state)]) == 0


def test_a_drive_letter_typed_in_the_other_case_still_finds_the_library(library):
    """The case this exists for is a drive that comes up as a different
    letter, and somebody typing that letter in lower case means the library
    they have — but every UPDATE matches the string exactly, because SQLite
    does not share the filesystem's indifference."""
    state, conn, _ = library
    assert reroot.main(["--from", OLD.lower(), "--to", NEW,
                        "--state-dir", str(state)]) == 0
    assert [r[0] for r in conn.execute(
        "SELECT DISTINCT root FROM assets")] == [NEW]


def test_it_refuses_while_ninaivu_is_running(library):
    """It renames the files Ninaivu is serving and rewrites the index
    underneath it."""
    state, _, _ = library
    (state / "ninaivu-server.lock").write_text("1")
    with pytest.raises(SystemExit) as raised:
        run(state)
    assert "running" in str(raised.value)
