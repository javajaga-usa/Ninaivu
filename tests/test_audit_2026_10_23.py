"""The audit after 2.2.0 (2026-10-23): what the seven new Advanced trials
and the features before them got wrong, each pinned here.

* Letters read aloud: the text reader never reads a hidden photograph, the
  words it read go when a photograph is hidden afterwards, and a reading's
  sound file does not outlive its words or its photograph.
* The household's date limit, which moves no library counter, is in force at
  once in Our year, A life in photographs and the festival albums, not after
  their answers are worked out again.
* Moments in videos are held to the best of what the viewer may see.
* Talk about this photo leaves no picture behind after a crash.
* A scene's picture is written under a name of its own.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ninaivu.api import api_letters, api_photo_talk
from ninaivu.media import life_story, our_year
from ninaivu.server import auth, date_policy
from ninaivu.storage import db, letters as letters_store

LETTER = "Dear Kannan,\nWe reached Madurai on Sunday."


def _a_photograph(conn) -> int:
    return int(conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 "
        "ORDER BY id LIMIT 1").fetchone()[0])


def _limit_dates(conn, **roles) -> None:
    """Set the household's date limit as People & access does."""
    db.set_meta(conn, date_policy.KEY, json.dumps({**date_policy.DEFAULTS, **roles}))
    conn.commit()


# -- letters read aloud ----------------------------------------------------------------

@pytest.fixture()
def lettered(app, people):
    cfg = app.config["MV_CONFIG"]
    cfg.letters_aloud = True
    return {"cfg": cfg, "conn": people["conn"]}


def test_a_hidden_photograph_is_never_read_by_the_text_reader(as_admin, lettered, monkeypatch):
    conn = lettered["conn"]
    target = _a_photograph(conn)
    read = []
    monkeypatch.setattr("ninaivu.media.ocr.available", lambda: True)
    monkeypatch.setattr("ninaivu.media.letters.read_from_photo",
                        lambda picture: read.append(picture) or {"words": "x", "language": "en",
                                                                 "lines": 1})
    db.update_asset(conn, target, visibility=auth.VIS_HIDDEN)
    status = as_admin.get(f"/api/asset/{target}/letter").get_json()
    assert status["readable"] is False, "the page is told before it offers the button"
    refused = as_admin.post(f"/api/asset/{target}/letter/read")
    assert refused.status_code == 403 and "typed" in refused.get_json()["error"]
    assert read == [], "not one pixel reached the text reader"
    # Typing the words is still the administrator's to do.
    kept = as_admin.put(f"/api/asset/{target}/letter", json={"words": LETTER})
    assert kept.status_code == 200 and kept.get_json()["words"] == LETTER


def test_words_the_reader_read_go_when_the_photograph_is_hidden(lettered):
    conn = lettered["conn"]
    target = _a_photograph(conn)
    other = int(conn.execute("SELECT id FROM assets WHERE kind='picture' AND id != ? "
                             "ORDER BY id LIMIT 1", (target,)).fetchone()[0])
    letters_store.put(conn, target, LETTER, "en", "read", None)
    letters_store.put(conn, other, LETTER, "en", "typed", None)
    db.update_asset(conn, target, visibility=auth.VIS_HIDDEN)
    db.update_asset(conn, other, visibility=auth.VIS_HIDDEN)
    db.forget_ai_reading(conn, ids=[target, other])
    assert letters_store.get(conn, target) is None, "what AI read off a hidden photograph goes"
    assert letters_store.get(conn, other)["words"] == LETTER, "what somebody typed stays"


