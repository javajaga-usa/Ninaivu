"""Audit of 10 October 2026: the family features (books, handover, ask the
family, family tree, prints).

Each test was a probe that showed the fault on main before the fix.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time

import numpy as np
import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.cloud import index_copy
from ninaivu.media import books
from ninaivu.server import auth
from ninaivu.storage import family_tree, handover


def _vector(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    vec = rng.normal(size=128).astype("float32")
    return (vec / np.linalg.norm(vec)).astype("float32").tobytes()


@pytest.fixture()
def home(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    books.reset()
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                     role=auth.ROLE_GUEST, created_by=admin.id)
    auth.create_user(conn, "visitor", "visitor-password-1", display_name="Visitor",
                     role=auth.ROLE_FAMILY, created_by=admin.id, scope="shared")
    conn.execute("UPDATE assets SET visibility=2 WHERE folder='private'")
    for (asset_id,) in conn.execute("SELECT id FROM assets").fetchall():
        conn.execute("UPDATE assets SET phash=?, dup_group=NULL, quality='[]' WHERE id=?",
                     (f"{random.Random(asset_id).getrandbits(64):016x}", asset_id))
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}
    hidden_person = conn.execute("INSERT INTO people_clusters(name, created_at) "
                                 "VALUES('Secret Uncle', 0)").lastrowid
    conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality) "
                 "VALUES(?, ?, 'confirmed', '[0,0,10,10]', ?, 0.9)",
                 (ids["secret0.jpg"], hidden_person, _vector(9)))
    unnamed = conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, created_at) "
        "VALUES(?, NULL, 'none', '[0,0,90,90]', ?, 0.8, ?)",
        (ids["shot1.jpg"], _vector(1), time.time())).lastrowid
    conn.commit()
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    clients = {"family": login(app.test_client(), *FAMILY),
               "admin": login(app.test_client(), *ADMIN),
               "visitor": login(app.test_client(), "visitor", "visitor-password-1")}
    console = login(create_admin_app(services).test_client(), *ADMIN)
    yield {"ids": ids, "conn": conn, "cfg": cfg, "app": app, "admin_user": admin,
           "console": console,
           "hidden_person": hidden_person, "unnamed": unnamed, **clients}
    books.reset()
    services.stop(timeout=5.0)


def _delete_and_reuse(conn, old_id, admin_id, **kw):
    auth.delete_user(conn, old_id, heir=admin_id)
    new = auth.create_user(conn, kw.pop("username"), kw.pop("password"),
                           created_by=admin_id, **kw)
    assert new.id == old_id, "the probe needs the id to be reused"
    return new


# -- book picker -------------------------------------------------------------

def test_book_pick_does_not_name_an_album_or_occasion_the_viewer_cannot_open(home):
    secret_ids = [home["ids"][f"secret{i}.jpg"] for i in range(3)]
    album = home["admin"].post("/api/albums", json={"name": "Lawyer - custody papers",
                                                   "ids": secret_ids}).get_json()["id"]
    picked = home["family"].post("/api/books/pick",
                                 json={"source": {"kind": "album", "id": album}})
    assert picked.status_code == 404
    assert "custody" not in picked.get_data(as_text=True)
    # The administrator who made it still can.
    assert home["admin"].post("/api/books/pick", json={
        "source": {"kind": "album", "id": album}}).status_code == 200

    conn = home["conn"]
    conn.execute("INSERT INTO occasions(root, key, title, place, started_at, ended_at, "
                 "days, count) VALUES(?, 'probe', 'Probe', 'Vellore', 0, 0, 1, 4)",
                 (home["cfg"].active_root,))
    occ_id = conn.execute("SELECT id FROM occasions WHERE key='probe'").fetchone()[0]
    conn.commit()
    picked = home["visitor"].post("/api/books/pick",
                                  json={"source": {"kind": "occasion", "id": occ_id}})
    assert picked.status_code == 404
    assert "Vellore" not in picked.get_data(as_text=True)


def test_a_book_is_not_handed_out_once_a_photograph_in_it_is_hidden(home):
    everything = sorted(home["ids"][f"shot{i}.jpg"] for i in range(6))
    album = home["admin"].post("/api/albums", json={"name": "All", "ids": everything}
                               ).get_json()["id"]
    made = home["family"].post("/api/books", json={
        "source": {"kind": "album", "id": album}, "ids": everything, "title": "Ours"})
    assert made.status_code == 202, made.get_json()
    book_id = made.get_json()["id"]
    assert books.wait(book_id, 120) == "done"
    got = home["family"].get(f"/api/books/{book_id}/download")
    assert got.status_code == 200
    got.close()
    home["conn"].execute("UPDATE assets SET visibility=2 WHERE id=?", (everything[0],))
    home["conn"].commit()
    assert home["family"].get(f"/api/books/{book_id}/download").status_code == 410


# -- a deleted profile's number ---------------------------------------------------

def test_a_deleted_members_books_go_with_them(home):
    conn, app, cfg = home["conn"], home["app"], home["cfg"]
    temp = auth.create_user(conn, "aunt", "aunt-password-1", display_name="Aunt",
                            role=auth.ROLE_FAMILY, created_by=home["admin_user"].id)
    aunt = login(app.test_client(), "aunt", "aunt-password-1")
    everything = sorted(home["ids"][f"shot{i}.jpg"] for i in range(6))
    album = home["admin"].post("/api/albums", json={"name": "All", "ids": everything}
                               ).get_json()["id"]
    made = aunt.post("/api/books", json={"source": {"kind": "album", "id": album},
                                         "ids": everything, "title": "Aunt's book"})
    book_id = made.get_json()["id"]
    assert books.wait(book_id, 120) == "done"
    pdf = books_dir_file(cfg, conn, book_id)
    assert pdf.is_file()

    gone = home["console"].delete(f"/api/people/{temp.id}")
    assert gone.status_code == 200, gone.get_json()
    assert "book_files" not in gone.get_json()["removed"]
    assert not pdf.exists()
    new = auth.create_user(conn, "kid", "kid-password-1", display_name="Kid",
                           role=auth.ROLE_FAMILY, created_by=home["admin_user"].id,
                           scope="shared")
    assert new.id == temp.id
    kid = login(app.test_client(), "kid", "kid-password-1")
    assert kid.get("/api/books").get_json()["books"] == []
    assert kid.get(f"/api/books/{book_id}/download").status_code == 404


def books_dir_file(cfg, conn, book_id):
    from ninaivu.storage import books as store
    return store.file_of(cfg.state_dir, store.get(conn, book_id))


def test_a_deleted_successors_number_does_not_name_the_next_profile(home):
    conn, app = home["conn"], home["app"]
    admin_id = home["admin_user"].id
    nephew = auth.create_user(conn, "nephew", "nephew-password-1", display_name="Nephew",
                              role=auth.ROLE_FAMILY, created_by=admin_id)
    plan = handover.clean_plan({"successors": [nephew.id]}, eligible=[nephew.id])
    handover.save(conn, plan, wait_days=0, user_id=admin_id, destinations="")
    code = handover.make_code()
    handover.set_code(conn, admin_id, auth.hash_password(handover.normalise_code(code)))
    _delete_and_reuse(conn, nephew.id, admin_id, username="teen",
                      password="teen-password-1", display_name="Teen",
                      role=auth.ROLE_FAMILY)
    assert handover.load(conn)["plan"]["successors"] == []
    teen = login(app.test_client(), "teen", "teen-password-1")
    assert teen.get("/api/handover/me").get_json()["successor"] is False
    claim = teen.post("/api/handover/claim", json={"code": code,
                                                   "password": "teen-password-1"})
    assert claim.status_code == 403
    assert auth.get_user(conn, nephew.id).role == auth.ROLE_FAMILY


def test_a_deleted_successors_waiting_claim_is_void(home):
    conn = home["conn"]
    admin_id = home["admin_user"].id
    nephew = auth.create_user(conn, "nephew", "nephew-password-1", display_name="Nephew",
                              role=auth.ROLE_FAMILY, created_by=admin_id)
    plan = handover.clean_plan({"successors": [nephew.id]}, eligible=[nephew.id])
    handover.save(conn, plan, wait_days=7, user_id=admin_id, destinations="")
    hashed = auth.hash_password("X")
    handover.set_code(conn, admin_id, hashed)
    claim = handover.start_claim(conn, nephew.id, hashed)
    _delete_and_reuse(conn, nephew.id, admin_id, username="teen",
                      password="teen-password-1", display_name="Teen",
                      role=auth.ROLE_FAMILY)
    assert handover.get_claim(conn, claim["id"])["state"] == handover.VOID
    from ninaivu.api.api_handover import complete_due
    with home["app"].test_request_context():
        assert complete_due(conn, now=claim["due_at"] + 1) == 0
    assert auth.get_user(conn, nephew.id).role == auth.ROLE_FAMILY


def test_a_claim_older_than_the_profile_never_completes(home):
    """Even with the plan naming the number again, a claim made before the
    profile existed was somebody else's."""
    conn = home["conn"]
    admin_id = home["admin_user"].id
    teen = auth.create_user(conn, "teen", "teen-password-1", display_name="Teen",
                            role=auth.ROLE_FAMILY, created_by=admin_id)
    plan = handover.clean_plan({"successors": [teen.id]}, eligible=[teen.id])
    handover.save(conn, plan, wait_days=7, user_id=admin_id, destinations="")
    conn.execute("INSERT INTO handover_claims(user_id, requested_at, due_at, state) "
                 "VALUES(?, ?, ?, 'waiting')", (teen.id, teen.created_at - 100, 1.0))
    conn.commit()
    from ninaivu.api.api_handover import complete_due
    with home["app"].test_request_context():
        assert complete_due(conn, now=time.time()) == 0
    assert auth.get_user(conn, teen.id).role == auth.ROLE_FAMILY


