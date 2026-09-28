"""Library-wide figures are remembered, and never out of date.

The gallery's facets, the sidebar's counts, the timeline and the console's
folder totals each read the whole library, and were worked out afresh on every
request — on 100,000 items the facets were half a second on every gallery load
and again on every keystroke of a search suggestion. They are now kept until a
counter the database maintains itself says the library has changed.

What matters about a cache is not that it is fast, which a test on a shared
runner cannot measure fairly, but that it is never wrong. So these tests change
the library every way a figure can see and check the next answer shows it.
"""

import gzip
import json

from ninaivu.server import auth
from ninaivu.storage import db

ROOT = "D:/lib"


def _open(path):
    conn = db.init_db(path)
    auth.init_auth_schema(conn)
    return conn


def _add(conn, n, *, start=0, folder="2020/01", tags=("beach",), camera="Cam",
         visibility=1, date_key="2020-01-15"):
    conn.executemany(
        "INSERT INTO assets(root, rel_path, filename, folder, kind, visibility, "
        "date_key, tags, camera, size, mtime, indexed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
        [(ROOT, f"{folder}/img{i}.jpg", f"img{i}.jpg", folder, "picture",
          visibility, date_key, json.dumps(list(tags)), camera, 1000, 1.6e9 + i)
         for i in range(start, start + n)])
    conn.commit()


def _facets(conn, **limits):
    return db.facets(conn, [ROOT], **limits)


# --- the counter -------------------------------------------------------------

def test_the_counter_moves_when_a_listed_column_is_written(tmp_path):
    conn = _open(tmp_path / "a.db")
    start = db.library_generation(conn)
    assert start is not None

    _add(conn, 2)
    after_insert = db.library_generation(conn)
    assert after_insert > start

    conn.execute("UPDATE assets SET visibility=2 WHERE id=1"); conn.commit()
    after_update = db.library_generation(conn)
    assert after_update > after_insert

    conn.execute("DELETE FROM assets WHERE id=2"); conn.commit()
    assert db.library_generation(conn) > after_update


def test_the_indexing_pipeline_does_not_move_it(tmp_path):
    """A scan writes thumbnails, hashes and occasions to every row; none of
    the figures reads them, and moving the counter for each would throw every
    remembered figure away for nothing."""
    conn = _open(tmp_path / "a.db")
    _add(conn, 3)
    before = db.library_generation(conn)
    conn.execute("UPDATE assets SET thumb='t', blurhash='b', sharpness=1.0, "
                 "occasion_id=7, indexed_at=5")
    conn.commit()
    assert db.library_generation(conn) == before


def test_two_new_databases_do_not_start_at_the_same_number(tmp_path):
    first = db.library_generation(_open(tmp_path / "a.db"))
    second = db.library_generation(_open(tmp_path / "b.db"))
    assert first != second


def test_reopening_keeps_the_count_and_the_triggers(tmp_path):
    conn = _open(tmp_path / "a.db")
    _add(conn, 1)
    kept = db.library_generation(conn)
    db.close_all()
    again = _open(tmp_path / "a.db")
    assert db.library_generation(again) == kept
    _add(again, 1, start=5)
    assert db.library_generation(again) > kept


# --- never stale --------------------------------------------------------------

def test_facets_see_a_new_tag_at_once(tmp_path):
    conn = _open(tmp_path / "a.db")
    _add(conn, 3, tags=("beach",))
    assert [t["name"] for t in _facets(conn)["tags"]] == ["beach"]
    conn.execute("UPDATE assets SET tags='[\"snow\"]' WHERE id=1"); conn.commit()
    names = {t["name"]: t["count"] for t in _facets(conn)["tags"]}
    assert names == {"beach": 2, "snow": 1}


