"""Regression tests for the code review of 28 September 2026.

See docs/audits/2026-09-28-review/REPORT.md. Findings with a home of their own
are tested there: the start-up deadlock in test_known_defects.py, the cloud
upload that died on one unreadable file in test_cloud_sync.py, the mDNS name
hand-back in test_discovery.py. What is here cuts across routes or has no
older file to sit in.
"""

import json
import sqlite3
import time

import pytest

from conftest import ADMIN, FAMILY, ids_of, login
from ninaivu.storage import db, recycle


def post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


@pytest.fixture()
def admin(app, people):
    client = login(app.test_client(), *ADMIN)
    client.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    return client


@pytest.fixture()
def family(app, people):
    client = login(app.test_client(), *FAMILY)
    client.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    return client


def hide(client, ids):
    response = client.post("/api/visibility",
                           json={"ids": list(ids), "visibility": "hidden"})
    assert response.status_code == 200, response.get_json()
    return ids


# -- an album made twice came back as somebody else's -------------------------

def test_making_an_album_with_a_name_already_taken_returns_that_album(scanned):
    """``lastrowid`` after ``ON CONFLICT DO NOTHING`` is whatever this
    connection inserted last — into any table. Between the two "Holiday"s
    below, that is a scan run."""
    _, conn, _ = scanned
    first = db.create_album(conn, "Holiday")
    for _ in range(3):
        db.start_scan_run(conn, "full")
    again = db.create_album(conn, "Holiday")
    assert again == first, f"the second 'Holiday' came back as album {again}, not {first}"


def test_posting_an_album_name_twice_does_not_put_photos_in_another_album(admin, people):
    conn = people["conn"]
    mine = admin.post("/api/albums", json={"name": "Mine"}).get_json()["id"]
    theirs = admin.post("/api/albums", json={"name": "Theirs"}).get_json()["id"]
    assert theirs != mine
    photo = ids_of(admin)[0]
    answer = admin.post("/api/albums", json={"name": "Mine", "ids": [photo]})
    assert answer.status_code < 500, answer.get_json()
    in_theirs = conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=?",
                             (theirs,)).fetchone()[0]
    assert in_theirs == 0, "the photograph went into the other album"


def test_upserting_an_asset_a_second_time_returns_its_own_id(scanned):
    _, conn, _ = scanned
    row = conn.execute("SELECT * FROM assets ORDER BY id LIMIT 1").fetchone()
    record = dict(row)
    for _ in range(2):
        db.start_scan_run(conn, "full")
    assert db.upsert_asset(conn, record) == row["id"]


# -- restoring from the bin lost the faces and the search vector --------------

def _face(conn, asset_id, person_id=None, source="none"):
    conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, confidence, bbox, landmarks, "
        "det_score, sharpness, quality, embedding, thumb, model, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset_id, person_id, source, 1.0 if person_id else 0.0, "[0,0,10,10]", "[]",
         0.9, 0.5, 0.5, b"\x00" * 16, None, "test", time.time()))
    conn.commit()


def _bin_entry_for(conn, asset_id):
    return conn.execute("SELECT id FROM recycled WHERE asset_id=? AND restored_at IS NULL",
                        (asset_id,)).fetchone()[0]


def test_a_restored_photograph_keeps_its_named_faces_and_its_vector(admin, people):
    conn = people["conn"]
    target = hide(admin, ids_of(admin)[:1])[0]
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES (7, 'Gran', ?)",
                 (time.time(),))
    _face(conn, target, person_id=7, source="confirmed")
    _face(conn, target)
    conn.execute("INSERT INTO embeddings(asset_id, model, dim, vector) VALUES (?,?,?,?)",
                 (target, "test", 4, b"\x01\x02\x03\x04"))
    conn.execute("UPDATE assets SET ai_version=7, face_version=5 WHERE id=?", (target,))
    conn.commit()

    assert admin.post("/api/delete", json={"ids": [target],
                                           "password": ADMIN[1]}).status_code == 200
    result = recycle.restore(conn, [_bin_entry_for(conn, target)])
    assert result["restored"] == 1, result

    back = conn.execute("SELECT id, ai_version, face_version FROM assets "
                        "WHERE rel_path=(SELECT rel_path FROM recycled WHERE asset_id=?)",
                        (target,)).fetchone()
    faces = conn.execute("SELECT person_id, source FROM faces WHERE asset_id=? "
                         "ORDER BY person_id IS NULL", (back["id"],)).fetchall()
    assert [(f["person_id"], f["source"]) for f in faces] == [(7, "confirmed"), (None, "none")], (
        "the faces did not come back with the photograph, or lost their names")
    vector = conn.execute("SELECT vector FROM embeddings WHERE asset_id=?",
                          (back["id"],)).fetchone()
    assert vector and bytes(vector["vector"]) == b"\x01\x02\x03\x04"
    assert (back["ai_version"], back["face_version"]) == (7, 5), (
        "with everything back there is nothing for the scanner to redo")


