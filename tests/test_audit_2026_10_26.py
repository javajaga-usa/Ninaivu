"""Twelfth audit (2026-10-26): what it found, kept from coming back.

H-01 a smart album of words or a tag is a search kept under a name, and a
family member saw in it what their search leaves out. M-01 the underwear
check's flags stayed when its line was raised or it was turned off. L-01 a
live photo's clip and a RAW's twin waited for the next scan to follow a
flagged photograph. L-02 a hidden screenshot was flagged and unflagged at
every catch-up.
"""
from __future__ import annotations

import pytest

from ninaivu.media import revealing
from ninaivu.storage import db, smart

from test_family_search_shield import (_ids, _judge, _picture_search, nearly,  # noqa: F401
                                       underwear)


# -- H-01 ---------------------------------------------------------------------

def _album(conn, rules, people) -> int:
    return smart.create(conn, "Beach", rules, created_by=people["admin"].id)


@pytest.mark.parametrize("rules", [{"text": "beach"}, {"tag": "beach"}])
def test_a_smart_album_of_words_leaves_out_what_search_does(
        nearly, people, as_family, as_admin, app, rules):  # noqa: F811
    _picture_search(app, nearly["cfg"])
    album = _album(nearly["conn"], rules, people)
    url = f"/api/segments?smart={album}"
    family = _ids(as_family.get(url))
    assert nearly["plain"] in family
    assert nearly["shown"] not in family and nearly["near"] not in family
    assert {nearly["shown"], nearly["near"], nearly["plain"]} <= _ids(as_admin.get(url))


def test_a_smart_albums_count_leaves_them_out_too(nearly, people, as_family, as_admin, app):  # noqa: F811
    _picture_search(app, nearly["cfg"])
    album = _album(nearly["conn"], {"tag": "beach"}, people)

    def count(client):
        listed = client.get("/api/smart-albums").get_json()["albums"]
        return next(a["n"] for a in listed if a["id"] == album)

    assert count(as_admin) == count(as_family) + 2


def test_a_smart_album_that_is_not_a_search_is_browsing(nearly, people, as_family):  # noqa: F811
    album = _album(nearly["conn"], {"kinds": ["picture"]}, people)
    family = _ids(as_family.get(f"/api/segments?smart={album}"))
    assert nearly["shown"] in family and nearly["near"] in family