def test_a_hidden_folder_leaves_the_family_facets_at_once(tmp_path):
    """The case that matters most: facets name folders, and a folder an admin
    has just hidden must not go on being named to the family."""
    conn = _open(tmp_path / "a.db")
    _add(conn, 2, folder="Holiday")
    _add(conn, 2, start=10, folder="Medical")
    family = {f["name"] for f in _facets(conn, max_visibility=1)["folders"]}
    assert family == {"Holiday", "Medical"}

    conn.execute("UPDATE assets SET visibility=2 WHERE folder='Medical'"); conn.commit()
    family = {f["name"] for f in _facets(conn, max_visibility=1)["folders"]}
    assert family == {"Holiday"}
    admin = {f["name"] for f in _facets(conn, max_visibility=2)["folders"]}
    assert admin == {"Holiday", "Medical"}


def test_each_viewer_is_remembered_separately(tmp_path):
    conn = _open(tmp_path / "a.db")
    _add(conn, 2, visibility=0)
    _add(conn, 3, start=10, visibility=1)
    assert _facets(conn, max_visibility=0)["years"] == [{"year": "2020", "count": 2}]
    assert _facets(conn, max_visibility=1)["years"] == [{"year": "2020", "count": 5}]
    assert _facets(conn, max_visibility=0)["years"] == [{"year": "2020", "count": 2}]


def test_stats_follow_a_deletion_and_favourites_are_always_live(tmp_path):
    conn = _open(tmp_path / "a.db")
    user = auth.create_user(conn, "maya", "summerdays24")
    _add(conn, 4)
    assert db.library_stats(conn, [ROOT], viewer_id=user.id)["count"] == 4

    conn.execute("DELETE FROM assets WHERE id=4"); conn.commit()
    stats = db.library_stats(conn, [ROOT], viewer_id=user.id)
    assert stats["count"] == 3 and stats["favorites"] == 0

    # A favourite writes user_assets, not assets: the counter does not move,
    # and the count must still change.
    before = db.library_generation(conn)
    db.set_user_asset(conn, user.id, 1, favorite=1)
    assert db.library_generation(conn) == before
    assert db.library_stats(conn, [ROOT], viewer_id=user.id)["favorites"] == 1


def test_the_timeline_follows_a_new_date(tmp_path):
    conn = _open(tmp_path / "a.db")
    _add(conn, 2, date_key="2020-01-15")
    assert [d["date"] for d in db.timeline(conn, [ROOT])] == ["2020-01-15"]
    conn.execute("UPDATE assets SET date_key='2021-03-01' WHERE id=1"); conn.commit()
    assert [d["date"] for d in db.timeline(conn, [ROOT])] == ["2021-03-01", "2020-01-15"]


def test_a_caller_cannot_edit_the_next_callers_answer(tmp_path):
    conn = _open(tmp_path / "a.db")
    _add(conn, 2)
    first = _facets(conn)
    first["tags"].clear()
    first["folders"][0]["count"] = 999
    again = _facets(conn)
    assert again["tags"] and again["folders"][0]["count"] == 2


def test_without_the_counter_every_answer_is_worked_out(tmp_path):
    """A database whose counter is missing — edited by hand, or by an older
    Ninaivu that dropped the row — is never answered from memory."""
    conn = _open(tmp_path / "a.db")
    _add(conn, 1)
    conn.execute("DELETE FROM meta WHERE key=?", (db.GENERATION_KEY,)); conn.commit()
    calls = []
    db.cached_aggregate(conn, ("x",), lambda: calls.append(1))
    db.cached_aggregate(conn, ("x",), lambda: calls.append(1))
    assert len(calls) == 2


# --- the rewritten queries give the same answers --------------------------------

