"""Browsing stays quick on a large library, and answers exactly as before.

Each change here made something a family member does every few seconds
cheaper — the tagger no longer throws the sidebar's figures away on every
write, the person and tag filters stop reading the whole library, the people
and occasion lists and the map's places are remembered, the gallery's count is
kept between pieces of a large layout. A faster answer that is ever a
different answer is a bug, so most of these build a small library with every
awkward case in it and check the new answer against the old query, row for
row and in order, before and after the library changes.
"""
import json
import threading
import time

import pytest

from ninaivu.server import auth
from ninaivu.storage import db, geo, roots
from ninaivu.utils.location import LocationFreeCopies

ROOT = "/lib"
CITIES = ("Chennai", "Madurai", "chennai", None, "Paris")
TAGS = (["beach"], ["Beach"], ["beach house"], ["sunset", "beach"], ["living room"],
        ["mom's car"], ["100%"], ["café"], ["!!"], [], ["sunset beach"], ["tree"])


def _open(path):
    conn = db.init_db(path)
    auth.init_auth_schema(conn)
    return conn


def _blob():
    return bytes(16)


@pytest.fixture()
def lib(tmp_path):
    """Sixty items with every visibility, flag, kind and tag shape, three
    people across their faces, and four occasions."""
    conn = _open(tmp_path / "index.db")
    rows = []
    for i in range(60):
        rows.append((
            ROOT, f"f{i % 4}/img{i}.jpg", f"img{i}.jpg", f"f{i % 4}",
            "audio" if i % 17 == 0 else ("video" if i % 7 == 0 else "picture"),
            i % 3,                                   # visibility 0, 1, 2
            1 if i % 13 == 0 else 0,                 # nsfw
            1 if i % 19 == 0 else 0,                 # trashed
            f"20{10 + i % 9}-0{1 + i % 9}-1{i % 10}",
            json.dumps(TAGS[i % len(TAGS)], ensure_ascii=i % 2 == 0),
            1.5e9 + (i * 7919) % 60 * 3600 if i % 5 else None,   # some without a time
            1.4e9 + i,
            CITIES[i % len(CITIES)], "India" if i % 2 else "France",
            None if i % 6 == 0 else f"t{i}",
            12.0 + i * 0.01, 77.0,
            1 + i % 4 if i % 3 else None,
        ))
    conn.executemany(
        "INSERT INTO assets(root, rel_path, filename, folder, kind, visibility, nsfw, "
        "trashed, date_key, tags, captured_at, mtime, city, country, thumb, gps_lat, "
        "gps_lon, occasion_id, size, indexed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1000,0)", rows)
    for o in range(1, 5):
        conn.execute("INSERT INTO occasions(id, root, key, title, place, started_at, "
                     "ended_at, days, count) VALUES (?,?,?,?,?,?,?,1,0)",
                     (o, ROOT, f"k{o}", f"Chennai, Trip {o}", "Chennai", 1.5e9 + o, 1.5e9 + o))
    now = time.time()
    for p, name in ((1, "Maya"), (2, "Arjun"), (3, "Ghost")):
        conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES (?,?,?)",
                     (p, name, now))
    for i in range(1, 61):
        for person in (1, 2, 3):
            if (i * person) % (person + 2) == 0:
                for _ in range(1 + i % 2):           # some photographs twice over
                    conn.execute(
                        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, "
                        "quality) VALUES (?,?,'confirmed','[0,0,9,9]',?,?)",
                        (i, person, _blob(), (i * 37 % 100) / 100))
    conn.execute("UPDATE people_clusters SET cover_face_id = "
                 "(SELECT MIN(id) FROM faces WHERE person_id = people_clusters.id)")
    conn.commit()
    return conn


def _old_ids(conn, extra, params, max_visibility=1):
    """The gallery's rows the way query_assets read them before."""
    where = (f"a.root = ? AND {db.visibility_clause('a', max_visibility)} "
             f"AND a.trashed = 0 AND a.nsfw = 0 AND {extra}")
    rows = conn.execute(
        f"SELECT a.id FROM assets a WHERE {where} "
        "ORDER BY COALESCE(a.captured_at, a.mtime) DESC, a.id DESC",
        (ROOT, max_visibility, *params)).fetchall()
    return [r[0] for r in rows]


