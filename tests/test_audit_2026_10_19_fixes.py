"""Fixes for the seventh audit's Medium findings (19 October 2026).

Each test names its finding. The probes the audit used are rewritten here as
the conditions the fix keeps.
"""
from __future__ import annotations

from ninaivu.storage import capsules, db, recycle


# -- C-08: a photograph restored from the bin is back in its time capsule ----

def test_a_photograph_comes_back_from_the_bin_into_its_capsule(scanned):
    _, conn, _ = scanned
    ids = [r["id"] for r in conn.execute("SELECT id FROM assets WHERE kind='picture' "
                                         "ORDER BY id LIMIT 2")]
    capsule = capsules.create(conn, title="For 2040", letter="", opens_on="2040-01-01",
                              for_user=None, created_by=None, from_name="Amma",
                              asset_ids=ids)
    conn.execute("INSERT OR REPLACE INTO descriptions(asset_id, text, model, version, made_at) "
                 "VALUES(?, 'a temple at dusk', 'florence', 1, 0)", (ids[0],))
    conn.commit()
    assert capsules.items_of(conn, capsule) == ids
    recycle.recycle(conn, [ids[0]])
    assert capsules.items_of(conn, capsule) == ids[1:]
    entry = recycle.listing(conn)[0]
    assert recycle.restore(conn, [entry["id"]])["restored"] == 1
    back = conn.execute("SELECT id FROM assets WHERE rel_path=(SELECT rel_path FROM recycled "
                        "WHERE id=?)", (entry["id"],)).fetchone()["id"]
    assert sorted(capsules.items_of(conn, capsule)) == sorted([back, ids[1]])
    assert conn.execute("SELECT position FROM capsule_items WHERE capsule_id=? AND asset_id=?",
                        (capsule, back)).fetchone()[0] == 0, "in its place"
    assert conn.execute("SELECT text FROM descriptions WHERE asset_id=?",
                        (back,)).fetchone()[0] == "a temple at dusk"


def test_a_capsule_deleted_meanwhile_is_not_brought_back(scanned):
    _, conn, _ = scanned
    first = conn.execute("SELECT id FROM assets WHERE kind='picture' ORDER BY id").fetchone()["id"]
    capsule = capsules.create(conn, title="Gone", letter="", opens_on="2040-01-01",
                              for_user=None, created_by=None, from_name="", asset_ids=[first])
    recycle.recycle(conn, [first])
    capsules.delete(conn, capsule)
    recycle.restore(conn, [recycle.listing(conn)[0]["id"]])
    assert conn.execute("SELECT COUNT(*) FROM capsule_items").fetchone()[0] == 0


# -- C-18: a long film's prompt stays inside what the model reads ------------

def test_a_long_film_gives_each_moment_less_rather_than_overflowing():
    from ninaivu.media import storyboard
    moments = [{"when": 1577836800 + n * 86400, "place": "Ooty", "people": ["Paati"],
                "about": "அ" * 400, "story": "ஆ" * 400} for n in range(120)]
    messages = storyboard.prompt(moments, language="ta")
    listed = messages[1]["content"]
    assert len(listed) < storyboard.PROMPT_CHARS + 120 * 120, len(listed)
    short = storyboard.prompt(moments[:10], language="ta")[1]["content"]
    assert short.count("அ") == 10 * storyboard.DESCRIPTION_CHARS, "a short film keeps it all"


# -- C-37: never cut a Tamil letter from its vowel sign -----------------------

def test_text_is_cut_between_letters_not_inside_one():
    from ninaivu.media import storyboard
    word = "தமிழ்"                      # த ம ி ழ ்
    assert storyboard.cut(word, 2) == "த", "not ம without its ி"
    assert storyboard.cut(word, 3) == "தமி"
    assert storyboard.cut(word, 4) == "தமி", "not ழ without its ்"
    assert storyboard.cut(word, 5) == word
    assert storyboard.cut("hello", 3) == "hel"


# -- C-21: months and years as the household's clock reads them --------------

def test_a_new_years_photograph_is_january_in_india(monkeypatch):
    import time
    from ninaivu.media import storyboard
    if not hasattr(time, "tzset"):
        import pytest
        pytest.skip("needs time.tzset")
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        # 02:00 on 1 January 2020 in India is 20:30 on 31 December UTC.
        when = 1577824200
        stamp = storyboard.local_time(when)
        assert (stamp.tm_year, stamp.tm_mon, stamp.tm_mday) == (2020, 1, 1)
        assert "1 January 2020" in storyboard._moment_line(0, {"when": when}, None)   # noqa: SLF001
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
