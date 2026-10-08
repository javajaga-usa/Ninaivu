"""The family tree: parents and spouses between named people.

What has to hold: nobody is their own parent or ancestor, a merge of two
people keeps the tree, and a viewer's tree holds only the people they may see
on the People page — somebody who appears only in hidden photographs is not
in it, and nor is any line to them.
"""

import json
import time

import numpy as np
import pytest

from conftest import FAMILY, login
from ninaivu.storage import db, family_tree


def _person(conn, name, asset_id, pid):
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(?,?,?)",
                 (pid, name, time.time()))
    conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, created_at) "
        "VALUES(?,?,'confirmed',?,?,0.8,?)",
        (asset_id, pid, json.dumps([0, 0, 9, 9]), np.zeros(128, "float32").tobytes(),
         time.time()))
    return pid


@pytest.fixture()
def household(app, people, scanned):
    """Grandfather, father, mother, child in family photographs; Ghost only in
    a hidden one."""
    cfg, conn, _ = scanned
    rows = conn.execute("SELECT id FROM assets WHERE root=? AND kind='picture' ORDER BY id",
                        (cfg.active_root,)).fetchall()
    seen, hidden = int(rows[0]["id"]), int(rows[1]["id"])
    conn.execute("UPDATE assets SET visibility=1 WHERE id=?", (seen,))
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden,))
    ids = {name: _person(conn, name, seen, n)
           for n, name in enumerate(("Thatha", "Appa", "Amma", "Maya"), start=1)}
    ids["Ghost"] = _person(conn, "Ghost", hidden, 9)
    conn.commit()
    return ids


# -- storage ---------------------------------------------------------------------

def test_self_relations_and_unknown_kinds_are_refused(scanned, household):
    _, conn, _ = scanned
    with pytest.raises(ValueError):
        family_tree.add(conn, household["Appa"], household["Appa"], "parent", None)
    with pytest.raises(ValueError):
        family_tree.add(conn, household["Appa"], household["Appa"], "spouse", None)
    with pytest.raises(ValueError):
        family_tree.add(conn, household["Appa"], household["Maya"], "cousin", None)
    # And the table itself says so, should anything write around the module.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO person_relations(person_a, person_b, kind, created_at) "
                     "VALUES(1, 1, 'parent', 0)")
    conn.rollback()


def test_a_cycle_in_parentage_is_refused(scanned, household):
    _, conn, _ = scanned
    t, a, m = household["Thatha"], household["Appa"], household["Maya"]
    family_tree.add(conn, t, a, "parent", None)
    family_tree.add(conn, a, m, "parent", None)
    for parent, child in ((m, t), (m, a), (a, t)):
        with pytest.raises(ValueError, match="own ancestor"):
            family_tree.add(conn, parent, child, "parent", None)
    assert len(family_tree.relations(conn)) == 2


def test_a_cycle_through_somebody_hidden_is_still_a_cycle(as_family, scanned, household):
    _, conn, _ = scanned
    family_tree.add(conn, household["Maya"], household["Ghost"], "parent", None)
    family_tree.add(conn, household["Ghost"], household["Thatha"], "parent", None)
    reply = as_family.post("/api/family-tree/relations", json={
        "person_id": household["Maya"], "other_id": household["Thatha"], "relation": "parent"})
    assert reply.status_code == 409
    assert "Ghost" not in reply.get_data(as_text=True)


def test_parent_and_spouse_of_the_same_pair_are_refused(scanned, household):
    _, conn, _ = scanned
    family_tree.add(conn, household["Appa"], household["Maya"], "parent", None)
    with pytest.raises(ValueError):
        family_tree.add(conn, household["Maya"], household["Appa"], "spouse", None)
    family_tree.add(conn, household["Appa"], household["Amma"], "spouse", None)
    with pytest.raises(ValueError):
        family_tree.add(conn, household["Amma"], household["Appa"], "parent", None)


def test_a_marriage_is_one_row_whichever_way_it_was_entered(scanned, household):
    _, conn, _ = scanned
    one = family_tree.add(conn, household["Amma"], household["Appa"], "spouse", None)
    two = family_tree.add(conn, household["Appa"], household["Amma"], "spouse", None)
    assert one == two
    assert (one["person_a"], one["person_b"]) == (household["Appa"], household["Amma"])


def test_generations_put_couples_on_one_row(scanned, household):
    _, conn, _ = scanned
    t, a, w, m = (household[n] for n in ("Thatha", "Appa", "Amma", "Maya"))
    family_tree.add(conn, t, a, "parent", None)
    family_tree.add(conn, a, m, "parent", None)
    family_tree.add(conn, a, w, "spouse", None)
    rows = family_tree.generations([t, a, w, m, household["Ghost"]], family_tree.relations(conn))
    assert rows == {t: 0, a: 1, w: 1, m: 2, household["Ghost"]: None}


