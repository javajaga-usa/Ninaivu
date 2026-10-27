"""Faces nobody is bothered to name: a stranger in the background, a face in
a crowd. Hiding a group keeps it out of naming without deleting anything,
keeps the same stranger out after the next regroup, and can be undone. And
the groups waiting for a name come a page at a time, so the person you want
is not lost below the first sixty."""

import json
import time

import numpy as np
import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu.media import facematch as fm
from ninaivu.storage import db


D = 128


def near(base, cos, rng):
    noise = rng.normal(0, 1, D)
    noise -= noise.dot(base) * base
    noise /= np.linalg.norm(noise)
    return fm.unit(cos * base + np.sqrt(max(0.0, 1 - cos ** 2)) * noise)


@pytest.fixture()
def rng():
    return np.random.RandomState(4321)


def _asset(conn, cfg):
    return conn.execute("SELECT id FROM assets WHERE root=? ORDER BY id LIMIT 1",
                        (cfg.active_root,)).fetchone()["id"]


def _face(conn, asset_id, *, key=None, vector=None, n=0, person=None, source="none"):
    blob = (vector if vector is not None else np.zeros(D)).astype("float32").tobytes()
    cur = conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, "
        "cluster_key, created_at) VALUES(?,?,?,?,?,0.9,?,?)",
        (asset_id, person, source, json.dumps([n, n, n + 40, n + 40]), blob, key, time.time()))
    conn.commit()
    return int(cur.lastrowid)


def _keys(conn, cfg, **kw):
    return [c["cluster_key"] for c in db.list_unnamed_clusters(conn, [cfg.active_root], **kw)]


def test_a_hidden_group_leaves_the_list_and_comes_back(scanned):
    cfg, conn, _ = scanned
    asset_id = _asset(conn, cfg)
    for n in range(4):
        _face(conn, asset_id, key="stranger", n=n * 50)
        _face(conn, asset_id, key="aunt", n=n * 50 + 1000)
    assert set(_keys(conn, cfg)) == {"stranger", "aunt"}

    assert db.hide_cluster(conn, "stranger") == 4
    assert _keys(conn, cfg) == ["aunt"]
    assert _keys(conn, cfg, hidden=True, min_size=1) == ["stranger"]
    assert db.count_unnamed_clusters(conn, [cfg.active_root]) == 1
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 8, "nothing deleted"

    assert db.show_cluster(conn, "stranger") == 4
    assert set(_keys(conn, cfg)) == {"stranger", "aunt"}
    assert db.hide_cluster(conn, "gone-after-regroup") == 0


def test_bring_them_all_back(scanned):
    cfg, conn, _ = scanned
    asset_id = _asset(conn, cfg)
    for n in range(3):
        _face(conn, asset_id, key="a", n=n * 50)
        _face(conn, asset_id, key="b", n=n * 50 + 1000)
    db.hide_cluster(conn, "a")
    db.hide_cluster(conn, "b")
    assert _keys(conn, cfg) == []
    assert db.show_cluster(conn) == 6
    assert len(_keys(conn, cfg)) == 2


def test_naming_a_hidden_face_unhides_it(scanned):
    from ninaivu.media.faceindex import FaceIndexer

    cfg, conn, _ = scanned
    asset_id = _asset(conn, cfg)
    face = _face(conn, asset_id, key="x")
    db.hide_cluster(conn, "x")
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(7,'Ravi',?)",
                 (time.time(),))
    conn.commit()
    FaceIndexer(cfg).confirm(conn, face, 7)
    assert db.hidden_face_ids(conn) == set()


def test_the_list_pages_past_the_first_groups(scanned):
    cfg, conn, _ = scanned
    asset_id = _asset(conn, cfg)
    for group in range(5):
        for n in range(3 + group):
            _face(conn, asset_id, key=f"g{group}", n=group * 1000 + n * 50)
    first = _keys(conn, cfg, limit=2)
    second = _keys(conn, cfg, limit=2, offset=2)
    third = _keys(conn, cfg, limit=2, offset=4)
    assert first == ["g4", "g3"] and second == ["g2", "g1"] and third == ["g0"]


