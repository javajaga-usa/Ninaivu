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
