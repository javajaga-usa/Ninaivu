"""Bulk visibility changes must be hard to do by accident and easy to undo.

The incident this exists for: an admin meant to click one button on the whole
library and hit the one beside it. Everything went to "Nobody", so they clicked
"Family" to put it back — and photographs that had been deliberately hidden
became visible to the whole household, because the first click had already
overwritten every per-item decision and nothing remembered what they were.

The whole-library control itself is gone now — it is refused outright, and
``test_admin_limits.py`` holds that. What remains is every other bulk change,
which is to say a rule on a named folder, and it can do the same damage to the
photographs beneath it. So the two things that made the incident survivable are
tested here on a folder instead: the warning that counts what a change would
expose before it happens, and an undo that restores where each setting came
from and not merely what it was.
"""

from pathlib import Path

import pytest

from conftest import FAMILY, login, ids_of
from ninaivu.storage import db
from ninaivu.server.auth import VIS_FAMILY, VIS_HIDDEN, VIS_PUBLIC


#: The folder these tests set rules on. Everything below is about what a rule
#: on a *named* folder does, because that is the only kind there is now.
FOLDER = "private"


@pytest.fixture()
def library_with_secrets(as_admin, people, scanned):
    """A folder whose contents an admin has deliberately hidden, one by one.

    Hidden per item rather than by a rule on the folder, because that is the
    decision the incident destroyed: a sweeping change overwrote choices made
    about individual photographs, and the question is whether undo brings those
    exact choices back.
    """
    _, conn, _ = scanned
    everything = ids_of(as_admin)
    listing = as_admin.get(f"/api/assets?limit=200&folder={FOLDER}").get_json()
    secrets = [item["id"] for item in listing["items"]]
    assert len(secrets) >= 3, "the fixture library changed shape"
    as_admin.post("/api/visibility", json={"ids": secrets, "visibility": "hidden"})
    return as_admin, conn, secrets, everything


def vis_of(conn, ids):
    marks = ",".join("?" * len(ids))
    return {r["id"]: (r["visibility"], r["vis_source"]) for r in conn.execute(
        f"SELECT id, visibility, vis_source FROM assets WHERE id IN ({marks})", ids)}


# --- the second question ---------------------------------------------------

def test_a_widening_change_is_refused_until_it_is_confirmed(library_with_secrets):
    console, _, _, _ = library_with_secrets
    response = console.post("/api/visibility/folder",
                            json={"folder": FOLDER, "visibility": "family"})
    assert response.status_code == 409
    body = response.get_json()
    assert body["needs_confirmation"] is True
    assert body["impact"]["exposed"] >= 3, body["impact"]


def test_the_refusal_says_how_many_would_become_visible(library_with_secrets):
    console, _, secrets, _ = library_with_secrets
    impact = console.post("/api/visibility/folder",
                          json={"folder": FOLDER, "visibility": "family"}
                          ).get_json()["impact"]
    assert impact["exposed"] == len(secrets)
    assert impact["decided_individually"] == len(secrets)
    # `total` is the folder's own count, not the library's — every one of
    # these was hidden by hand, so here the two numbers meet.
    assert impact["total"] == len(secrets)


def test_nothing_changes_while_the_question_is_unanswered(library_with_secrets):
    console, conn, secrets, _ = library_with_secrets
    before = vis_of(conn, secrets)
    console.post("/api/visibility/folder", json={"folder": FOLDER, "visibility": "family"})
    assert vis_of(conn, secrets) == before, "the refused change was applied anyway"


def test_confirming_lets_it_through(library_with_secrets):
    console, conn, secrets, _ = library_with_secrets
    response = console.post("/api/visibility/folder",
                            json={"folder": FOLDER, "visibility": "family", "confirm": True})
    assert response.status_code == 200
    assert all(v == VIS_FAMILY for v, _ in vis_of(conn, secrets).values())


def test_a_narrowing_change_needs_no_confirmation(library_with_secrets):
    """Hiding things cannot expose anything, so it must not nag."""
    console, _, _, _ = library_with_secrets
    response = console.post("/api/visibility/folder",
                            json={"folder": FOLDER, "visibility": "hidden"})
    assert response.status_code == 200


