"""The embedding matrix is held in memory, and held correctly.

Searching means multiplying one query vector against every stored vector.
Reading them back out of SQLite for each search was most of the cost — the same
hundred megabytes, per search, on a library big enough to be worth searching.

Holding it is only safe if it is dropped the moment the vectors change, so that
is what most of this file is about.
"""

import numpy as np
import pytest

from ninaivu import ai
from ninaivu.storage import db


@pytest.fixture()
def library(tmp_path):
    """Two assets with one four-dimension vector each."""
    conn = db.init_db(tmp_path / "index.db")
    for n, name in ((1, "a.jpg"), (2, "b.jpg")):
        conn.execute(
            "INSERT INTO assets(id, root, rel_path, filename, folder, ext, kind, "
            "size, mtime, date_key) VALUES(?,?,?,?,'','jpg','picture',1,1,'2026-01-01')",
            (n, "/lib", name, name))
    conn.commit()
    db.store_embedding(conn, 1, "m", 4, np.array([1, 0, 0, 0], "float32").tobytes())
    db.store_embedding(conn, 2, "m", 4, np.array([0, 1, 0, 0], "float32").tobytes())
    return conn


def test_the_matrix_is_built_once(library):
    ids, buffer, dim, index = db.embedding_store(library)
    assert ids == [1, 2] and dim == 4 and len(buffer) == 32
    assert index == {1: 0, 2: 1}

    again, same_buffer, _, _ = db.embedding_store(library)
    assert same_buffer is buffer, "the second search rebuilt the matrix"


def test_a_new_vector_drops_it(library):
    _, buffer, _, _ = db.embedding_store(library)
    library.execute(
        "INSERT INTO assets(id, root, rel_path, filename, folder, ext, kind, size, "
        "mtime, date_key) VALUES(3,'/lib','c.jpg','c.jpg','','jpg','picture',1,1,'2026-01-01')")
    library.commit()
    db.store_embedding(library, 3, "m", 4, np.array([0, 0, 1, 0], "float32").tobytes())

    ids, rebuilt, _, _ = db.embedding_store(library)
    assert ids == [1, 2, 3]
    assert rebuilt != buffer


def test_replacing_a_vector_reaches_the_matrix(library):
    """The row count and the highest id are both unchanged here, which is
    exactly the case those two numbers cannot see on their own."""
    db.embedding_store(library)
    db.store_embedding(library, 2, "m", 4,
                       np.array([0, 0, 0, 1], "float32").tobytes())
    ids, buffer, dim, index = db.embedding_store(library)
    matrix = np.frombuffer(buffer, "float32").reshape(len(ids), dim)
    assert matrix[index[2]].tolist() == [0, 0, 0, 1], "search still sees the old vector"


def _add(conn, asset_id, vector):
    conn.execute(
        "INSERT INTO assets(id, root, rel_path, filename, folder, ext, kind, size, "
        "mtime, date_key) VALUES(?,'/lib',?,?,'','jpg','picture',1,1,'2026-01-01')",
        (asset_id, f"{asset_id}.jpg", f"{asset_id}.jpg"))
    conn.commit()
    db.store_embedding(conn, asset_id, "m", 4, np.array(vector, "float32").tobytes())


def test_tagging_appends_instead_of_rebuilding(library, monkeypatch):
    """Tagging stores vectors one at a time for hours. Each search in between
    used to read every vector back; new ones are now added to what is held."""
    db.forget_embeddings()
    db.embedding_store(library)
    rebuilds = []
    from ninaivu.storage import embeddings
    real = embeddings._rebuild_embeddings
    monkeypatch.setattr(embeddings, "_rebuild_embeddings",
                        lambda conn, key: (rebuilds.append(key), real(conn, key)))
    held_ids, held_buffer, _, held_index = db.embedding_store(library)

    for asset_id in range(3, 40):             # enough to outgrow the spare room
        _add(library, asset_id, [asset_id, 0, 0, 1])
        ids, buffer, dim, index = db.embedding_store(library)
        matrix = np.frombuffer(buffer, "float32").reshape(len(ids), dim)
        assert ids[-1] == asset_id and matrix[index[asset_id]][0] == asset_id

    assert rebuilds == [], f"{len(rebuilds)} full rebuilds during tagging"
    assert ids == list(range(1, 40))
    # What an earlier search was handed did not change under it.
    assert held_ids == [1, 2] and len(held_buffer) == 32 and set(held_index) == {1, 2}