def test_a_reading_goes_with_its_words_and_its_photograph(lettered):
    conn = lettered["conn"]
    folder = Path(lettered["cfg"].state_dir) / "letters"
    folder.mkdir(parents=True, exist_ok=True)
    target = _a_photograph(conn)
    letters_store.put(conn, target, LETTER, "en", "typed", None)
    key = api_letters.letters.key(LETTER, "en")
    letters_store.keep_reading(conn, target, {"key": key, "duration": 1.0, "segments": []})
    wanted = folder / f"{target}-{key}.wav"
    old_words = folder / f"{target}-{'0' * 20}.wav"
    erased = folder / f"999999-{'1' * 20}.wav"
    writing = folder / f".{target}-{key}.123.part.wav"
    fresh = folder / f"888888-{'2' * 20}.wav"
    long_ago = time.time() - 3600
    for path in (wanted, old_words, erased, writing, fresh):
        path.write_bytes(b"RIFF")
        if path is not fresh:
            os.utime(path, (long_ago, long_ago))
    assert letters_store.sweep_readings(folder, conn) == 2
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        p.name for p in (wanted, writing, fresh)), \
        "kept: the reading of the words as they are, one being written, one just made"


def test_letter_words_are_written_under_the_index_write_lock(lettered):
    conn = lettered["conn"]
    target = _a_photograph(conn)
    held = []
    real = db._write_lock                                             # noqa: SLF001

    class Watching:
        def __enter__(self):
            held.append(True)
            return real.__enter__()

        def __exit__(self, *exc):
            return real.__exit__(*exc)

    db._write_lock = Watching()                                       # noqa: SLF001
    try:
        letters_store.put(conn, target, LETTER, "en", "typed", None)
        letters_store.keep_reading(conn, target, {"key": "k"})
        letters_store.remove(conn, target)
    finally:
        db._write_lock = real                                         # noqa: SLF001
    assert len(held) == 3


# -- the date limit, in force at once ---------------------------------------------------

def test_our_year_follows_a_new_date_limit_at_once(as_family, app, people, monkeypatch):
    monkeypatch.setattr(our_year, "MIN_ITEMS", 3)
    conn = people["conn"]
    app.config["MV_CONFIG"].our_year = True
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET date_key='2023-05-01', captured_at=1682899200 "
                     "WHERE kind='picture'")
        conn.commit()
    assert [y["year"] for y in as_family.get("/api/our-year").get_json()["years"]] == [2023]
    assert as_family.get("/api/our-year/2023").status_code == 200
    _limit_dates(conn, family="before", cutoff="2010-01-01")
    assert as_family.get("/api/our-year").get_json()["years"] == [], \
        "a year now outside the family's dates is not told from a kept answer"
    assert as_family.get("/api/our-year/2023").status_code == 404


def test_a_life_follows_a_new_date_limit_at_once(as_family, app, people, monkeypatch):
    monkeypatch.setattr(life_story, "MIN_ITEMS", 3)
    monkeypatch.setattr(life_story, "MIN_CHAPTER", 1)
    conn = people["conn"]
    app.config["MV_CONFIG"].life_story = True
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 ORDER BY id")]
    with db._write_lock:                                              # noqa: SLF001
        for n, aid in enumerate(ids):
            conn.execute("UPDATE assets SET date_key=?, sharpness=200, brightness=0.5 WHERE id=?",
                         (f"{1990 + n}-01-01", aid))
        paati = conn.execute(
            "INSERT INTO people_clusters(name, created_at) VALUES ('Paati', 0)").lastrowid
        for aid in ids:
            conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality) "
                         "VALUES (?, ?, 'confirmed', '[0,0,10,10]', x'00', 0.9)", (aid, paati))
        conn.commit()
    listed = as_family.get("/api/life").get_json()["people"]
    assert [p["photos"] for p in listed] == [len(ids)]
    before = as_family.get(f"/api/life/{paati}").get_json()["person"]["photos"]
    assert before == len(ids)
    _limit_dates(conn, family="before", cutoff=f"{1990 + 4}-01-01")
    assert as_family.get(f"/api/life/{paati}").get_json()["person"]["photos"] == 4
    assert [p["photos"] for p in as_family.get("/api/life").get_json()["people"]] == [4]