def test_a_preview_can_be_asked_for_without_changing_anything(library_with_secrets):
    console, conn, secrets, _ = library_with_secrets
    before = vis_of(conn, secrets)
    impact = console.post("/api/visibility/preview",
                          json={"folder": FOLDER, "visibility": "public"}).get_json()
    assert impact["exposed"] >= 3
    assert vis_of(conn, secrets) == before


# --- putting it back -------------------------------------------------------

def test_the_exact_reported_incident_is_recoverable(library_with_secrets):
    """Nobody, then Family, then Undo — and the secrets are secret again."""
    console, conn, secrets, _ = library_with_secrets
    original = vis_of(conn, secrets)
    assert all(v == VIS_HIDDEN for v, _ in original.values())

    # The mis-click.
    console.post("/api/visibility/folder", json={"folder": FOLDER, "visibility": "hidden"})
    # …and the attempt to put it back, which is what did the damage.
    console.post("/api/visibility/folder",
                 json={"folder": FOLDER, "visibility": "family", "confirm": True})
    assert all(v == VIS_FAMILY for v, _ in vis_of(conn, secrets).values())

    # Undo the "Family", then undo the "Nobody".
    assert console.post("/api/visibility/undo", json={}).status_code == 200
    assert console.post("/api/visibility/undo", json={}).status_code == 200

    assert vis_of(conn, secrets) == original, (
        "undo did not restore the visibility the admin had chosen per item")


def test_undo_restores_where_the_setting_came_from_too(library_with_secrets):
    console, conn, secrets, _ = library_with_secrets
    assert all(src == "item" for _, src in vis_of(conn, secrets).values())
    console.post("/api/visibility/folder",
                 json={"folder": FOLDER, "visibility": "public", "confirm": True})
    assert all(src == "folder" for _, src in vis_of(conn, secrets).values())

    console.post("/api/visibility/undo", json={})
    assert all(src == "item" for _, src in vis_of(conn, secrets).values()), (
        "the items came back as folder-ruled, so the next rescan would "
        "overwrite them again")


def test_undo_removes_a_folder_rule_that_did_not_exist_before(as_admin, scanned):
    _, conn, _ = scanned
    assert not db.folder_rules(conn, str(Path(conn.execute(
        'SELECT root FROM assets LIMIT 1').fetchone()["root"])))
    as_admin.post("/api/visibility/folder",
                  json={"folder": "private", "visibility": "public", "confirm": True})
    root = conn.execute("SELECT root FROM assets LIMIT 1").fetchone()["root"]
    assert any(r["folder"] == "private" for r in db.folder_rules(conn, root))

    as_admin.post("/api/visibility/undo", json={})
    assert not any(r["folder"] == "private" for r in db.folder_rules(conn, root)), (
        "the rule stayed behind, so newly scanned files would still inherit it")


def test_undo_restores_a_folder_rule_that_did_exist(as_admin, scanned):
    _, conn, _ = scanned
    root = conn.execute("SELECT root FROM assets LIMIT 1").fetchone()["root"]
    as_admin.post("/api/visibility/folder",
                  json={"folder": "private", "visibility": "hidden", "confirm": True})
    as_admin.post("/api/visibility/folder",
                  json={"folder": "private", "visibility": "public", "confirm": True})
    as_admin.post("/api/visibility/undo", json={})
    rule = [r for r in db.folder_rules(conn, root) if r["folder"] == "private"]
    assert rule and int(rule[0]["visibility"]) == VIS_HIDDEN


def test_per_item_changes_are_undoable_as_well(library_with_secrets):
    console, conn, secrets, everything = library_with_secrets
    console.post("/api/visibility", json={"ids": everything, "visibility": "public"})
    assert all(v == VIS_PUBLIC for v, _ in vis_of(conn, secrets).values())
    console.post("/api/visibility/undo", json={})
    assert all(v == VIS_HIDDEN for v, _ in vis_of(conn, secrets).values())


