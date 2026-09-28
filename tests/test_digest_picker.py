"""Choosing the one photograph from a day that is worth sending.

"On this day" finds the candidates; this decides which of them a person
should actually be shown, and that is the whole difficulty. A day in a real
library offers a couple of hundred photographs across twenty years, and the
largest trips run to two thousand frames. A digest that sends a blurred burst
frame, a screenshot, or the ninth near-identical shot of the same moment
teaches the household to ignore it — once, permanently.

So most of what follows is about refusing. Refusing is not scoring zero: a
screenshot is not a weak memory, it is not a memory, and no amount of
sharpness should let it through.
"""

from __future__ import annotations

import json
import time

import pytest

from ninaivu.storage import db

ROOT = "/library"
THIS_YEAR = int(time.strftime("%Y"))


@pytest.fixture()
def conn(tmp_path):
    return db.init_db(tmp_path / "index.db")


def add(conn, *, rel_path, year, month=7, day=4, faces=0, sharpness=500.0,
        brightness=0.5, quality=(), kind="picture", thumb="ab/cdef",
        visibility=1, nsfw=0, trashed=0, dup_group=None,
        width=4000, height=3000):
    """One photograph in the index, with the signals the picker reads."""
    cursor = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, date_key, "
        "  captured_at, thumb, visibility, nsfw, trashed, sharpness, "
        "  brightness, quality, dup_group, width, height, indexed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (ROOT, rel_path, rel_path.rsplit("/", 1)[-1], kind,
         f"{year:04d}-{month:02d}-{day:02d}", 0, thumb, visibility, nsfw,
         trashed, sharpness, brightness, json.dumps(list(quality)),
         dup_group, width, height))
    asset_id = cursor.lastrowid
    for n in range(faces):
        conn.execute(
            "INSERT INTO faces(asset_id, cluster_key, bbox, embedding) "
            "VALUES(?,?,?,?)", (asset_id, f"c{n}", "[]", b""))
    conn.commit()
    return asset_id


def picked(conn, **kwargs):
    chosen = db.pick_for_the_day(conn, [ROOT], month=7, day=4, **kwargs)
    return chosen["filename"] if chosen else None


def pool(conn, **kwargs):
    return [i["filename"] for i in
            db.candidates_for_the_day(conn, [ROOT], month=7, day=4, **kwargs)]


# -- what it refuses --------------------------------------------------------

def test_a_day_with_nothing_worth_sending_sends_nothing(conn):
    """None is a real answer, and the caller has to respect it.

    Saying nothing on a thin day is what keeps people reading the ones that
    do arrive.
    """
    assert db.pick_for_the_day(conn, [ROOT], month=7, day=4) is None


def test_a_screenshot_is_not_a_weak_memory(conn):
    add(conn, rel_path="shot.png", year=2010, quality=["screenshot"],
        faces=2, sharpness=3000.0)
    assert picked(conn) is None


def test_a_document_is_refused_however_sharp(conn):
    add(conn, rel_path="scan.jpg", year=2010, quality=["document"],
        sharpness=3000.0, faces=1)
    assert picked(conn) is None


def test_a_blurred_photograph_is_refused(conn):
    add(conn, rel_path="blur.jpg", year=2010, faces=3,
        sharpness=db.PICK_MIN_SHARPNESS - 1)
    assert picked(conn) is None


@pytest.mark.parametrize("brightness", [0.02, 0.99])
def test_a_lens_cap_and_a_window_are_both_refused(conn, brightness):
    """Real photographs, neither of them a memory."""
    add(conn, rel_path="edge.jpg", year=2010, faces=2, brightness=brightness)
    assert picked(conn) is None


def test_a_video_is_refused_because_a_message_cannot_hold_one(conn):
    add(conn, rel_path="clip.mp4", year=2010, kind="video", faces=4)
    assert picked(conn) is None


def test_something_with_no_thumbnail_has_nothing_to_send(conn):
    add(conn, rel_path="unindexed.jpg", year=2010, faces=3, thumb="")
    assert picked(conn) is None


def test_a_hidden_photograph_is_never_sent(conn):
    """Somebody put it out of sight. A digest must not undo that."""
    add(conn, rel_path="private.jpg", year=2010, faces=3, visibility=3)
    assert picked(conn, max_visibility=1) is None