def _new_ids(conn, max_visibility=1, **filters):
    rows, total = db.query_assets(conn, ROOT, max_visibility=max_visibility,
                                  limit=1000, **filters)
    ids = [r["id"] for r in rows]
    assert total == len(ids)
    return ids


# --- 4: the person filter ------------------------------------------------------

@pytest.mark.parametrize("ceiling", [0, 1, 2])
def test_the_person_filter_finds_the_same_photographs_in_the_same_order(lib, ceiling):
    for person in (1, 2, 3, 99):
        old = _old_ids(lib, "EXISTS (SELECT 1 FROM faces pf WHERE pf.asset_id = a.id "
                            "AND pf.person_id = ?)", (person,), ceiling)
        assert _new_ids(lib, ceiling, person=person) == old
    both = _old_ids(lib, "EXISTS (SELECT 1 FROM faces pf WHERE pf.asset_id = a.id AND "
                         "pf.person_id = ?) AND EXISTS (SELECT 1 FROM faces pf WHERE "
                         "pf.asset_id = a.id AND pf.person_id = ?)", (1, 2), ceiling)
    assert _new_ids(lib, ceiling, people=[1, 2]) == both
    assert both or ceiling < 2


def test_the_maps_person_filter_is_unchanged(lib):
    where, params = geo._filters(ROOT, person=2)              # noqa: SLF001
    new = sorted(r[0] for r in lib.execute(f"SELECT a.id FROM assets a WHERE {where}", params))
    old_where = where.replace(
        "a.id IN (SELECT asset_id FROM faces WHERE person_id = ?)",
        "EXISTS (SELECT 1 FROM faces pf WHERE pf.asset_id = a.id AND pf.person_id = ?)")
    assert old_where != where
    old = sorted(r[0] for r in lib.execute(f"SELECT a.id FROM assets a WHERE {old_where}",
                                           params))
    assert new and new == old


# --- 5: the tag filter -----------------------------------------------------------

@pytest.mark.parametrize("tag", ["beach", "Beach", "BEACH", "beach house", "sunset",
                                 "sunset beach", "living room", "mom's car", "mom", "100%",
                                 "100_", "café", "cafe", "!!", "tree", "nothing", ""])
def test_the_tag_filter_finds_exactly_what_the_like_did(lib, tag):
    assert db.fts_enabled(lib)
    if not tag:
        assert _new_ids(lib, tag=tag) == _old_ids(lib, "1", ())
        return
    old = _old_ids(lib, "a.tags LIKE ? ESCAPE '\\'", (f'%"{db._like_escape(tag)}"%',))
    assert _new_ids(lib, tag=tag) == old
    assert _new_ids(lib, 2, tag=tag) == _old_ids(
        lib, "a.tags LIKE ? ESCAPE '\\'", (f'%"{db._like_escape(tag)}"%',), 2)


def test_the_tag_filter_follows_a_retag_and_works_with_words(lib):
    lib.execute("UPDATE assets SET tags='[\"beach\"]' WHERE id=2"); lib.commit()
    old = _old_ids(lib, "a.tags LIKE ? ESCAPE '\\'", ('%"beach"%',))
    assert 2 in old and _new_ids(lib, tag="beach") == old
    # Words and a tag at once: two matches against the same full-text table.
    rows, _ = db.query_assets(lib, ROOT, tag="beach", text="img", limit=100)
    assert {r["id"] for r in rows} == set(old)


def test_without_the_full_text_index_the_tag_filter_still_works(lib):
    db.set_meta(lib, "fts", "0"); lib.commit()
    old = _old_ids(lib, "a.tags LIKE ? ESCAPE '\\'", ('%"beach"%',))
    assert old and _new_ids(lib, tag="beach") == old


# --- 6: people and occasions, remembered -------------------------------------------

def _fresh(fn):
    db._AGGREGATES.clear()                                     # noqa: SLF001
    return fn()


@pytest.mark.parametrize("ceiling", [0, 1, 2])
def test_people_and_occasions_are_the_same_remembered_or_not(lib, ceiling):
    people = lambda: db.list_people(lib, ROOT, max_visibility=ceiling)       # noqa: E731
    occasions = lambda: db.list_occasions(lib, ROOT, max_visibility=ceiling)  # noqa: E731
    first_people, first_occasions = _fresh(people), _fresh(occasions)
    assert first_people and (first_occasions or ceiling == 0)
    assert people() == first_people and occasions() == first_occasions
    guard, params = db._face_visibility_sql("a", ceiling, None, ROOT)       # noqa: SLF001
    assert first_people == db._list_people(lib, ROOT, guard, params, ceiling, None, 1)