def test_a_face_whose_person_was_merged_away_comes_back_unnamed(admin, people):
    conn = people["conn"]
    target = hide(admin, ids_of(admin)[:1])[0]
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES (8, 'Uncle', ?)",
                 (time.time(),))
    _face(conn, target, person_id=8, source="confirmed")
    conn.commit()
    admin.post("/api/delete", json={"ids": [target], "password": ADMIN[1]})
    conn.execute("DELETE FROM people_clusters WHERE id=8")
    conn.commit()

    assert recycle.restore(conn, [_bin_entry_for(conn, target)])["restored"] == 1
    face = conn.execute("SELECT person_id, source FROM faces").fetchone()
    assert face is not None, "the face itself was lost"
    assert (face["person_id"], face["source"]) == (None, "none")


def test_an_entry_binned_before_faces_were_kept_is_re_indexed(admin, people):
    """Older bin entries hold no faces or vector. Their stamps must not say
    the work is done, or the scanner never does it again."""
    conn = people["conn"]
    target = hide(admin, ids_of(admin)[:1])[0]
    conn.execute("UPDATE assets SET ai_version=7, face_version=5 WHERE id=?", (target,))
    conn.commit()
    admin.post("/api/delete", json={"ids": [target], "password": ADMIN[1]})
    entry = _bin_entry_for(conn, target)
    saved = json.loads(conn.execute("SELECT metadata FROM recycled WHERE id=?",
                                    (entry,)).fetchone()["metadata"])
    for table in ("faces", "embeddings"):
        saved["relations"].pop(table, None)
    conn.execute("UPDATE recycled SET metadata=? WHERE id=?", (json.dumps(saved), entry))
    conn.commit()

    assert recycle.restore(conn, [entry])["restored"] == 1
    back = conn.execute("SELECT ai_version, face_version FROM assets "
                        "WHERE rel_path=(SELECT rel_path FROM recycled WHERE id=?)",
                        (entry,)).fetchone()
    assert (back["ai_version"], back["face_version"]) == (0, 0)


# -- ids that look like digits but are not, and ids too large for SQLite ------

@pytest.mark.parametrize("bad", ["²", "99999999999999999999", "٣"])
def test_an_id_that_is_not_one_is_ignored_on_the_asset_list(family, bad):
    answer = family.get(f"/api/assets?ids={bad}")
    assert answer.status_code == 200, answer.get_data(as_text=True)
    assert answer.get_json()["items"] == []


def test_a_real_id_beside_a_bad_one_is_still_served(family):
    photo = ids_of(family)[0]
    answer = family.get(f"/api/assets?ids={photo},²,99999999999999999999")
    assert answer.status_code == 200
    assert [item["id"] for item in answer.get_json()["items"]] == [photo]


def test_no_valid_ids_is_nothing_not_the_first_page(family):
    """``?ids=abc`` asked for particular items and none of them exist; it must
    not fall through to a page of everything."""
    assert family.get("/api/assets?ids=abc").get_json()["items"] == []


@pytest.mark.parametrize("bad", ["²", "99999999999999999999"])
def test_zip_download_refuses_ids_that_are_not_ones(family, bad):
    answer = family.get(f"/api/download/zip?ids={bad}")
    assert answer.status_code == 400, answer.status_code


@pytest.mark.parametrize("url", ["/api/assets/bulk", "/api/delete",
                                 "/api/recycle/restore", "/api/recycle/purge"])
@pytest.mark.parametrize("bad", ["²", "99999999999999999999", 2 ** 63, -1, 0])
def test_bad_ids_in_a_body_are_passed_over_not_a_500(admin, url, bad):
    answer = post(admin, url, {"ids": [bad], "favorite": True, "password": ADMIN[1]})
    assert answer.status_code < 500, answer.get_json()


@pytest.mark.parametrize("bad", ["²", "99999999999999999999"])
def test_the_admin_preview_takes_a_bad_person_id_as_no_person(admin, bad):
    answer = admin.get(f"/api/admin/preview?person={bad}")
    assert answer.status_code < 500, answer.get_data(as_text=True)


# -- Infinity is a number json.loads accepts and int() refuses ----------------

@pytest.mark.parametrize("body", ['{"rotation": Infinity}', '{"rotation": 1e400}',
                                  '{"rotation": -Infinity}', '{"rotation": NaN}'])
def test_an_infinite_rotation_is_a_400_on_the_family_route(family, body):
    photo = ids_of(family)[0]
    answer = family.post(f"/api/asset/{photo}/rotate", data=body,
                         content_type="application/json")
    assert answer.status_code == 400, answer.status_code


