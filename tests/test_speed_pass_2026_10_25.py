"""The pages that were slow on a family library of 210,000 photographs, made
fast without answering differently (tools/bench_app.py measures them).

Duplicates sends what its review draws and nothing else; a person's review
queue is scored in one multiplication; the groups waiting for a name, and the
"who is this?" picker, are kept until a face, a group or what is hidden from
naming moves; exact copies are judged with a few statements, not a few each.
"""

import json
import time

import numpy as np

from conftest import ADMIN, login
from ninaivu.media import facematch as fm
from ninaivu.storage import db, exact_copies, fingerprints


D = 128


def _asset(conn, cfg, n=0):
    return conn.execute("SELECT id FROM assets WHERE root=? ORDER BY id LIMIT 1 OFFSET ?",
                        (cfg.active_root, n)).fetchone()["id"]


def _face(conn, asset_id, *, key=None, vector=None, n=0, person=None, source="none"):
    blob = (vector if vector is not None else np.ones(D)).astype("float32").tobytes()
    cur = conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, "
        "cluster_key, created_at) VALUES(?,?,?,?,?,0.9,?,?)",
        (asset_id, person, source, json.dumps([n, n, n + 40, n + 40]), blob, key, time.time()))
    conn.commit()
    return int(cur.lastrowid)


# -- Duplicates ---------------------------------------------------------------

def test_duplicates_send_what_the_review_draws(as_family):
    data = as_family.get("/api/duplicates").get_json()
    items = [i for g in data["groups"] for i in g["items"]]
    assert {"shot0.jpg", "shot0_copy.jpg"} <= {i["name"] for i in items}
    for item in items:
        assert {"id", "name", "width", "height", "size_h", "thumb_v"} <= set(item)
        # Not every member's whole record: no EXIF, tags, place or paths.
        assert not {"tags", "camera", "gps", "src", "caption"} & set(item)
    for group in data["groups"]:
        assert group["best"] in [i["id"] for i in group["items"]]
    assert data["total_wasted"] > 0


def test_a_favourite_is_still_the_keeper(as_family, scanned):
    groups = as_family.get("/api/duplicates").get_json()["groups"]
    group = groups[0]
    other = next(i["id"] for i in group["items"] if i["id"] != group["best"])
    assert as_family.post(f"/api/asset/{other}", json={"favorite": True}).status_code == 200
    again = {g["group"]: g for g in as_family.get("/api/duplicates").get_json()["groups"]}
    assert again[group["group"]]["best"] == other


# -- A person's review queue --------------------------------------------------

def test_the_queue_is_ranked_as_one_face_at_a_time_ranked_it():
    rng = np.random.RandomState(7)
    centre = fm.unit(rng.normal(0, 1, D))
    person = fm.Person(person_id=3, name="Amma", centroid=centre)
    vectors = rng.normal(0, 1, (300, D)).astype("float32")
    vectors[:40] += 4 * centre                 # some of them are her
    ids = list(range(1000, 1300))
    blocked = {(1001, 3), (1002, 9)}           # not her; another person's "no"
    ranked = fm.rank_for_person(person, ids, vectors, floor=0.3, blocked=blocked)

    expected = sorted(((i, float(np.dot(centre, fm.unit(v)))) for i, v in zip(ids, vectors)
                       if float(np.dot(centre, fm.unit(v))) >= 0.3 and i != 1001),
                      key=lambda p: -p[1])
    assert [i for i, _ in ranked] == [i for i, _ in expected]
    assert all(abs(a - b) < 1e-5 for (_, a), (_, b) in zip(ranked, expected))
    assert 1001 not in dict(ranked) and 1002 in dict(ranked)


def test_a_face_of_another_length_is_left_out(monkeypatch):
    centre = fm.unit(np.ones(D))
    person = fm.Person(person_id=1, name="x", centroid=centre)
    assert fm.rank_for_person(person, [1], np.ones((1, 512), "float32")) == []
    assert fm.rank_for_person(person, [], np.zeros((0, D), "float32")) == []


# -- Groups waiting for a name, kept until they move ---------------------------