CHANGES = [
    "UPDATE people_clusters SET name='Maya K' WHERE id=1",
    "UPDATE people_clusters SET avatar_asset_id=4 WHERE id=2",
    "UPDATE people_clusters SET cover_face_id=(SELECT MAX(id) FROM faces WHERE person_id=1) "
    "WHERE id=1",
    "UPDATE faces SET person_id=2 WHERE id=(SELECT MIN(id) FROM faces WHERE person_id=1)",
    "UPDATE faces SET quality=0.99 WHERE id=(SELECT MAX(id) FROM faces WHERE person_id=3)",
    "DELETE FROM faces WHERE person_id=3",
    "DELETE FROM people_clusters WHERE id=2",
    "INSERT INTO people_clusters(id, name, created_at) VALUES (9, 'New', 0)",
    "UPDATE assets SET visibility=2 WHERE id IN (3, 4, 5)",
    "UPDATE assets SET occasion_id=4 WHERE id IN (7, 8)",
    "UPDATE assets SET thumb=NULL WHERE occasion_id=2",
    "UPDATE assets SET thumb='x' WHERE thumb IS NULL",
    "UPDATE occasions SET title='Madurai, Wedding', place='Madurai' WHERE id=3",
    "DELETE FROM occasions WHERE id=1",
    "UPDATE assets SET date_key='2030-01-01' WHERE id=10",
]


@pytest.mark.parametrize("change", CHANGES)
def test_people_and_occasions_follow_every_change(lib, change):
    people = lambda: db.list_people(lib, ROOT, max_visibility=1)        # noqa: E731
    occasions = lambda: db.list_occasions(lib, ROOT, max_visibility=1)  # noqa: E731
    people(), occasions()                                # remembered now
    lib.execute(change)
    lib.commit()
    assert people() == _fresh(people)
    assert occasions() == _fresh(occasions)


@pytest.mark.parametrize("change", [
    "UPDATE assets SET city='Ooty' WHERE id=5",
    "UPDATE assets SET gps_lat=40.0 WHERE id=6",
    "UPDATE assets SET thumb=NULL WHERE id=9",
    "UPDATE assets SET visibility=2 WHERE id=12",
])
def test_the_maps_places_follow_every_change(lib, change):
    places = lambda: geo.places(lib, ROOT, max_visibility=1)            # noqa: E731
    by_person = lambda: geo.places(lib, ROOT, max_visibility=1, person=1)  # noqa: E731
    places(), by_person()
    lib.execute(change)
    lib.execute("UPDATE faces SET person_id=1 WHERE person_id=2")
    lib.commit()
    assert places() == _fresh(places)
    assert by_person() == _fresh(by_person)


# --- 1: the tagger does not throw every figure away ----------------------------------

def test_writing_a_value_back_unchanged_moves_no_counter(lib):
    before = db.change_counters(lib)
    tags, nsfw = lib.execute("SELECT tags, nsfw FROM assets WHERE id=2").fetchone()
    db.store_ai_fields(lib, 2, tags=tags, nsfw=nsfw)
    lib.execute("UPDATE assets SET visibility=visibility, city=city, thumb=thumb")
    lib.execute("UPDATE faces SET person_id=person_id, quality=quality")
    lib.commit()
    assert db.change_counters(lib) == before


def test_a_new_tag_moves_only_the_tag_counter(lib):
    before = db.library_generation(lib)
    tags_before = db.meta_counter(lib, db.TAGS_GENERATION_KEY)
    timeline = db.timeline(lib, ROOT)
    db.store_ai_fields(lib, 2, tags=["snow"])
    assert db.library_generation(lib) == before, "the timeline is kept"
    assert db.meta_counter(lib, db.TAGS_GENERATION_KEY) > tags_before
    assert db.timeline(lib, ROOT) == timeline
    assert any(t["name"] == "snow" for t in db.facets(lib, [ROOT])["tags"])