def test_a_tag_search_is_answered_again_when_its_line_moves(nearly, as_family, app):  # noqa: F811
    """The kept layout of a tag search is tagged with the lines it was cut by."""
    _picture_search(app, nearly["cfg"])
    first = as_family.get("/api/segments?tag=beach")
    assert nearly["near"] not in _ids(first)
    nearly["cfg"].explicit_threshold = 0.02                 # half of it is 0.01 > 0.007
    again = as_family.get("/api/segments?tag=beach",
                          headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 200
    assert nearly["near"] in _ids(again)


# -- M-01 ---------------------------------------------------------------------

def _flag(conn, aid):
    return tuple(conn.execute("SELECT nsfw, nsfw_source FROM assets WHERE id=?",
                              (aid,)).fetchone())


def test_a_higher_line_takes_back_what_the_old_one_flagged(underwear):  # noqa: F811
    conn, cfg = underwear["conn"], underwear["cfg"]
    root = cfg.active_root
    _judge(cfg, conn, underwear["engine"], root)
    assert _flag(conn, underwear["bare"]) == (1, db.REVEALING_SOURCE)
    with db._write_lock:
        conn.execute("UPDATE assets SET revealing=0.03 WHERE id=?", (underwear["bare"],))
        conn.commit()
    cfg.underwear_threshold = 0.05
    _judge(cfg, conn, underwear["engine"], root)
    assert _flag(conn, underwear["bare"]) == (0, "auto")
    # And flagged again when the line comes back down.
    cfg.underwear_threshold = revealing.LINE
    _judge(cfg, conn, underwear["engine"], root)
    assert _flag(conn, underwear["bare"]) == (1, db.REVEALING_SOURCE)


def test_the_check_turned_off_takes_back_its_flags_only(underwear):  # noqa: F811
    conn, cfg = underwear["conn"], underwear["cfg"]
    root = cfg.active_root
    _judge(cfg, conn, underwear["engine"], root)
    with db._write_lock:
        # The content check's own flag, on another picture.
        conn.execute("UPDATE assets SET nsfw=1, nsfw_score=0.9 WHERE id=?", (underwear["swim"],))
        conn.commit()
    cfg.underwear_threshold = 0
    _judge(cfg, conn, underwear["engine"], root)
    assert _flag(conn, underwear["bare"]) == (0, "auto")
    assert _flag(conn, underwear["swim"]) == (1, "auto")


def test_one_the_content_check_would_flag_keeps_its_flag(underwear):  # noqa: F811
    conn, cfg = underwear["conn"], underwear["cfg"]
    root = cfg.active_root
    _judge(cfg, conn, underwear["engine"], root)
    with db._write_lock:
        conn.execute("UPDATE assets SET nsfw_score=0.95 WHERE id=?", (underwear["bare"],))
        conn.commit()
    cfg.underwear_threshold = 0
    _judge(cfg, conn, underwear["engine"], root)
    assert _flag(conn, underwear["bare"])[0] == 1


# -- L-01 ---------------------------------------------------------------------

def test_a_live_clip_follows_its_photograph_at_once(underwear):  # noqa: F811
    conn, cfg = underwear["conn"], underwear["cfg"]
    root = cfg.active_root
    bare = underwear["bare"]
    clip = conn.execute("SELECT id FROM assets WHERE id NOT IN (?, ?) ORDER BY id LIMIT 1",
                        (bare, underwear["swim"])).fetchone()[0]
    rel = conn.execute("SELECT rel_path FROM assets WHERE id=?", (clip,)).fetchone()[0]
    with db._write_lock:
        conn.execute("UPDATE assets SET is_live=1, live_video_path=? WHERE id=?", (rel, bare))
        conn.execute("UPDATE assets SET live_clip=1, nsfw=0 WHERE id=?", (clip,))
        conn.commit()
    _judge(cfg, conn, underwear["engine"], root)
    assert conn.execute("SELECT nsfw FROM assets WHERE id=?", (clip,)).fetchone()[0] == 1
    # And is let go with it.
    cfg.underwear_threshold = 0
    _judge(cfg, conn, underwear["engine"], root)
    assert conn.execute("SELECT nsfw FROM assets WHERE id=?", (clip,)).fetchone()[0] == 0


# -- L-02 ---------------------------------------------------------------------

def test_a_hidden_screenshot_is_not_flagged(underwear):  # noqa: F811
    conn, cfg = underwear["conn"], underwear["cfg"]
    with db._write_lock:
        conn.execute("UPDATE assets SET rel_path='Screenshot_20200824-210908.jpg', "
                     "visibility=2, vis_source=? WHERE id=?",
                     (db.SCREEN_SOURCE, underwear["bare"]))
        conn.commit()
    _judge(cfg, conn, underwear["engine"], cfg.active_root)
    assert _flag(conn, underwear["bare"])[0] == 0
    assert conn.execute("SELECT revealing FROM assets WHERE id=?",
                        (underwear["bare"],)).fetchone()[0] > 0.99


def test_a_hidden_pictures_reading_is_forgotten_and_its_flag_kept(underwear):  # noqa: F811
    """The score is a reading off the vector forget_ai_reading drops; the flag
    it gave stays, so the picture is still explicit should it be shown again."""
    conn, cfg = underwear["conn"], underwear["cfg"]
    _judge(cfg, conn, underwear["engine"], cfg.active_root)
    with db._write_lock:
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (underwear["bare"],))
        conn.commit()
    db.forget_ai_reading(conn, ids=[underwear["bare"]])
    assert conn.execute("SELECT revealing, revealing_version FROM assets WHERE id=?",
                        (underwear["bare"],)).fetchone()[:] == (0, 0)
    cfg.underwear_threshold = 0.5
    db.unflag_revealing(conn, cfg.active_root, revealing.line(cfg), 0.6)
    assert _flag(conn, underwear["bare"]) == (1, db.REVEALING_SOURCE)