def test_an_infinite_rotation_is_a_400_on_the_bulk_route(admin):
    photo = ids_of(admin)[0]
    answer = admin.post("/api/rotate", data='{"rotation": Infinity, "ids": [%d]}' % photo,
                        content_type="application/json")
    assert answer.status_code == 400, answer.status_code


def test_infinite_keyframes_are_a_400(admin):
    answer = admin.post("/api/admin/settings", data='{"video_keyframes": Infinity}',
                        content_type="application/json")
    assert answer.status_code == 400, answer.status_code


# -- unbounded id lists went into one IN (...) --------------------------------

def test_an_album_request_with_too_many_ids_is_refused_not_a_500(family):
    answer = post(family, "/api/albums", {"name": "big", "ids": list(range(1, 5002))})
    assert answer.status_code == 400, answer.status_code


def test_reading_an_album_far_larger_than_sqlite_takes_in_one_query(admin, people,
                                                                    monkeypatch):
    """Members added a few thousand at a time can pass any per-request cap.
    Reading the album then bound every id at once; past SQLite's limit the
    album could not be opened at all."""
    from ninaivu.api import api as api_mod
    conn = people["conn"]
    album = admin.post("/api/albums", json={"name": "Everything"}).get_json()["id"]
    real = ids_of(admin)
    root = conn.execute("SELECT root FROM assets LIMIT 1").fetchone()["root"]
    # Index rows for files that are not there; the query's shape is the point.
    made = []
    for i in range(1200):
        record = db._normalise({"root": root, "rel_path": f"many/{i}.jpg",
                                "filename": f"{i}.jpg", "kind": "picture",
                                "visibility": 2, "vis_source": "item"})
        record.pop("nsfw_given")      # a bound parameter of the upsert, not a column
        cols = list(record)
        made.append(conn.execute(
            f"INSERT INTO assets ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [record[c] for c in cols]).lastrowid)
    conn.executemany("INSERT INTO album_items(album_id, asset_id, added_at) VALUES (?,?,?)",
                     [(album, i, time.time()) for i in made + real])
    conn.commit()

    # Stand in for a stock build's variable limit, far above this suite's data.
    original = db.query_assets

    def strict(conn, roots, *args, **kwargs):
        if len(kwargs.get("ids") or []) > 1000:
            raise sqlite3.OperationalError("too many SQL variables")
        return original(conn, roots, *args, **kwargs)

    monkeypatch.setattr(api_mod.db, "query_assets", strict)
    answer = admin.get(f"/api/albums/{album}")
    assert answer.status_code == 200, answer.get_data(as_text=True)
    got = set(answer.get_json()["album"]["item_ids"])
    assert got >= set(real), "the album's real photographs were not all read"
    assert len(got) == len(made) + len(real), "some of the album was left out"


# -- the rate limiter could be flushed with throwaway keys ---------------------

def test_a_flood_of_one_shot_keys_does_not_free_a_paused_profile():
    """When the table is full of live keys, half go. It used to be the oldest
    half by last attempt — which is exactly where a paused profile's counter
    sits, since it was filled *before* the flood. A few thousand cheap requests
    then unlocked the PIN guessing again."""
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS.clear()
    accounts_api._last_sweep = 0.0
    paused = "*|profile:3"
    for _ in range(accounts_api._PROFILE_MAX_ATTEMPTS):
        accounts_api.record_attempt(paused)
    assert accounts_api.rate_limited(paused, accounts_api._PROFILE_MAX_ATTEMPTS,
                                     accounts_api._PROFILE_WINDOW)
    for i in range(accounts_api._MAX_KEYS + 10):
        accounts_api.record_attempt(f"10.0.0.9|profile:{900000 + i}")
    assert accounts_api.rate_limited(paused, accounts_api._PROFILE_MAX_ATTEMPTS,
                                     accounts_api._PROFILE_WINDOW), (
        "the flood pushed the paused profile's counter out of the table")
    accounts_api._ATTEMPTS.clear()


def test_a_pin_for_a_profile_that_does_not_exist_mints_no_limiter_key(app, people):
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS.clear()
    client = app.test_client()
    for i in range(5):
        client.post("/api/auth/enter", json={"id": 900000 + i, "secret": "0000"})
    minted = [k for k in accounts_api._ATTEMPTS if "profile:9000" in k]
    assert minted == [], f"keys were made for profiles that are nobody's: {minted}"
    accounts_api._ATTEMPTS.clear()


def test_a_wrong_pin_for_a_real_profile_is_still_counted(app, people):
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS.clear()
    family_id = people["family"].id
    app.test_client().post("/api/auth/enter", json={"id": family_id, "secret": "0000"})
    assert f"*|profile:{family_id}" in accounts_api._ATTEMPTS
    accounts_api._ATTEMPTS.clear()


# -- the storage check could wedge itself "running" --------------------------

def test_a_storage_check_that_fails_to_start_can_be_started_again(scanned, monkeypatch):
    """Its running flag is set by the caller and cleared in the thread's
    ``finally`` — which did not cover the set-up. A busy database there left
    the flag up, and every later Start said "already running" until a restart."""
    import threading
    from ninaivu.api import admin_api

    cfg, _, _ = scanned

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(admin_api.db, "connect", busy)
    assert admin_api.start_scrubber_job(cfg.db_path) is True
    deadline = time.time() + 5
    while time.time() < deadline and admin_api._SCRUBBER_RUNNING:
        time.sleep(0.05)
    assert not admin_api._SCRUBBER_RUNNING, "the flag stayed up after the thread died"
    assert not admin_api._SCRUBBER_PROGRESS.get("running")

    monkeypatch.undo()
    assert admin_api.start_scrubber_job(cfg.db_path) is True, "could not start again"
    for thread in threading.enumerate():
        if thread.name == "ninaivu-scrubber":
            thread.join(10)


# -- config.json: a damaged file was ignored and then overwritten --------------

def test_a_damaged_config_is_set_aside_not_silently_replaced(tmp_path, monkeypatch):
    """A truncated config.json read as "nothing set", and the first save after
    that replaced it with the defaults — the roots, the Drive backup and the
    mail settings gone with it. The damaged file is kept beside a fresh one."""
    from ninaivu.server.config import Config

    state = tmp_path / "state"
    state.mkdir()
    (state / "config.json").write_text('{"roots": ["/lib"], "house_name": "Us"', encoding="utf-8")
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(state))

    cfg = Config.load()
    assert cfg.roots == []
    kept = [p for p in state.iterdir() if p.name.startswith("config.json.broken-")]
    assert len(kept) == 1, f"the damaged file was not kept: {sorted(state.iterdir())}"
    assert '"house_name": "Us"' in kept[0].read_text(encoding="utf-8")
    cfg.save()
    assert kept[0].exists(), "saving fresh settings destroyed the damaged original"


@pytest.mark.parametrize("key", ["db_path", "libraries", "save", "config_path"])
def test_a_config_key_named_like_a_property_or_method_is_ignored(tmp_path, monkeypatch, key):
    """``hasattr`` was the test, and it is true of properties and methods too:
    a hand-edited key with one of those names raised at start, or replaced a
    method with a string that failed at the first save."""
    from ninaivu.server.config import Config

    state = tmp_path / "state"
    state.mkdir()
    (state / "config.json").write_text(json.dumps({key: ["/typo"], "house_name": "Us"}),
                                       encoding="utf-8")
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(state))

    cfg = Config.load()
    assert cfg.house_name == "Us"
    cfg.save()                     # still a method; still a path
    assert cfg.config_path == state / "config.json"