def test_an_existing_database_gets_the_new_triggers(tmp_path):
    conn = _open(tmp_path / "a.db")
    # As an earlier version left it: a trigger with no WHEN, tags among its
    # columns, and none of the newer counters.
    conn.executescript("""
        DROP TRIGGER assets_generation_update;
        CREATE TRIGGER assets_generation_update AFTER UPDATE OF tags, visibility ON assets
        BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1
              WHERE key = 'assets_generation'; END;
        DROP TRIGGER assets_tags_generation_update;
        DELETE FROM meta WHERE key = 'assets_tags_generation';
    """)
    conn.commit()
    db.close_all()
    db._READY.clear()                                          # noqa: SLF001
    again = _open(tmp_path / "a.db")
    sql = again.execute("SELECT sql FROM sqlite_master WHERE name='assets_generation_update'"
                        ).fetchone()[0]
    assert "WHEN" in sql and "tags" not in sql.split("WHEN")[0]
    assert db.meta_counter(again, db.TAGS_GENERATION_KEY) is not None
    _open(tmp_path / "a.db")                                   # and again: idempotent


def test_a_search_vector_moves_its_counter_only_when_one_is_added_or_removed(lib):
    from ninaivu.storage.embeddings import store_embedding
    lib.execute("UPDATE assets SET visibility=1, nsfw=0, trashed=0 WHERE id=2")
    lib.commit()
    before = db.meta_counter(lib, db.EMBEDDINGS_GENERATION_KEY)
    store_embedding(lib, 2, "m", 4, bytes(16))
    added = db.meta_counter(lib, db.EMBEDDINGS_GENERATION_KEY)
    assert added > before
    store_embedding(lib, 2, "m", 4, b"\x01" * 16)
    assert db.meta_counter(lib, db.EMBEDDINGS_GENERATION_KEY) == added
    lib.execute("DELETE FROM embeddings WHERE asset_id=2"); lib.commit()
    assert db.meta_counter(lib, db.EMBEDDINGS_GENERATION_KEY) > added


# --- 2: the gallery's count, and its ETag ---------------------------------------------

@pytest.mark.parametrize("filters", [
    {}, {"tag": "beach"}, {"person": 1}, {"occasion": 2}, {"place": "chennai"},
    {"kinds": ["video"]}, {"folder": "f1"},
])
def test_a_remembered_count_follows_the_library(lib, filters):
    def count():
        return db.query_assets(lib, ROOT, limit=1, remember_total=True, **filters)[1]
    def plain():
        return db.query_assets(lib, ROOT, limit=1, **filters)[1]
    assert count() == plain()
    for change in ("UPDATE assets SET tags='[\"beach\"]', city='Chennai', occasion_id=2, "
                   "kind='video', folder='f1' WHERE id IN (20, 21, 22, 23)",
                   "INSERT INTO faces(asset_id, person_id, bbox, embedding) "
                   "VALUES (25, 1, '[0,0,1,1]', x'00')",
                   "DELETE FROM faces WHERE person_id=1 AND asset_id < 10",
                   "UPDATE assets SET visibility=2 WHERE id IN (24, 25, 26)"):
        lib.execute(change)
        lib.commit()
        assert count() == plain(), change


def test_the_layout_answers_304_until_something_it_shows_changes(as_family, people, app):
    conn = people["conn"]
    first = as_family.get("/api/segments")
    assert first.status_code == 200 and first.headers.get("ETag")
    tag = first.headers["ETag"]
    again = as_family.get("/api/segments", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.headers["ETag"] == tag
    other = as_family.get("/api/segments?kind=video", headers={"If-None-Match": tag})
    assert other.status_code == 200, "another query is another tag"

    some = first.get_json()["segments"][0]["items"][0][0]
    for change in (
        lambda: db.set_user_asset(conn, people["family"].id, some, favorite=1),
        lambda: conn.execute("UPDATE assets SET rotation=90 WHERE id=?", (some,)),
        lambda: conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (some,)),
    ):
        change()
        conn.commit()
        fresh = as_family.get("/api/segments", headers={"If-None-Match": tag})
        assert fresh.status_code == 200
        tag = fresh.headers["ETag"]

    # Somebody else's favourite is not this viewer's business, nor a reason to resend.
    db.set_user_asset(conn, people["admin"].id, some, favorite=1)
    assert as_family.get("/api/segments", headers={"If-None-Match": tag}).status_code == 304
    # A search is always worked out.
    assert "ETag" not in as_family.get("/api/segments?q=shot").headers


def test_another_viewer_never_matches_the_tag(as_family, as_admin):
    tag = as_family.get("/api/segments").headers["ETag"]
    assert as_admin.get("/api/segments", headers={"If-None-Match": tag}).status_code == 200


