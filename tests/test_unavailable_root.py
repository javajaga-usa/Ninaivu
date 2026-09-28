"""A drive that is not plugged in is not a library that was emptied.

The index is the part of Ninaivu a rescan cannot rebuild — album membership,
visibility decisions, the names somebody put to ten face clusters, dates
corrected by hand. `delete_missing` already refused on the two soundest
signals: a walk that raised anything, and a walk that found nothing at all.

What was left was `PRUNE_CEILING = 0.5`, which permits removing up to half a
root and its thumbnails in one pass. On 196,174 items that is ~98,000 rows
and their thumbnails, gone, on the strength of a proportion. On a machine
whose drives have a history of dropping off the bus, that is a bet.

These pin the answer that replaces the bet: a root that is not reachable is
not a root whose files were deleted, and no proportion may be read as saying
otherwise.
"""

from __future__ import annotations

import pytest

from ninaivu.storage import db
from ninaivu.storage import roots as roots_kit


@pytest.fixture(autouse=True)
def fresh_answers():
    """The cache is process-wide; no test may inherit another's."""
    roots_kit.forget()
    yield
    roots_kit.forget()


def stock(conn, root, count=40):
    db.bulk_upsert(conn, [{
        "root": str(root), "rel_path": f"a/{i}.jpg", "filename": f"{i}.jpg",
        "folder": "a", "ext": "jpg", "kind": "picture", "size": 1000,
        "mtime": 0, "thumb": f"ab/{i}", "indexed_at": 0,
    } for i in range(count)])


# -- the guarantee ----------------------------------------------------------

def test_an_unplugged_drive_is_never_read_as_a_deletion(tmp_path):
    """The whole point. Not a percentage — an answer."""
    conn = db.init_db(tmp_path / "index.db")
    root = tmp_path / "lib"
    root.mkdir()
    stock(conn, root)
    root.rename(tmp_path / "lib-unplugged")          # what unplugging looks like

    with pytest.raises(db.PruneRefused) as refused:
        db.delete_missing(conn, str(root), [])
    assert "not connected" in str(refused.value)
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 40


def test_it_refuses_before_the_proportion_is_even_considered(tmp_path):
    """A single missing file on an absent drive is refused just the same.

    The ceiling asks "how much of this walk went missing?". That question
    should never be asked about a folder nobody can read.
    """
    conn = db.init_db(tmp_path / "index.db")
    root = tmp_path / "lib"
    root.mkdir()
    stock(conn, root, count=3)                        # under PRUNE_FLOOR
    root.rename(tmp_path / "away")

    with pytest.raises(db.PruneRefused) as refused:
        db.delete_missing(conn, str(root), ["a/0.jpg", "a/1.jpg"])
    assert "not connected" in str(refused.value)


def test_a_connected_drive_still_prunes_normally(tmp_path):
    """The guard must not become a reason nothing is ever tidied up."""
    conn = db.init_db(tmp_path / "index.db")
    root = tmp_path / "lib"
    root.mkdir()
    stock(conn, root, count=40)

    gone = db.delete_missing(conn, str(root), [f"a/{i}.jpg" for i in range(39)])
    assert gone == ["ab/39"]


def test_force_still_means_force(tmp_path):
    """For the caller who has established the files really are gone."""
    conn = db.init_db(tmp_path / "index.db")
    root = tmp_path / "lib"
    root.mkdir()
    stock(conn, root, count=5)
    root.rename(tmp_path / "away")

    assert len(db.delete_missing(conn, str(root), [], force=True)) == 5


# -- the answer itself ------------------------------------------------------

def test_a_folder_that_is_there_is_available(tmp_path):
    (tmp_path / "here").mkdir()
    assert roots_kit.available(tmp_path / "here") is True


def test_a_folder_that_is_not_is_not(tmp_path):
    assert roots_kit.available(tmp_path / "nowhere") is False


def test_a_path_that_raises_is_not_an_empty_library(monkeypatch, tmp_path):
    """A device mid-reset raises. "It errored" is not "they are gone"."""
    from pathlib import Path

    def explodes(self):
        raise OSError(21, "the device is not ready")

    monkeypatch.setattr(Path, "is_dir", explodes)
    assert roots_kit.available(tmp_path / "resetting") is False


def test_the_answer_is_believed_for_a_while(tmp_path):
    """Callers ask per file, and a dead drive can take seconds to answer."""
    root = tmp_path / "lib"
    root.mkdir()
    assert roots_kit.available(root, now=1000.0) is True
    root.rmdir()
    # Still believed: within the window, nothing goes back to the disk.
    assert roots_kit.available(root, now=1000.0 + 1) is True
    # And past it, the truth.
    assert roots_kit.available(root, now=1000.0 + roots_kit.BELIEVE_FOR + 1) is False


def test_forgetting_makes_the_next_ask_real(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    assert roots_kit.available(root) is True
    root.rmdir()
    roots_kit.forget(root)
    assert roots_kit.available(root) is False


def test_it_can_name_the_ones_that_are_away(tmp_path):
    (tmp_path / "here").mkdir()
    away = roots_kit.unavailable([tmp_path / "here", tmp_path / "gone"])
    assert away == [str(tmp_path / "gone")]


# -- and they stop vanishing from the gallery -------------------------------

def test_a_library_folder_that_is_away_is_still_one_of_yours(cfg, tmp_path):
    """With two roots and one unplugged, its photographs used to disappear
    from the gallery with nothing on screen to say why."""
    away = tmp_path / "second"
    cfg.roots = [cfg.active_root, str(away)]

    assert str(away) in cfg.roots            # still configured
    assert str(away) not in cfg.libraries    # not readable
    assert cfg.libraries_away == [str(away)]  # and named as such


# -- and it is said out loud ------------------------------------------------

def test_the_status_says_how_many_are_away(client, cfg, tmp_path):
    """`libraries_away` was worked out and then shown to nobody. A gallery
    that is quietly short looks exactly like photographs having gone missing.
    """
    away = tmp_path / "second"
    cfg.roots = [cfg.active_root, str(away)]

    body = client.get("/api/status?stats=0").get_json()
    assert body["libraries_away"] == 1


def test_an_admin_is_told_which_folder(as_admin, cfg, tmp_path):
    """Which folder is infrastructure detail, like `roots` beside it."""
    away = tmp_path / "second"
    cfg.roots = [cfg.active_root, str(away)]

    body = as_admin.get("/api/status?stats=0").get_json()
    assert body["libraries_away_paths"] == [str(away)]


def test_the_family_is_told_that_one_is_away_but_not_where(as_family, cfg, tmp_path):
    away = tmp_path / "second"
    cfg.roots = [cfg.active_root, str(away)]

    body = as_family.get("/api/status?stats=0").get_json()
    assert body["libraries_away"] == 1
    assert body["libraries_away_paths"] == []


def test_nothing_is_said_when_everything_is_plugged_in(client, cfg):
    body = client.get("/api/status?stats=0").get_json()
    assert body["libraries_away"] == 0
    assert body["libraries_away_paths"] == []
