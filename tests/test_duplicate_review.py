"""The duplicates review, side by side: exact copies (the same fingerprint)
and near ones (the same look) as one list, read through the viewer's own
limits; an administrator keeps one and hides or merges the others, or says
they are not duplicates; every decision can be put back; no file is touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ninaivu.storage import db, duplicate_review, fingerprints, migrations


# --- which files are one group ----------------------------------------------------------------

def test_look_and_fingerprint_together_make_one_group():
    rows = [{"id": 1, "dup_group": "a", "digest": None},
            {"id": 2, "dup_group": "a", "digest": "x"},
            # The same bytes as 2, never hashed for its look: still one group.
            {"id": 3, "dup_group": None, "digest": "x"},
            {"id": 4, "dup_group": None, "digest": "y"},
            {"id": 5, "dup_group": "b", "digest": None}]
    groups = duplicate_review.link(rows)
    assert [[r["id"] for r in g] for g in groups] == [[1, 2, 3]]


def test_the_tables_come_with_a_numbered_migration(scanned):
    _, conn, _ = scanned
    assert migrations.recorded(conn) >= 4
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"duplicate_reviews", "duplicate_undo"} <= names


# --- in the library ---------------------------------------------------------------------------

@pytest.fixture()
def dupes(app, people):
    """shot0 and shot1 the same bytes by fingerprint; shot2, shot3 and shot4
    alike by look; secret0 alike shot5, but hidden."""
    conn = people["conn"]
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}
    with db._write_lock:                                                    # noqa: SLF001
        conn.execute("UPDATE assets SET dup_group=NULL, nsfw=0")
        for name in ("shot0.jpg", "shot1.jpg"):
            row = conn.execute("SELECT size, mtime FROM assets WHERE id=?",
                               (ids[name],)).fetchone()
            conn.execute("INSERT OR REPLACE INTO content_fingerprints(asset_id, algo, digest, "
                         "size, mtime) VALUES(?,?,?,?,?)",
                         (ids[name], fingerprints.ALGO, "same-bytes", row[0], row[1]))
        for name in ("shot2.jpg", "shot3.jpg", "shot4.jpg"):
            conn.execute("UPDATE assets SET dup_group='look', visibility=1 WHERE id=?",
                         (ids[name],))
        for name in ("shot5.jpg", "secret0.jpg"):
            conn.execute("UPDATE assets SET dup_group='hid', visibility=1 WHERE id=?",
                         (ids[name],))
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (ids["secret0.jpg"],))
        conn.commit()
    return {"conn": conn, "ids": ids, "root": app.config["MV_CONFIG"].active_root}


def _groups(client, query=""):
    response = client.get(f"/api/duplicates/review{query}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _members(body):
    return sorted(sorted(i["name"] for i in g["items"]) for g in body["groups"])


def test_exact_and_near_copies_are_one_list_each_saying_which(as_admin, dupes):
    body = _groups(as_admin)
    kinds = {tuple(sorted(i["name"] for i in g["items"])): g["kind"] for g in body["groups"]}
    assert kinds[("shot0.jpg", "shot1.jpg")] == "exact"
    assert kinds[("shot2.jpg", "shot3.jpg", "shot4.jpg")] == "near"
    assert body["counts"] == {"exact": 1, "near": 2} and body["can_decide"] is True
    exact = _groups(as_admin, "?kind=exact")
    assert _members(exact) == [["shot0.jpg", "shot1.jpg"]]
    assert all(i["same_bytes"] for i in exact["groups"][0]["items"])
    # The best is drawn first.
    for g in body["groups"]:
        assert g["items"][0]["id"] == g["best"]


def test_a_family_member_never_learns_of_a_hidden_or_explicit_copy(as_family, as_admin, dupes):
    conn, ids = dupes["conn"], dupes["ids"]
    assert ["secret0.jpg", "shot5.jpg"] in _members(_groups(as_admin))
    seen = _groups(as_family)
    assert ["secret0.jpg", "shot5.jpg"] not in _members(seen)
    assert all("visibility" not in i for g in seen["groups"] for i in g["items"])
    assert seen["can_decide"] is False
    with db._write_lock:                                                    # noqa: SLF001
        conn.execute("UPDATE assets SET nsfw=1 WHERE id=?", (ids["shot1.jpg"],))
        conn.commit()
    assert ["shot0.jpg", "shot1.jpg"] not in _members(_groups(as_family, "?kind=exact"))
    # The administrator sees everything, the explicit copy too.
    assert ["shot0.jpg", "shot1.jpg"] in _members(_groups(as_admin, "?kind=exact"))


def test_only_an_administrator_decides(as_family, as_guest, dupes):
    ids = dupes["ids"]
    body = {"keep": ids["shot0.jpg"], "others": [ids["shot1.jpg"]], "action": "hide"}
    assert as_family.post("/api/duplicates/review/decide", json=body).status_code == 403
    assert as_guest.post("/api/duplicates/review/decide", json=body).status_code == 403
    assert as_guest.get("/api/duplicates/review").status_code == 403


def test_photographs_that_are_not_copies_are_refused(as_admin, dupes):
    ids = dupes["ids"]
    got = as_admin.post("/api/duplicates/review/decide", json={
        "keep": ids["shot0.jpg"], "others": [ids["shot2.jpg"]], "action": "hide"})
    assert got.status_code == 409
    assert as_admin.post("/api/duplicates/review/decide", json={
        "keep": ids["shot0.jpg"], "others": [ids["shot1.jpg"]], "action": "burn"}
    ).status_code == 400


def test_merge_carries_marks_and_albums_hides_the_rest_and_puts_back(
        as_admin, as_family, dupes, people):
    conn, ids = dupes["conn"], dupes["ids"]
    keep, other = ids["shot2.jpg"], ids["shot3.jpg"]
    family_id = people["family"].id
    db.set_user_asset(conn, family_id, other, favorite=1, rating=4)
    album = db.create_album(conn, "Beach day")
    album_id = album if isinstance(album, int) else album["id"]
    db.album_add(conn, album_id, [other])
    files = sorted(p.name for p in Path(dupes["root"]).rglob("*.jpg"))

    done = as_admin.post("/api/duplicates/review/decide", json={
        "keep": keep, "others": [other], "action": "merge"}).get_json()
    assert done["carried"] == {"ratings": 1, "albums": 1, "caption": 0}
    row = conn.execute("SELECT visibility, vis_source FROM assets WHERE id=?", (other,)).fetchone()
    assert tuple(row) == (2, duplicate_review.SOURCE)
    mark = conn.execute("SELECT favorite, rating FROM user_assets WHERE user_id=? AND asset_id=?",
                        (family_id, keep)).fetchone()
    assert tuple(mark) == (1, 4)
    assert keep in db.album_asset_ids(conn, album_id)
    # Nothing on disk changed.
    assert sorted(p.name for p in Path(dupes["root"]).rglob("*.jpg")) == files
    # The family no longer sees the hidden copy; the group of three is a
    # group of two still to review.
    assert ["shot2.jpg", "shot4.jpg"] in _members(_groups(as_admin))
    assert as_family.get(f"/api/asset/{other}").status_code == 404

    aside = as_admin.get("/api/duplicates/review/set-aside").get_json()
    assert aside["total"] == 1 and aside["entries"][0]["others"] == [other]

    back = as_admin.post("/api/duplicates/review/put-back",
                         json={"batch": done["batch"]}).get_json()
    assert back == {"found": True, "shown": 1}
    assert conn.execute("SELECT visibility FROM assets WHERE id=?", (other,)).fetchone()[0] == 1
    assert keep not in db.album_asset_ids(conn, album_id)
    assert conn.execute("SELECT COALESCE(SUM(favorite+rating),0) FROM user_assets "
                        "WHERE user_id=? AND asset_id=?", (family_id, keep)).fetchone()[0] == 0
    assert ["shot2.jpg", "shot3.jpg", "shot4.jpg"] in _members(_groups(as_admin))
    assert as_admin.post("/api/duplicates/review/put-back",
                         json={"batch": done["batch"]}).status_code == 404


def test_not_duplicates_leaves_the_list_until_a_new_copy_joins(as_admin, dupes):
    conn, ids = dupes["conn"], dupes["ids"]
    as_admin.post("/api/duplicates/review/decide", json={
        "keep": ids["shot0.jpg"], "others": [ids["shot1.jpg"]], "action": "keep_all"})
    assert ["shot0.jpg", "shot1.jpg"] not in _members(_groups(as_admin))
    assert conn.execute("SELECT visibility FROM assets WHERE id=?",
                        (ids["shot1.jpg"],)).fetchone()[0] == 1
    row = conn.execute("SELECT size, mtime FROM assets WHERE id=?",
                       (ids["secret1.jpg"],)).fetchone()
    with db._write_lock:                                                    # noqa: SLF001
        conn.execute("INSERT OR REPLACE INTO content_fingerprints(asset_id, algo, digest, size, "
                     "mtime) VALUES(?,?,?,?,?)",
                     (ids["secret1.jpg"], fingerprints.ALGO, "same-bytes", row[0], row[1]))
        conn.execute("UPDATE assets SET visibility=1 WHERE id=?", (ids["secret1.jpg"],))
        conn.commit()
    assert ["secret1.jpg", "shot0.jpg", "shot1.jpg"] in _members(_groups(as_admin))