# --- 3: words already turned into a vector ---------------------------------------------

def test_the_same_words_are_encoded_once_per_model():
    from ninaivu.api import api

    class Engine:
        model_id = "clip/a"
        calls = 0

        def encode_text(self, text):
            Engine.calls += 1
            return [len(text), Engine.calls]

    engine = Engine()
    first = api._text_vector(engine, "a dog on a beach")       # noqa: SLF001
    assert api._text_vector(engine, "a dog on a beach") is first  # noqa: SLF001
    assert Engine.calls == 1
    api._text_vector(engine, "a cat")                          # noqa: SLF001
    engine.model_id = "clip/b"
    api._text_vector(engine, "a cat")                          # noqa: SLF001
    assert Engine.calls == 3
    for n in range(api._TEXT_VECTORS_MAX + 5):                 # noqa: SLF001
        api._text_vector(engine, f"words {n}")                 # noqa: SLF001
    assert len(api._TEXT_VECTORS[engine]) == api._TEXT_VECTORS_MAX  # noqa: SLF001


# --- 8: a location-free copy keeps its tag -----------------------------------------------

def test_a_location_free_copy_is_sent_with_a_tag_that_matches_next_time(as_family, people, app):
    app.config["MV_CONFIG"].strip_location = "all"
    some = people["conn"].execute(
        "SELECT id FROM assets WHERE kind='picture' AND ext IN ('jpg','jpeg') "
        "AND visibility <= 1 LIMIT 1").fetchone()[0]
    first = as_family.get(f"/api/file/{some}")
    assert first.status_code == 200 and first.headers.get("ETag")
    time.sleep(0.02)                     # the copy's time moves when it is used again
    again = as_family.get(f"/api/file/{some}", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304


def test_copies_of_different_photographs_are_made_side_by_side(tmp_path, monkeypatch):
    from PIL import Image
    copies = LocationFreeCopies(tmp_path / "state")
    sources = []
    for n in range(2):
        path = tmp_path / f"p{n}.png"
        Image.new("RGB", (4, 4)).save(path)
        sources.append(({"id": n, "ext": "png", "kind": "picture", "filename": path.name}, path))
    inside, release = threading.Event(), threading.Event()

    def slow(path, out, png):
        if path == sources[0][1]:
            inside.set()
            release.wait(5)
        out.write_bytes(b"x")
    monkeypatch.setattr(LocationFreeCopies, "_picture", staticmethod(slow))
    worker = threading.Thread(target=copies.copy, args=sources[0])
    worker.start()
    assert inside.wait(5)
    started = time.monotonic()
    copies.copy(*sources[1])            # not held up by the first
    assert time.monotonic() - started < 2
    release.set()
    worker.join(5)


# --- 9: a shared album's date only under a date policy --------------------------------------

def test_an_albums_date_is_not_worked_out_without_a_date_policy(lib, monkeypatch):
    album = db.create_album(lib, "Trip")
    lib.execute("INSERT INTO album_items(album_id, asset_id, added_at) VALUES (?, 2, 0)",
                (album,))
    lib.commit()
    monkeypatch.setattr(db, "album_date", lambda *a: pytest.fail("asked for its date"))
    rows, _ = db.query_assets(lib, ROOT, album=album, max_visibility=2, limit=10)
    assert [r["id"] for r in rows] == [2]


# --- 10: one thread asks the disk ------------------------------------------------------------

def test_one_thread_asks_the_disk_and_the_rest_use_the_last_answer(monkeypatch, tmp_path):
    roots.forget()
    asked = []
    gate = threading.Event()

    def slow(root, **_):
        asked.append(root)
        gate.wait(5)
        return True
    monkeypatch.setattr(roots, "root_present", slow)
    gate.set()
    assert roots.available("X:/lib", now=0.0) is True           # a first answer
    gate.clear()
    asked.clear()
    answers = []
    later = roots.BELIEVE_FOR + 1
    threads = [threading.Thread(target=lambda: answers.append(roots.available("X:/lib", now=later)))
               for _ in range(6)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while len(answers) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(answers) == 5 and all(answers), "five answered from the last value"
    gate.set()
    for thread in threads:
        thread.join(5)
    assert asked == ["X:/lib"], "and the disk was asked once"
    roots.forget()