def test_a_deleted_profiles_stories_are_not_the_next_ones(home):
    conn = home["conn"]
    temp = auth.create_user(conn, "aunt", "aunt-password-1", display_name="Aunt",
                            role=auth.ROLE_FAMILY, created_by=home["admin_user"].id)
    conn.execute("INSERT INTO stories(asset_id, file, mime, created_by, created_at) "
                 "VALUES(?, 'x.webm', 'audio/webm', ?, 0)", (home["ids"]["shot0.jpg"], temp.id))
    conn.commit()
    auth.delete_user(conn, temp.id, heir=home["admin_user"].id)
    assert conn.execute("SELECT created_by FROM stories").fetchone()[0] is None


# -- handover allowance --------------------------------------------------------

def test_somebody_not_named_cannot_spend_the_households_handover_allowance(home):
    conn, app = home["conn"], home["app"]
    admin_id = home["admin_user"].id
    heir = auth.create_user(conn, "heir", "heir-password-1", display_name="Heir",
                            role=auth.ROLE_FAMILY, created_by=admin_id)
    plan = handover.clean_plan({"successors": [heir.id]}, eligible=[heir.id])
    handover.save(conn, plan, wait_days=7, user_id=admin_id, destinations="")
    code = handover.make_code()
    handover.set_code(conn, admin_id, auth.hash_password(handover.normalise_code(code)))
    from ninaivu.api import api_handover
    everywhere = api_handover._TRIES_EVERYWHERE[0]
    # Spread over many people, so no one person's allowance runs out first.
    for n in range(everywhere + 2):
        auth.create_user(conn, f"cousin{n}", "cousin-password-1", display_name=f"C{n}",
                         role=auth.ROLE_FAMILY, created_by=admin_id)
        cousin = login(app.test_client(), f"cousin{n}", "cousin-password-1")
        cousin.post("/api/handover/claim", json={"code": "WRONG", "password": "x"},
                    environ_base={"REMOTE_ADDR": f"10.0.0.{n + 2}"})
    heir_client = login(app.test_client(), "heir", "heir-password-1")
    claim = heir_client.post("/api/handover/claim",
                             json={"code": code, "password": "heir-password-1"},
                             environ_base={"REMOTE_ADDR": "10.0.1.1"})
    assert claim.status_code == 200, claim.get_json()