def test_a_config_that_is_not_an_object_is_ignored(tmp_path, monkeypatch):
    from ninaivu.server.config import Config

    state = tmp_path / "state"
    state.mkdir()
    (state / "config.json").write_text('["/lib"]', encoding="utf-8")
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(state))
    assert Config.load().roots == []


# -- two requests could take the last two administrators away at once ---------

def test_two_demotions_at_once_leave_one_administrator(app, people):
    """Each request counted the administrators at its top and wrote later. Two
    at the same moment both saw two, both went through, and the household had
    none — which reopens the first-run setup to anyone on the network."""
    import threading
    from ninaivu.server import auth
    from ninaivu.api import accounts_api

    conn = people["conn"]
    admin = people["admin"]
    other = auth.create_user(conn, "mum", "anotherpass99", display_name="Mum",
                             role=auth.ROLE_ADMIN, created_by=admin.id)
    assert auth.count_active_admins(conn) == 2

    # Hold both requests at the point where they have read the count and are
    # about to write, then let them go together.
    gate = threading.Barrier(2, timeout=3)
    real = accounts_api.auth.count_active_admins

    def counted_then_wait(c):
        n = real(c)
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return n

    accounts_api.auth.count_active_admins = counted_then_wait
    results = {}
    try:
        def demote(who, me, target):
            client = login(app.test_client(), *me)
            client.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
            results[who] = client.post(f"/api/people/{target}",
                                       json={"role": "family"}).status_code

        threads = [threading.Thread(target=demote, args=("a", ADMIN, other.id)),
                   threading.Thread(target=demote, args=("b", ("mum", "anotherpass99"), admin.id))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
    finally:
        accounts_api.auth.count_active_admins = real

    assert auth.count_active_admins(conn) >= 1, f"no administrator is left: {results}"
    assert sorted(results.values()) == [200, 409], results