def test_a_vector_filed_below_the_end_rebuilds_correctly(library):
    db.embedding_store(library)
    _add(library, 10, [0, 0, 1, 0])
    db.embedding_store(library)
    _add(library, 5, [0, 1, 1, 0])            # lower id than one already held
    ids, buffer, dim, index = db.embedding_store(library)
    assert ids == [1, 2, 5, 10]
    matrix = np.frombuffer(buffer, "float32").reshape(len(ids), dim)
    assert matrix[index[5]].tolist() == [0, 1, 1, 0]


def test_two_databases_do_not_share_a_matrix(library, tmp_path):
    """Same count, same highest id, different vectors."""
    db.embedding_store(library)
    other = db.init_db(tmp_path / "other" / "index.db")
    for n in (1, 2):
        other.execute(
            "INSERT INTO assets(id, root, rel_path, filename, folder, ext, kind, size, "
            "mtime, date_key) VALUES(?,'/lib',?,?,'','jpg','picture',1,1,'2026-01-01')",
            (n, f"{n}.jpg", f"{n}.jpg"))
    other.commit()
    db.store_embedding(other, 1, "m", 4, np.array([9, 9, 9, 9], "float32").tobytes())
    db.store_embedding(other, 2, "m", 4, np.array([8, 8, 8, 8], "float32").tobytes())
    ids, buffer, dim, index = db.embedding_store(other)
    matrix = np.frombuffer(buffer, "float32").reshape(len(ids), dim)
    assert matrix[index[1]].tolist() == [9, 9, 9, 9]


def test_a_deleted_asset_leaves_the_matrix(library):
    _, buffer, _, _ = db.embedding_store(library)
    library.execute("DELETE FROM assets WHERE id=2")
    library.commit()
    ids, rebuilt, _, _ = db.embedding_store(library)
    assert ids == [1], "the embedding outlived its asset"
    assert rebuilt != buffer


def test_ranking_is_unchanged(library):
    ids, buffer, dim, _ = db.embedding_store(library)
    ranked = ai.semantic_search(np.array([1, 0, 0, 0], "float32"), ids, buffer, dim)
    assert ranked[0][0] == 1

    # The shape the older callers and the tests use still works.
    loose = ai.semantic_search(np.array([1, 0, 0, 0], "float32"), [1, 2],
                               [np.array([1, 0, 0, 0], "float32").tobytes(),
                                np.array([0, 1, 0, 0], "float32").tobytes()], 4)
    assert loose[0][0] == 1


def test_rows_narrow_the_answer(library):
    """What a viewer may be told about is a separate question from what is in
    the matrix, and it is answered by picking rows out of it."""
    ids, buffer, dim, index = db.embedding_store(library)

    # Asset 2's own vector, restricted to asset 2's row: it comes back.
    mine = ai.semantic_search(np.array([0, 1, 0, 0], "float32"), ids, buffer,
                              dim, rows=[index[2]])
    assert [i for i, _ in mine] == [2], mine

    # Asset 1's vector, restricted to asset 2's row: asset 1 is the best match
    # in the matrix and must not come back anyway.
    theirs = ai.semantic_search(np.array([1, 0, 0, 0], "float32"), ids, buffer,
                                dim, rows=[index[2]])
    assert [i for i, _ in theirs] == [], theirs

    assert ai.semantic_search(np.array([1, 0, 0, 0], "float32"), ids, buffer,
                              dim, rows=[]) == []


def test_visible_ids_never_read_a_vector(library):
    """The id query is what runs per viewer, so it must stay cheap."""
    assert sorted(db.visible_embedding_ids(library, "/lib")) == [1, 2]
    library.execute("UPDATE assets SET visibility=2 WHERE id=2")   # hidden
    library.commit()
    assert db.visible_embedding_ids(library, "/lib", max_visibility=1) == [1]


def test_the_cache_check_does_not_read_the_vectors(library):
    """The check runs before every AI search. Counting the table itself walks
    every vector's page; a narrow index answers the same count."""
    plan = " | ".join(r[3] for r in library.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) n, COALESCE(MAX(asset_id),0) hi FROM embeddings"))
    assert "COVERING INDEX" in plan, plan