# -- ask the family --------------------------------------------------------------

def test_accepting_a_name_never_joins_a_person_only_admins_can_see(home):
    family, conn = home["family"], home["conn"]
    made = family.post("/api/ask-family/questions", json={"face_ids": [home["unnamed"]]})
    assert made.status_code == 200, made.get_json()
    token = made.get_json()["token"]
    sent = home["app"].test_client().post(
        f"/api/ask/{token}/answer", json={"answers": [{"n": 1, "name": "secret uncle"}]})
    assert sent.status_code == 200, sent.get_json()
    answer = family.get("/api/ask-family/answers").get_json()["answers"][0]
    accepted = family.post(f"/api/ask-family/answers/{answer['id']}/accept",
                           json={"name": "secret uncle"})
    assert accepted.status_code == 200, accepted.get_json()
    body = accepted.get_json()
    assert body["person"]["id"] != home["hidden_person"]
    assert body["person"]["name"] == "secret uncle"
    row = conn.execute("SELECT person_id FROM faces WHERE id=?", (home["unnamed"],)).fetchone()
    assert row["person_id"] == body["person"]["id"]
    # The admin-only person's own faces are untouched.
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id=?",
                        (home["hidden_person"],)).fetchone()[0] == 1


# -- the Drive copy of the index ------------------------------------------------------