def test_a_regroup_keeps_the_same_stranger_hidden(scanned, rng):
    """New photographs of somebody hidden join a group made mostly of hidden
    faces; they are hidden too, rather than a fresh group to wave away. A
    hidden face is never given to a named person either."""
    from ninaivu.media.faceindex import FaceIndexer

    cfg, conn, _ = scanned
    asset_id = _asset(conn, cfg)
    stranger, friend = fm.unit(rng.normal(0, 1, D)), fm.unit(rng.normal(0, 1, D))
    old = [_face(conn, asset_id, vector=near(stranger, 0.97, rng), n=n * 50) for n in range(4)]
    for n in range(4):
        _face(conn, asset_id, vector=near(friend, 0.97, rng), n=5000 + n * 50)
    indexer = FaceIndexer(cfg)
    indexer.regroup(conn, [cfg.active_root])
    key = conn.execute("SELECT cluster_key FROM faces WHERE id=?", (old[0],)).fetchone()[0]
    assert key and len(_keys(conn, cfg)) == 2
    db.hide_cluster(conn, key)

    new = [_face(conn, asset_id, vector=near(stranger, 0.97, rng), n=9000 + n * 50)
           for n in range(2)]
    result = indexer.regroup(conn, [cfg.active_root])
    assert set(new) <= db.hidden_face_ids(conn)
    assert len(_keys(conn, cfg)) == 1, "only the friend is waiting for a name"
    assert result["clusters"] == 1

    # Named somebody who looks like the stranger: the hidden faces stay hidden.
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(9,'Lookalike',?)",
                 (time.time(),))
    _face(conn, asset_id, vector=near(stranger, 0.99, rng), n=20000, person=9, source="confirmed")
    indexer.regroup(conn, [cfg.active_root])
    assert conn.execute(
        "SELECT COUNT(*) FROM faces WHERE person_id=9").fetchone()[0] == 1
    assert not [s for s in indexer.suggestions(conn, 9, [cfg.active_root])
                if s["face_id"] in db.hidden_face_ids(conn)]


def test_hiding_survives_a_new_search_for_faces(scanned):
    cfg, conn, _ = scanned
    asset_id = _asset(conn, cfg)
    _face(conn, asset_id, key="k", n=0)
    db.hide_cluster(conn, "k")
    blob = np.zeros(D, dtype="float32").tobytes()
    db.replace_asset_faces(conn, asset_id, [
        {"bbox": [1, 1, 41, 41], "embedding": blob, "quality": 0.9},
        {"bbox": [500, 500, 540, 540], "embedding": blob, "quality": 0.9}], "sface")
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM faces WHERE asset_id=? ORDER BY id", (asset_id,))]
    assert db.hidden_face_ids(conn) == {ids[0]}


def test_the_console_routes(app, people):
    client = login(app.test_client(), *ADMIN)
    client.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    assert client.post("/api/faces/clusters/hide", json={}).status_code == 400
    assert client.post("/api/faces/clusters/hide",
                       json={"cluster_key": "nope"}).status_code == 409
    body = client.get("/api/faces/clusters").get_json()
    assert "more" in body and "hidden_faces" in body
    assert client.post("/api/faces/clusters/show", json={"all": True}).status_code == 200
    family = login(app.test_client(), *FAMILY)
    family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    for path in ("/api/faces/clusters/hide", "/api/faces/clusters/show"):
        assert family.post(path, json={"all": True}).status_code in (401, 403, 404)


def test_only_faces_seen_often_are_waiting_for_a_name(scanned):
    """The console's count: a stranger in a few photographs is listed on the
    Faces page but not counted as waiting; somebody in many photographs is."""
    cfg, conn, _ = scanned
    ids = [r["id"] for r in conn.execute("SELECT id FROM assets WHERE root=? ORDER BY id",
                                         (cfg.active_root,)).fetchall()]
    assert len(ids) >= 3
    asset_id = ids[0]
    for n in range(5):                          # five faces, one photograph
        _face(conn, asset_id, key="stranger", n=n * 50)
    for i, aid in enumerate(ids[:3]):           # in three photographs
        _face(conn, aid, key="aunt", n=i * 50 + 1000)
    assert set(_keys(conn, cfg)) == {"stranger", "aunt"}
    root = [cfg.active_root]
    assert db.count_unnamed_clusters(conn, root) == 2
    assert db.count_unnamed_clusters(conn, root, min_photos=3) == 1
    assert db.count_unnamed_clusters(conn, root, min_photos=4) == 0