def test_undoing_twice_is_refused_rather_than_doubling_back(library_with_secrets):
    console, _, _, _ = library_with_secrets
    console.post("/api/visibility/folder",
                 json={"folder": FOLDER, "visibility": "public", "confirm": True})
    assert console.post("/api/visibility/undo", json={}).status_code == 200
    again = console.post("/api/visibility/undo",
                         json={"batch_id": console.get("/api/visibility/history")
                               .get_json()["changes"][0]["id"]})
    assert again.status_code == 409


def test_nothing_to_undo_is_a_clear_message(as_admin):
    response = as_admin.post("/api/visibility/undo", json={})
    assert response.status_code == 409
    assert "nothing to undo" in response.get_json()["error"].lower()


# --- the history -----------------------------------------------------------

def test_the_history_lists_what_changed(library_with_secrets):
    console, _, _, _ = library_with_secrets
    console.post("/api/visibility/folder",
                 json={"folder": FOLDER, "visibility": "public", "confirm": True})
    changes = console.get("/api/visibility/history").get_json()["changes"]
    assert changes and changes[0]["scope"] == "folder"
    assert changes[0]["exposed"] >= 3
    assert changes[0]["restorable"] > 0


def test_the_history_stays_bounded(as_admin, scanned):
    for i in range(db.UNDO_HISTORY + 5):
        as_admin.post("/api/visibility/folder",
                      json={"folder": FOLDER, "visibility":
                            "public" if i % 2 else "family", "confirm": True})
    _, conn, _ = scanned
    kept = conn.execute("SELECT COUNT(*) n FROM visibility_batches").fetchone()["n"]
    assert kept <= db.UNDO_HISTORY


# --- and the family really cannot see them in the meantime -----------------

def test_the_family_never_saw_the_secrets_at_any_point(library_with_secrets, app):
    console, conn, secrets, _ = library_with_secrets
    family = login(app.test_client(), *FAMILY)
    assert family.get(f"/api/asset/{secrets[0]}").status_code == 404
    console.post("/api/visibility/folder",
                 json={"folder": FOLDER, "visibility": "hidden"})
    assert family.get(f"/api/asset/{secrets[0]}").status_code == 404
    console.post("/api/visibility/undo", json={})
    assert family.get(f"/api/asset/{secrets[0]}").status_code == 404


# --- the whole library is not offered at all any more ----------------------
#
# It used to be guarded: a password in every direction, then the exposure
# warning, then undo. All three worked, and it was still the one control where
# a slip cost everything — so it was removed rather than guarded harder. The
# refusal itself is held by test_admin_limits.py; what belongs here is that
# taking it away did not quietly weaken anything else.

def test_a_named_folder_needs_no_password(as_admin):
    """Folder rules stay one click. The danger was never the folders."""
    response = as_admin.post("/api/visibility/folder",
                             json={"folder": FOLDER, "visibility": "hidden"})
    assert response.status_code == 200


def test_a_folder_rule_still_cannot_expose_without_asking(library_with_secrets):
    """The guard that mattered is on folders too, and stayed there."""
    console, conn, secrets, _ = library_with_secrets
    before = vis_of(conn, secrets)
    response = console.post("/api/visibility/folder",
                            json={"folder": FOLDER, "visibility": "public"})
    assert response.status_code == 409
    assert response.get_json()["impact"]["exposed"] == len(secrets)
    assert vis_of(conn, secrets) == before


def test_a_folder_rule_is_still_undoable(library_with_secrets):
    """Undo was built for the whole-library incident and outlives it."""
    console, conn, secrets, _ = library_with_secrets
    original = vis_of(conn, secrets)
    console.post("/api/visibility/folder",
                 json={"folder": FOLDER, "visibility": "public", "confirm": True})
    assert all(v == VIS_PUBLIC for v, _ in vis_of(conn, secrets).values())
    assert console.post("/api/visibility/undo", json={}).status_code == 200
    assert vis_of(conn, secrets) == original


def test_per_item_changes_need_no_password_either(as_admin):
    ids = ids_of(as_admin)[:2]
    response = as_admin.post("/api/visibility",
                             json={"ids": ids, "visibility": "hidden"})
    assert response.status_code == 200
    assert response.get_json()["updated"] == len(ids)