def test_the_drive_copy_carries_no_ask_links_or_handover_code(tmp_path):
    path = tmp_path / "index.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE ask_questions(id INTEGER PRIMARY KEY, token TEXT);"
            "CREATE TABLE ask_faces(question_id INTEGER, face_id INTEGER);"
            "CREATE TABLE ask_answers(id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE handover_plan(id INTEGER PRIMARY KEY, code_hash TEXT, plan TEXT);"
            "INSERT INTO ask_questions VALUES(1, 'live-token');"
            "INSERT INTO ask_faces VALUES(1, 1);"
            "INSERT INTO ask_answers VALUES(1, 'a stranger''s words');"
            "INSERT INTO handover_plan VALUES(1, 'scrypt$hash', '{}');")
    index_copy._slim(path)
    with sqlite3.connect(path) as conn:
        for table in ("ask_questions", "ask_faces", "ask_answers"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT code_hash, plan FROM handover_plan").fetchone() == ("", "{}")


# -- family tree ------------------------------------------------------------------------

def _tree_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    family_tree.init(conn)
    return conn


def test_a_loop_through_a_marriage_is_refused():
    conn = _tree_db()
    family_tree.add(conn, 1, 2, "parent", None)      # A is B's parent
    family_tree.add(conn, 2, 3, "spouse", None)      # B married C
    with pytest.raises(ValueError):
        family_tree.add(conn, 3, 1, "parent", None)  # C is A's parent: a loop
    family_tree.add(conn, 4, 1, "parent", None)      # an ordinary grandparent is fine
    family_tree.add(conn, 3, 5, "parent", None)      # and a child of the couple
    # The same loop entered the other way round, closing on the marriage.
    family_tree.add(conn, 6, 7, "parent", None)
    family_tree.add(conn, 8, 6, "parent", None)
    with pytest.raises(ValueError):
        family_tree.add(conn, 7, 8, "spouse", None)


def test_an_existing_loop_does_not_block_unrelated_relations():
    conn = _tree_db()
    conn.executescript(
        "INSERT INTO person_relations(person_a, person_b, kind, created_at) VALUES"
        "(1, 2, 'parent', 0), (2, 3, 'spouse', 0), (3, 1, 'parent', 0);")
    family_tree.add(conn, 10, 11, "parent", None)


# -- prints --------------------------------------------------------------------------

def test_a_print_batch_cannot_be_saved_twice_at_once(home, monkeypatch, tmp_path):
    from ninaivu.api import api_scan_prints
    from ninaivu.media import print_scan
    folder = tmp_path / "batch"
    folder.mkdir()
    meta = {"photos": [{"file": "a.jpg", "prints": [{"quad": [[0, 0]] * 4}]}]}
    monkeypatch.setattr(print_scan, "load_session", lambda cfg, token, owner: (folder, meta))
    api_scan_prints._SAVING.add(str(folder))
    try:
        again = home["family"].post("/api/prints/save",
                                    json={"session": "x", "picks": [[0, 0]]})
        assert again.status_code == 409
    finally:
        api_scan_prints._SAVING.discard(str(folder))
    assert json.dumps(sorted(api_scan_prints._SAVING)) == "[]"