def test_the_bin_is_not_a_source_of_memories(conn):
    add(conn, rel_path="deleted.jpg", year=2010, faces=3, trashed=1)
    assert picked(conn) is None


def test_this_year_is_not_a_memory(conn):
    """The point is the years you have forgotten."""
    add(conn, rel_path="recent.jpg", year=THIS_YEAR, faces=4)
    assert picked(conn) is None


# -- what it prefers --------------------------------------------------------

def test_a_photograph_with_people_beats_one_without(conn):
    add(conn, rel_path="hills.jpg", year=2010, faces=0, sharpness=3000.0)
    add(conn, rel_path="family.jpg", year=2010, faces=2, sharpness=200.0)
    assert picked(conn) == "family.jpg"


def test_a_group_beats_one_person(conn):
    add(conn, rel_path="alone.jpg", year=2010, faces=1)
    add(conn, rel_path="everyone.jpg", year=2010, faces=5)
    assert picked(conn) == "everyone.jpg"


def test_the_older_of_two_equals_wins(conn):
    """Last year is a photograph you remember taking."""
    add(conn, rel_path="lastyear.jpg", year=THIS_YEAR - 1, faces=3)
    add(conn, rel_path="longago.jpg", year=THIS_YEAR - 20, faces=3)
    assert picked(conn) == "longago.jpg"


def test_age_does_not_beat_having_people_in_it(conn):
    """Old and empty is still empty."""
    add(conn, rel_path="oldhills.jpg", year=1995, faces=0)
    add(conn, rel_path="newfamily.jpg", year=THIS_YEAR - 2, faces=4)
    assert picked(conn) == "newfamily.jpg"


def test_one_frame_from_a_burst_not_nine(conn):
    """The same moment twice wastes the only two lines anybody reads."""
    for n in range(9):
        add(conn, rel_path=f"burst{n}.jpg", year=2010, faces=3,
            dup_group="same-moment", sharpness=500.0 + n)
    add(conn, rel_path="different.jpg", year=2010, faces=3, dup_group=None)
    assert len(pool(conn)) == 2


def test_ungrouped_photographs_are_all_kept(conn):
    """No duplicate group means nothing is known, not that they are the same."""
    for n in range(4):
        add(conn, rel_path=f"loose{n}.jpg", year=2010, faces=2, dup_group=None)
    assert len(pool(conn)) == 4


# -- the shape of the answer ------------------------------------------------

def test_the_pick_carries_what_a_message_needs(conn):
    add(conn, rel_path="2010/summer/beach.jpg", year=2010, month=7, day=4,
        faces=3)
    chosen = db.pick_for_the_day(conn, [ROOT], month=7, day=4)
    assert chosen["year"] == "2010"
    assert chosen["face_count"] == 3
    assert chosen["thumb"]                      # something to embed
    assert chosen["pick_score"] > 0


def test_the_candidates_are_best_first(conn):
    add(conn, rel_path="weak.jpg", year=THIS_YEAR - 1, faces=0)
    add(conn, rel_path="good.jpg", year=THIS_YEAR - 1, faces=2)
    add(conn, rel_path="best.jpg", year=THIS_YEAR - 20, faces=5)
    assert pool(conn) == ["best.jpg", "good.jpg", "weak.jpg"]


def test_another_day_is_not_this_day(conn):
    add(conn, rel_path="july.jpg", year=2010, month=7, day=4, faces=3)
    add(conn, rel_path="august.jpg", year=2010, month=8, day=4, faces=3)
    assert pool(conn) == ["july.jpg"]


# -- the scale that caught me out -------------------------------------------

def test_brightness_is_read_on_the_scale_it_is_stored_on(conn):
    """A mean on 0–1, not 0–255 — see `media.image_stats`, which divides.

    Getting this wrong does not give a wrong answer, it gives an empty one:
    every photograph in a real library sits inside 0.1 and 0.97, so a 0–255
    range refuses all of them. Found against a library of 196,174 items,
    where the first version of this picked nothing on any day of the year.
    """
    assert db.PICK_BRIGHTNESS == (0.16, 0.86)
    add(conn, rel_path="ordinary.jpg", year=2010, faces=2, brightness=0.45)
    assert picked(conn) == "ordinary.jpg"