def _keys(conn, cfg):
    return [c["cluster_key"] for c in db.list_unnamed_clusters(conn, [cfg.active_root])]


def test_the_groups_follow_a_regroup_and_a_hide(scanned):
    cfg, conn, _ = scanned
    asset_id = _asset(conn, cfg)
    faces = [_face(conn, asset_id, key="g1", n=n * 50) for n in range(3)]
    assert _keys(conn, cfg) == ["g1"]
    assert _keys(conn, cfg) == ["g1"]          # kept

    # A regroup reissues the keys; nothing about who the faces are moves.
    db.save_cluster_keys(conn, [(f, "g2") for f in faces])
    assert _keys(conn, cfg) == ["g2"]

    # Written straight to the table (another process's regroup) is seen too.
    conn.execute("UPDATE faces SET cluster_key='g3'")
    conn.commit()
    assert _keys(conn, cfg) == ["g3"]

    conn.execute("INSERT INTO faces_ignored(face_id, created_at) SELECT id, 0 FROM faces")
    conn.commit()
    assert _keys(conn, cfg) == []
    conn.execute("DELETE FROM faces_ignored")
    conn.commit()
    assert _keys(conn, cfg) == ["g3"]


def test_the_picker_follows_a_hide(app, people, scanned):
    cfg, conn, _ = scanned
    for n in range(4):
        _face(conn, _asset(conn, cfg), key="cousin", n=n * 50)
    conn.execute("UPDATE faces SET thumb='x.webp'")
    conn.commit()
    client = login(app.test_client(), *ADMIN)
    first = client.get("/api/ask-family/faces").get_json()["faces"]
    assert [f["size"] for f in first] == [4]
    db.hide_cluster(conn, "cousin")
    assert client.get("/api/ask-family/faces").get_json()["faces"] == []


# -- Exact copies --------------------------------------------------------------

def _same_bytes(conn, ids, digest="d" * 64):
    for asset_id in ids:
        size, mtime = conn.execute("SELECT size, mtime FROM assets WHERE id=?",
                                   (asset_id,)).fetchone()
        conn.execute("INSERT OR REPLACE INTO content_fingerprints(asset_id, algo, digest, "
                     "size, mtime, taken_at) VALUES (?,?,?,?,?,0)",
                     (asset_id, fingerprints.ALGO, digest, size, mtime))
        conn.execute("UPDATE assets SET size=? WHERE id=?", (size, asset_id))
    conn.commit()


def test_exact_copies_are_judged_together(scanned):
    cfg, conn, _ = scanned
    ids = [_asset(conn, cfg, n) for n in range(4)]
    size = conn.execute("SELECT size FROM assets WHERE id=?", (ids[0],)).fetchone()[0]
    conn.executemany("UPDATE assets SET size=? WHERE id=?", [(size, i) for i in ids])
    conn.commit()
    _same_bytes(conn, ids[:2], "a" * 64)
    _same_bytes(conn, ids[2:], "b" * 64)
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (ids[3],))
    conn.commit()

    groups = fingerprints.duplicate_groups(conn)
    assert sorted(sorted(f["id"] for f in g["files"]) for g in groups) == \
        [sorted(ids[:2]), sorted(ids[2:])]

    plan = {g["fingerprint"]: g for g in exact_copies.plan(conn, [cfg.active_root])["groups"]}
    assert plan["a" * 64]["left_alone"] is None
    assert len(plan["a" * 64]["set_aside"]) == 1
    assert plan["b" * 64]["left_alone"] == "seen_differently"


def test_companions_are_found_for_each_photograph(scanned):
    cfg, conn, _ = scanned
    still, clip = _asset(conn, cfg, 0), _asset(conn, cfg, 1)
    rel = conn.execute("SELECT rel_path FROM assets WHERE id=?", (clip,)).fetchone()[0]
    conn.execute("UPDATE assets SET is_live=1, live_video_path=? WHERE id=?", (rel, still))
    conn.execute("UPDATE assets SET live_clip=1 WHERE id=?", (clip,))
    conn.commit()
    assert db.companion_map(conn, [still, clip]) == {still: [clip]}
    assert db.with_companions(conn, [still]) == [still, clip]