def test_deleting_a_person_takes_their_relations(scanned, household):
    _, conn, _ = scanned
    family_tree.add(conn, household["Appa"], household["Maya"], "parent", None)
    db.delete_person_cluster(conn, household["Appa"])
    assert family_tree.relations(conn) == []


def test_merging_two_people_keeps_the_tree(scanned, household):
    """Two clusters that were one person: the parent recorded against the
    duplicate is the real person's parent now, and a line that would become
    a loop or a person with themselves is dropped, not an error."""
    _, conn, _ = scanned
    t, a, w, m = (household[n] for n in ("Thatha", "Appa", "Amma", "Maya"))
    family_tree.add(conn, t, a, "parent", None)
    family_tree.add(conn, w, m, "parent", None)
    family_tree.add(conn, a, w, "spouse", None)
    db.merge_people(conn, w, a)          # "Amma" turns out to be Appa's duplicate
    kept = {(r["person_a"], r["person_b"], r["kind"]) for r in family_tree.relations(conn)}
    assert kept == {(t, a, "parent"), (a, m, "parent")}


# -- what a viewer sees ------------------------------------------------------------

def test_the_tree_holds_only_the_people_the_viewer_may_see(as_family, as_admin, scanned,
                                                           household):
    _, conn, _ = scanned
    family_tree.add(conn, household["Appa"], household["Maya"], "parent", None)
    family_tree.add(conn, household["Ghost"], household["Appa"], "parent", None)

    family = as_family.get("/api/family-tree").get_json()
    names = {p["name"] for p in family["people"]}
    assert "Ghost" not in names and "Maya" in names
    assert "Ghost" not in json.dumps(family)
    assert {(r["person_a"], r["person_b"]) for r in family["relations"]} == {
        (household["Appa"], household["Maya"])}
    rows = {p["name"]: p["generation"] for p in family["people"]}
    assert (rows["Appa"], rows["Maya"], rows["Thatha"]) == (0, 1, None)

    admin = as_admin.get("/api/family-tree").get_json()
    assert "Ghost" in {p["name"] for p in admin["people"]}
    assert len(admin["relations"]) == 2


def test_a_family_member_edits_only_between_people_they_can_see(as_family, scanned,
                                                                household):
    _, conn, _ = scanned
    add = as_family.post("/api/family-tree/relations", json={
        "person_id": household["Maya"], "other_id": household["Appa"], "relation": "parent"})
    assert add.status_code == 200, add.get_json()
    relation = add.get_json()["relation"]
    assert (relation["person_a"], relation["person_b"], relation["kind"]) == (
        household["Appa"], household["Maya"], "parent")
    child = as_family.post("/api/family-tree/relations", json={
        "person_id": household["Thatha"], "other_id": household["Appa"], "relation": "child"})
    assert child.get_json()["relation"]["person_a"] == household["Thatha"]
    spouse = as_family.post("/api/family-tree/relations", json={
        "person_id": household["Appa"], "other_id": household["Amma"], "relation": "spouse"})
    assert spouse.status_code == 200

    hidden = as_family.post("/api/family-tree/relations", json={
        "person_id": household["Maya"], "other_id": household["Ghost"], "relation": "parent"})
    assert hidden.status_code == 404
    loop = as_family.post("/api/family-tree/relations", json={
        "person_id": household["Thatha"], "other_id": household["Maya"], "relation": "parent"})
    assert loop.status_code == 409
    itself = as_family.post("/api/family-tree/relations", json={
        "person_id": household["Maya"], "other_id": household["Maya"], "relation": "spouse"})
    assert itself.status_code == 409
    for body in ({"person_id": "1", "other_id": 2, "relation": "parent"},
                 {"person_id": 1, "other_id": 2, "relation": "uncle"}, [1]):
        assert as_family.post("/api/family-tree/relations", json=body).status_code == 400

    assert as_family.delete(f"/api/family-tree/relations/{relation['id']}").status_code == 200
    assert as_family.delete(f"/api/family-tree/relations/{relation['id']}").status_code == 404
    ghostly = family_tree.add(conn, household["Ghost"], household["Maya"], "parent", None)
    assert as_family.delete(f"/api/family-tree/relations/{ghostly['id']}").status_code == 404
    assert family_tree.get(conn, ghostly["id"]) is not None


def test_a_guest_has_no_tree(as_guest, household):
    assert as_guest.get("/api/family-tree").status_code in (401, 403)
    assert as_guest.post("/api/family-tree/relations", json={
        "person_id": household["Maya"], "other_id": household["Appa"],
        "relation": "parent"}).status_code in (401, 403)


def test_the_tree_is_cached_per_viewer_but_follows_a_change(app, household, scanned):
    """list_people is remembered between requests; the tree's relations are
    read fresh, so an edit shows at once."""
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/family-tree").get_json()["relations"] == []
    family.post("/api/family-tree/relations", json={
        "person_id": household["Maya"], "other_id": household["Appa"], "relation": "parent"})
    assert len(family.get("/api/family-tree").get_json()["relations"]) == 1