def test_the_festival_albums_follow_a_new_date_limit_at_once(as_family, app, people):
    conn = people["conn"]
    app.config["MV_CONFIG"].festival_albums = True
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 ORDER BY id")]
    with db._write_lock:                                              # noqa: SLF001
        conn.executemany("UPDATE assets SET date_key='2023-12-25', date_source='exif', "
                         "captured_at=1703500000 WHERE id=?", [(i,) for i in ids])
        conn.commit()

    def christmas():
        found = as_family.get("/api/festivals").get_json()["festivals"]
        return next(f for f in found if f["key"] == "christmas")["items"]
    assert christmas() == len(ids)
    _limit_dates(conn, family="before", cutoff="2020-01-01")
    assert christmas() == 0


# -- moments in videos -----------------------------------------------------------------

def test_moments_are_held_to_the_best_the_viewer_may_see(as_family, app, people, monkeypatch):
    from ninaivu.api import api_video_moments
    from ninaivu.storage import video_moments as store
    conn = people["conn"]
    app.config["MV_CONFIG"].video_moments = True
    hidden, shown = [r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE live_clip=0 AND raw_twin=0 ORDER BY id LIMIT 2")]
    with db._write_lock:                                              # noqa: SLF001
        conn.executemany("UPDATE assets SET kind='video' WHERE id=?", [(hidden,), (shown,)])
        conn.commit()
    db.update_asset(conn, hidden, visibility=auth.VIS_HIDDEN)

    class Engine:
        semantic = True
        model_id = "m"
        search_floor = 0.1
    monkeypatch.setattr(api_video_moments, "_search_engine", lambda: Engine())
    monkeypatch.setattr(api_video_moments, "_text_vector", lambda engine, text: [1.0])
    monkeypatch.setattr(store, "search", lambda *a, **k: [
        {"id": hidden, "score": 0.9, "moments": [{"at": 1.0, "score": 0.9}]},
        {"id": shown, "score": 0.4, "moments": [{"at": 2.0, "score": 0.4}]}])
    clips = as_family.get("/api/moments?q=cake").get_json()["clips"]
    assert [c["id"] for c in clips] == [shown], \
        "a hidden clip's better score does not cut the family's own match"


# -- talk about this photo -------------------------------------------------------------

def test_a_picture_left_by_a_crash_mid_answer_is_cleared(app):
    cfg = app.config["MV_CONFIG"]
    cfg.photo_talk = True
    folder = Path(cfg.state_dir) / "talk"
    folder.mkdir(parents=True, exist_ok=True)
    left = folder / "aa.jpg"
    asking = folder / "bb.jpg"
    left.write_bytes(b"x")
    asking.write_bytes(b"x")
    long_ago = time.time() - api_photo_talk.LEFTOVER_SECONDS - 60
    os.utime(left, (long_ago, long_ago))
    with app.test_request_context():
        api_photo_talk._folder()                                      # noqa: SLF001
    assert not left.exists() and asking.exists()


def test_two_openings_of_one_scene_write_their_own_part_files(as_family, app, people, monkeypatch):
    from PIL import Image

    from ninaivu.media import media
    from ninaivu.storage import video_moments as store
    conn = people["conn"]
    app.config["MV_CONFIG"].video_moments = True
    clip = int(conn.execute("SELECT id FROM assets WHERE live_clip=0 AND raw_twin=0 "
                            "ORDER BY id LIMIT 1").fetchone()[0])
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET kind='video' WHERE id=?", (clip,))
        conn.commit()
    monkeypatch.setattr(store, "of_clip", lambda conn, asset_id: [3.0])
    written = []
    real_save = Image.Image.save

    def save(self, fp, *a, **k):
        written.append(Path(fp).name)
        return real_save(self, fp, *a, **k)
    monkeypatch.setattr(Image.Image, "save", save)
    monkeypatch.setattr(media, "extract_video_frame",
                        lambda source, at, max_side: Image.new("RGB", (8, 8)))
    assert as_family.get(f"/api/moments/{clip}/frame?at=3").status_code == 200
    assert written and written[0].startswith(".") and written[0].endswith(".part")
    assert str(os.getpid()) in written[0]
    state = Path(app.config["MV_CONFIG"].state_dir)
    assert not list(state.rglob(".*.part")), "none left behind"