def _old_facets(conn, limit=40, max_visibility=1):
    """The four separate queries facets used to run, for comparison."""
    guard = ("root = ? AND trashed=0 AND " + db.visibility_clause("", max_visibility))
    base = [ROOT, max_visibility]
    counts = {}
    for (raw,) in conn.execute(f"SELECT tags FROM assets WHERE {guard} AND nsfw=0", base):
        for tag in json.loads(raw or "[]"):
            counts[tag] = counts.get(tag, 0) + 1
    folders = conn.execute(f"SELECT folder, COUNT(*) n FROM assets WHERE {guard} "
                           "GROUP BY folder", base).fetchall()
    cameras = conn.execute(f"SELECT camera, COUNT(*) n FROM assets WHERE {guard} "
                           "AND camera IS NOT NULL AND camera != '' GROUP BY camera",
                           base).fetchall()
    years = conn.execute(f"SELECT substr(date_key,1,4) y, COUNT(*) n FROM assets "
                         f"WHERE {guard} AND date_key != '' GROUP BY y ORDER BY y DESC",
                         base).fetchall()
    return (counts, {r[0]: r[1] for r in folders}, {r[0]: r[1] for r in cameras},
            [(r[0], r[1]) for r in years])


def test_one_pass_facets_match_the_four_queries_they_replace(tmp_path):
    conn = _open(tmp_path / "a.db")
    _add(conn, 5, folder="2019/06", tags=("beach", "dog"), camera="Pixel")
    _add(conn, 3, start=10, folder="2020/01", tags=(), camera=None)
    _add(conn, 2, start=20, folder="", tags=("dog",), camera="", date_key="")
    _add(conn, 4, start=30, folder="2021/12", tags=("snow",), camera="EOS",
         date_key="2021-12-24")
    conn.execute("UPDATE assets SET nsfw=1 WHERE id=1"); conn.commit()

    tags, folders, cameras, years = _old_facets(conn)
    new = _facets(conn)
    assert {t["name"]: t["count"] for t in new["tags"]} == tags
    assert {f["name"]: f["count"] for f in new["folders"]} == folders
    assert {c["name"]: c["count"] for c in new["cameras"]} == cameras
    assert [(y["year"], y["count"]) for y in new["years"]] == years


def test_lean_rows_are_what_they_were(tmp_path):
    conn = _open(tmp_path / "a.db")
    user = auth.create_user(conn, "maya", "summerdays24")
    _add(conn, 5)
    db.set_user_asset(conn, user.id, 2, favorite=1, rating=4)
    columns = ("id", "width", "kind", "date_key")
    lean, total = db.query_assets(conn, [ROOT], viewer_id=user.id, columns=columns)
    full, _ = db.query_assets(conn, [ROOT], viewer_id=user.id)
    assert total == 5
    for thin, whole in zip(lean, full):
        assert set(thin) == {*columns, "favorite", "rating"}
        assert {k: whole[k] for k in thin} == thin
        assert isinstance(thin["favorite"], bool) and isinstance(thin["rating"], int)
    assert [r["rating"] for r in lean if r["favorite"]] == [4]


# --- compressed answers ---------------------------------------------------------

def test_a_large_json_answer_is_gzipped_for_a_browser(as_family):
    plain = as_family.get("/api/assets?limit=200")
    zipped = as_family.get("/api/assets?limit=200",
                           headers={"Accept-Encoding": "gzip, deflate, br"})
    assert "Content-Encoding" not in plain.headers
    assert zipped.headers["Content-Encoding"] == "gzip"
    vary = zipped.headers["Vary"]
    assert "Accept-Encoding" in vary and "Cookie" in vary
    assert json.loads(gzip.decompress(zipped.get_data())) == plain.get_json()
    assert int(zipped.headers["Content-Length"]) == len(zipped.get_data())


def test_small_or_refused_answers_are_left_alone(as_family):
    small = as_family.get("/api/me", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in small.headers
    refused = as_family.get("/api/assets?limit=200",
                            headers={"Accept-Encoding": "gzip;q=0, identity"})
    assert "Content-Encoding" not in refused.headers


def test_files_and_streams_are_never_gzipped_again(as_family):
    """Photographs are compressed already, and a stream cannot be read whole."""
    item = as_family.get("/api/assets?limit=1").get_json()["items"][0]
    for url in (f"/api/file/{item['id']}", f"/api/download/zip?ids={item['id']}"):
        response = as_family.get(url, headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200, url
        assert "Content-Encoding" not in response.headers, url
