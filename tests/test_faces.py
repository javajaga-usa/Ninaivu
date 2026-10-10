"""Face detection, grouping and who is allowed to see any of it.

Three separate things are tested here, and they are separate on purpose.

The *policy* tests use synthetic embeddings at controlled cosine distances.
They need no model on disk and no photographs, so they run everywhere and they
are the tests that actually pin down the behaviour that matters: that a chain
of individually-plausible resemblances never merges two identities, that a
face resembling two people is asked about rather than decided, and that a
rejection is permanent.

The *engine* tests need the ONNX models and are skipped without them.

The *access* tests are the important ones. Faces are derived from
photographs, so every rule that governs a photograph has to govern its faces
too — otherwise the people browser becomes a way to learn who is in a picture
you are not allowed to open.
"""

import json
import time

import numpy as np
import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.storage import db
from ninaivu.media import facematch as fm


D = 128


def near(base, cos, rng):
    """A unit vector approximately ``cos`` away from ``base``."""
    noise = rng.normal(0, 1, D)
    noise -= noise.dot(base) * base
    noise /= np.linalg.norm(noise)
    return fm.unit(cos * base + np.sqrt(max(0.0, 1 - cos ** 2)) * noise)


@pytest.fixture()
def rng():
    return np.random.RandomState(1234)


# ---------------------------------------------------------------------------
# The matching policy
# ---------------------------------------------------------------------------

def test_a_chain_of_resemblances_never_merges_two_identities(rng):
    """The failure mode that ruins face grouping, tested directly.

    A resembles B and B resembles C, while A and C are plainly different
    people. Greedy clustering on the centroid alone walks that chain and ends
    up with one cluster containing both identities. The exemplar link check
    exists to stop it.
    """
    a = fm.unit(rng.normal(0, 1, D))
    c = fm.unit(rng.normal(0, 1, D))
    b = fm.unit(a + c)                       # exactly between the two

    assert fm.similarity(a, b) > fm.T_CLUSTER
    assert fm.similarity(b, c) > fm.T_CLUSTER
    assert fm.similarity(a, c) < fm.COSINE_SAME

    clusters = fm.cluster_faces([
        fm.Candidate(1, a, 0.9), fm.Candidate(2, b, 0.8), fm.Candidate(3, c, 0.7),
    ])
    for cluster in clusters:
        assert not {1, 3}.issubset(set(cluster.members))


def test_real_people_cluster_together_and_stay_pure(rng):
    """Two people, at the spread measured on real photographs, group cleanly."""
    bases = [fm.unit(rng.normal(0, 1, D)), fm.unit(rng.normal(0, 1, D))]
    candidates, truth, face_id = [], {}, 1
    for label, base in enumerate(bases):
        for _ in range(10):
            vector = near(base, rng.uniform(0.70, 0.95), rng)
            candidates.append(fm.Candidate(face_id, vector, rng.uniform(0.3, 0.9)))
            truth[face_id] = label
            face_id += 1

    clusters = [c for c in fm.cluster_faces(candidates) if c.size >= 3]
    assert len(clusters) == 2
    for cluster in clusters:
        labels = {truth[m] for m in cluster.members}
        assert len(labels) == 1, "a cluster mixed two identities"


def test_a_face_between_two_lookalikes_is_asked_about_not_decided(rng):
    """The sibling guard.

    An absolute threshold is not enough when the library contains two people
    who genuinely resemble each other. The margin over the runner-up is what
    turns that into a question.
    """
    base = fm.unit(rng.normal(0, 1, D))
    lookalike = near(base, 0.80, rng)
    alice = fm.Person(1, base, [near(base, 0.9, rng) for _ in range(5)], "Alice")
    bob = fm.Person(2, lookalike,
                    [near(lookalike, 0.9, rng) for _ in range(5)], "Bob")

    ambiguous = fm.unit(alice.centroid + bob.centroid)
    result = fm.assign_face(ambiguous, [alice, bob])

    assert result.decision == "suggest"
    assert abs(result.score - result.runner_up_score) < fm.MARGIN


def test_an_unambiguous_face_is_assigned_without_asking(rng):
    base = fm.unit(rng.normal(0, 1, D))
    other = fm.unit(rng.normal(0, 1, D))
    alice = fm.Person(1, base, [near(base, 0.9, rng) for _ in range(5)], "Alice")
    bob = fm.Person(2, other, [near(other, 0.9, rng) for _ in range(5)], "Bob")

    result = fm.assign_face(near(base, 0.88, rng), [alice, bob])
    assert result.decision == "auto"
    assert result.person_id == 1


@pytest.mark.parametrize("cosine", [0.772, 0.803, 0.675, 0.871, 0.881, 0.715])
def test_degraded_photographs_of_the_same_person_still_match(rng, cosine):
    """The similarities actually measured against this model.

    In order: two frames of one person, JPEG quality 10, a 20-degree tilt, a
    face shrunk to 52 pixels, greyscale, and heavy sensor noise. Every one of
    them has to stay on the confident side of the line, or the grouping stops
    working on exactly the old photographs this is for.
    """
    base = fm.unit(rng.normal(0, 1, D))
    person = fm.Person(1, base, [near(base, 0.9, rng) for _ in range(5)], "Alice")
    assert fm.assign_face(near(base, cosine, rng), [person]).decision == "auto"


def test_a_stranger_is_never_offered_as_a_match(rng):
    base = fm.unit(rng.normal(0, 1, D))
    person = fm.Person(1, base, [near(base, 0.9, rng) for _ in range(5)], "Alice")
    stranger = fm.unit(rng.normal(0, 1, D))
    assert fm.assign_face(stranger, [person]).decision == "none"


def test_a_rejection_is_permanent_and_downgrades_later_guesses(rng):
    """Answering "not them" once must settle it, and must make the matcher
    more cautious about that face generally rather than simply moving it to
    the next-best person."""
    base = fm.unit(rng.normal(0, 1, D))
    other = near(base, 0.75, rng)
    alice = fm.Person(1, base, [near(base, 0.9, rng) for _ in range(5)], "Alice")
    bob = fm.Person(2, other, [near(other, 0.9, rng) for _ in range(5)], "Bob")
    face = near(base, 0.9, rng)

    assert fm.assign_face(face, [alice, bob]).person_id == 1
    after = fm.assign_face(face, [alice, bob], blocked=[1])
    assert after.person_id != 1
    assert after.decision != "auto"


def test_centroid_is_renormalised_so_prolific_people_are_not_favoured(rng):
    """Without re-normalising, whoever has the most photographs wins every
    comparison simply by having a longer vector."""
    base = fm.unit(rng.normal(0, 1, D))
    few = fm.centroid([near(base, 0.9, rng) for _ in range(2)])
    many = fm.centroid([near(base, 0.9, rng) for _ in range(200)])
    assert np.isclose(np.linalg.norm(few), 1.0, atol=1e-5)
    assert np.isclose(np.linalg.norm(many), 1.0, atol=1e-5)


def test_tiny_clusters_are_not_offered_for_naming(rng):
    """One stray crop is not a person, and a console full of them is a console
    nobody opens."""
    singles = [fm.Candidate(i, fm.unit(rng.normal(0, 1, D)), 0.5)
               for i in range(1, 6)]
    clusters = fm.cluster_faces(singles)
    assert all(c.size < fm.MIN_CLUSTER_SIZE for c in clusters)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def _seed_faces(conn, root, *, name="Maya"):
    """Put one person in a public photo and one in a hidden photo."""
    rows = conn.execute(
        "SELECT id, visibility FROM assets WHERE root=? ORDER BY id", (root,)
    ).fetchall()
    assert len(rows) >= 2
    public_id, hidden_id = int(rows[0]["id"]), int(rows[1]["id"])
    conn.execute("UPDATE assets SET visibility=0 WHERE id=?", (public_id,))
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden_id,))
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(1,?,?)",
                 (name, time.time()))
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(2,'Ghost',?)",
                 (time.time(),))
    blob = np.zeros(128, dtype="float32").tobytes()
    for asset_id, person in ((public_id, 1), (hidden_id, 1), (hidden_id, 2)):
        conn.execute(
            "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, "
            "quality, created_at) VALUES(?,?,'confirmed',?,?,0.8,?)",
            (asset_id, person, json.dumps([0, 0, 90, 90]), blob, time.time()))
    conn.commit()
    return public_id, hidden_id


def test_a_person_only_in_hidden_photographs_is_invisible_to_the_family(scanned):
    """Not "present with a count of zero" — absent.

    A name with a zero beside it still discloses that somebody is in the
    library, which is precisely what hiding a photograph was meant to prevent.
    """
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)

    family = [p["name"] for p in db.list_people(conn, cfg.active_root,
                                                max_visibility=1)]
    admin = [p["name"] for p in db.list_people(conn, cfg.active_root,
                                               max_visibility=2)]
    assert "Ghost" not in family
    assert "Ghost" in admin


def test_face_counts_are_per_viewer(scanned):
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)

    def count(vis):
        people = db.list_people(conn, cfg.active_root, max_visibility=vis)
        return {p["name"]: p["photo_count"] for p in people}

    assert count(0)["Maya"] == 1      # the public photograph only
    assert count(1)["Maya"] == 1
    assert count(2)["Maya"] == 2      # an admin also sees the hidden one


def test_a_cover_the_viewer_may_not_open_is_replaced_by_one_they_may(scanned):
    """The crop is served only to somebody who may open its photograph, so a
    stored cover in a hidden photograph was a broken image on the family's
    People page."""
    cfg, conn, _ = scanned
    public_id, hidden_id = _seed_faces(conn, cfg.active_root)
    hidden_face = conn.execute("SELECT id FROM faces WHERE asset_id=? AND person_id=1",
                               (hidden_id,)).fetchone()["id"]
    public_face = conn.execute("SELECT id FROM faces WHERE asset_id=? AND person_id=1",
                               (public_id,)).fetchone()["id"]
    conn.execute("UPDATE people_clusters SET cover_face_id=? WHERE id=1", (hidden_face,))
    conn.commit()

    def cover(vis):
        people = db.list_people(conn, cfg.active_root, max_visibility=vis)
        return next(p["cover_face_id"] for p in people if p["name"] == "Maya")

    assert cover(1) == public_face
    assert cover(2) == hidden_face, "an administrator keeps the chosen cover"


def test_the_person_filter_cannot_widen_what_a_viewer_sees(scanned):
    """``?person=N`` narrows. It must never be a way around visibility."""
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)

    _, guest_total = db.query_assets(conn, cfg.active_root,
                                     max_visibility=0, person=1)
    _, admin_total = db.query_assets(conn, cfg.active_root,
                                     max_visibility=2, person=1)
    assert guest_total == 1
    assert admin_total == 2


def test_folder_scope_applies_to_people_too(scanned):
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)
    people = db.list_people(conn, cfg.active_root, max_visibility=2,
                            scope="nowhere/at/all")
    assert people == []


def test_a_photo_with_several_faces_of_one_person_is_listed_once(scanned):
    """EXISTS rather than a join — three faces of Maya in one photograph is
    still one photograph."""
    cfg, conn, _ = scanned
    public_id, _ = _seed_faces(conn, cfg.active_root)
    blob = np.zeros(128, dtype="float32").tobytes()
    for _ in range(2):
        conn.execute(
            "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, "
            "quality, created_at) VALUES(?,1,'confirmed',?,?,0.7,?)",
            (public_id, json.dumps([5, 5, 40, 40]), blob, time.time()))
    conn.commit()
    rows, total = db.query_assets(conn, cfg.active_root, max_visibility=0, person=1)
    assert total == 1
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Renaming and merging
# ---------------------------------------------------------------------------
#
# A typo fix and a merge are the same admission looked at from two sides:
# "I spelled that wrong" and "these are the same person" both mean the
# library should end up with one row, named once — so both go through
# db.rename_person_cluster, which decides which one happened by whether the
# new name already belongs to somebody else.

def test_a_typo_is_just_a_rename(scanned):
    """Renaming to a name nobody else has is exactly that: no merge, and
    nobody else's faces move."""
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root, name="Mayaa")

    result = db.rename_person_cluster(conn, 1, "Maya")
    assert result["merged"] is False
    assert result["person_id"] == 1
    assert result["person"]["name"] == "Maya"
    names = [p["name"] for p in db.list_people(conn, cfg.active_root, max_visibility=2)]
    assert names.count("Maya") == 1


def test_renaming_to_an_existing_name_merges_instead(scanned):
    """Typing somebody else's name is how an admin says "these are the same
    person" — so it folds the two rows into one rather than leaving two
    people who happen to share a name."""
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)   # person 1 "Maya" (2 faces), 2 "Ghost" (1 face)

    result = db.rename_person_cluster(conn, 1, "Ghost")
    assert result["merged"] is True
    assert result["person_id"] == 2

    remaining = [dict(r) for r in
                conn.execute("SELECT id, name FROM people_clusters").fetchall()]
    assert remaining == [{"id": 2, "name": "Ghost"}]
    moved = conn.execute(
        "SELECT COUNT(*) n FROM faces WHERE person_id=2").fetchone()["n"]
    assert moved == 3   # Maya's two faces plus Ghost's own


def test_the_name_match_is_case_insensitive_like_naming_a_cluster_already_is(scanned):
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)
    result = db.rename_person_cluster(conn, 1, "ghost")
    assert result["merged"] is True
    assert result["person_id"] == 2


def test_renaming_a_person_to_their_own_name_does_not_merge_them_with_themselves(scanned):
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)
    result = db.rename_person_cluster(conn, 1, "Maya")
    assert result["merged"] is False
    assert result["person_id"] == 1


def test_naming_an_unnamed_cluster_with_an_existing_name_joins_that_person():
    """The other half of the same idea: create_or_update_person_cluster (used
    when naming a fresh cluster) already reuses an existing name rather than
    creating a duplicate — this pins that down directly, in isolation from
    the face-matching machinery."""
    from ninaivu.server import auth
    connection = db.init_db(":memory:")
    auth.init_auth_schema(connection)
    first = db.create_or_update_person_cluster(connection, "Priya")
    second = db.create_or_update_person_cluster(connection, "priya")
    assert first["id"] == second["id"]
    assert connection.execute(
        "SELECT COUNT(*) n FROM people_clusters").fetchone()["n"] == 1


def test_naming_a_cluster_says_whether_it_joined_someone(scanned):
    """The console needs to tell "you named a new person" from "you just
    added faces to Maya" apart, so name_cluster reports which one happened."""
    from ninaivu.media.faceindex import FaceIndexer

    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)   # person 1 is already named "Maya"
    asset_id = conn.execute(
        "SELECT id FROM assets WHERE root=? LIMIT 1", (cfg.active_root,)
    ).fetchone()["id"]
    blob = np.zeros(128, dtype="float32").tobytes()
    conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, "
        "quality, cluster_key, created_at) VALUES(?,NULL,'auto',?,?,0.7,'c1',?)",
        (asset_id, json.dumps([0, 0, 10, 10]), blob, time.time()))
    conn.commit()

    indexer = FaceIndexer(cfg)
    joined = indexer.name_cluster(conn, "c1", "maya")   # case differs on purpose
    assert joined["joined_existing"] is True
    assert joined["person"]["id"] == 1

    conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, "
        "quality, cluster_key, created_at) VALUES(?,NULL,'auto',?,?,0.7,'c2',?)",
        (asset_id, json.dumps([20, 20, 30, 30]), blob, time.time()))
    conn.commit()
    fresh = indexer.name_cluster(conn, "c2", "Someone New")
    assert fresh["joined_existing"] is False
    assert fresh["person"]["id"] != 1


def test_naming_a_group_a_regroup_replaced_names_nobody(scanned):
    """Naming a group starts a regroup, which reissues every group's key, so a
    card still on screen can name a group that no longer exists. That used to
    create the person anyway: a name with no faces, counted and never shown."""
    from ninaivu.media.faceindex import FaceIndexer

    cfg, conn, _ = scanned
    before = conn.execute("SELECT COUNT(*) n FROM people_clusters").fetchone()["n"]
    with pytest.raises(LookupError):
        FaceIndexer(cfg).name_cluster(conn, "gone-after-regroup", "Deepa")
    after = conn.execute("SELECT COUNT(*) n FROM people_clusters").fetchone()["n"]
    assert after == before


def test_naming_a_large_group_confirms_every_face_in_it(scanned):
    from ninaivu.media.faceindex import FaceIndexer

    cfg, conn, _ = scanned
    asset_id = conn.execute(
        "SELECT id FROM assets WHERE root=? LIMIT 1", (cfg.active_root,)
    ).fetchone()["id"]
    blob = np.zeros(128, dtype="float32").tobytes()
    conn.executemany(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, "
        "quality, cluster_key, created_at) VALUES(?,NULL,'auto',?,?,0.7,'big',?)",
        [(asset_id, json.dumps([n, 0, n + 10, 10]), blob, time.time())
         for n in range(620)])
    conn.commit()
    result = FaceIndexer(cfg).name_cluster(conn, "big", "Large Family")
    assert result["faces"] == 620


def test_people_named_counts_only_people_with_faces(scanned):
    """The stat and the People list must agree: a name with no faces is not a
    person anyone can see."""
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)                  # Maya and Ghost have faces
    db.create_or_update_person_cluster(conn, "Nobody Yet")
    assert db.face_stats(conn, cfg.active_root)["people"] == 2


# ---------------------------------------------------------------------------
# The role boundary
# ---------------------------------------------------------------------------

FACE_CONSOLE_ROUTES = [
    ("get", "/api/faces/status"),
    ("get", "/api/faces/clusters"),
    ("post", "/api/faces/scan"),
    ("post", "/api/faces/regroup"),
    ("post", "/api/faces/confirm"),
    ("post", "/api/faces/reject"),
    ("post", "/api/faces/person"),
    ("post", "/api/faces/person/1/rename"),
    ("post", "/api/faces/merge"),
    ("post", "/api/faces/models"),
    ("post", "/api/faces/clusters/name"),
]


@pytest.mark.parametrize("method,path", FACE_CONSOLE_ROUTES)
@pytest.mark.parametrize("who", ["family", "guest"])
def test_only_an_admin_may_manage_faces(app, people, method, path, who):
    """Naming people, confirming matches and running detection are management.

    A family member browsing by person is a different thing from a family
    member deciding who somebody is, and only the second one is restricted.
    """
    client = app.test_client()
    login(client, *(FAMILY if who == "family" else GUEST))
    response = getattr(client, method)(path, json={})
    assert response.status_code in (401, 403, 404), (
        f"{method.upper()} {path} was reachable by a {who}")


def test_the_family_app_does_not_route_face_management_at_all(scanned):
    """Absent, not forbidden — the same boundary the rest of the console has."""
    from ninaivu.server import auth
    from ninaivu import build_services, create_home_app

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    client = login(create_home_app(services).test_client(), *ADMIN)

    for method, path in FACE_CONSOLE_ROUTES:
        response = getattr(client, method)(path, json={})
        assert response.status_code == 404, (
            f"{method.upper()} {path} exists on the family app")


def test_a_family_member_may_browse_people(app, people):
    client = app.test_client()
    login(client, *FAMILY)
    response = client.get("/api/faces/people")
    assert response.status_code == 200
    assert "people" in response.get_json()


def test_a_face_crop_from_a_hidden_photograph_is_404_for_the_family(app, people, scanned):
    """The crop is part of the photograph. Anyone who may not open the
    photograph may not have a piece of it either, and the answer is 404 so the
    face's existence is not confirmed."""
    cfg, conn, _ = scanned
    _seed_faces(conn, cfg.active_root)
    hidden_face = conn.execute(
        "SELECT f.id FROM faces f JOIN assets a ON a.id=f.asset_id "
        "WHERE a.visibility=2 LIMIT 1").fetchone()
    conn.execute("UPDATE faces SET thumb='nonexistent.jpg' WHERE id=?",
                 (hidden_face["id"],))
    conn.commit()

    client = app.test_client()
    login(client, *FAMILY)
    assert client.get(f"/api/faces/thumb/{hidden_face['id']}").status_code == 404


# ---------------------------------------------------------------------------
# The engine, when the models are present
# ---------------------------------------------------------------------------

def _engine(cfg):
    """The engine, from the test state dir or the real one.

    The fixtures build a fresh state directory per test, which never contains
    the models. Falling back to ``~/.ninaivu`` means these tests actually run
    on a machine where somebody has downloaded them, instead of silently
    skipping forever on the one machine where they could have caught something.
    """
    from pathlib import Path
    from ninaivu.media import faces as faces_mod

    for state in (cfg.state_dir, Path.home() / ".ninaivu"):
        engine = faces_mod.FaceEngine(state)
        if engine.available:
            return engine
    pytest.skip(f"face models unavailable: {engine.unavailable_reason}")


def test_the_engine_reports_itself_rather_than_raising(cfg):
    """A missing model is a state to report, never an exception. Every caller
    is written to carry on without faces."""
    from ninaivu.media import faces as faces_mod
    engine = faces_mod.FaceEngine(cfg.state_dir / "definitely-not-here")
    assert engine.available is False
    assert isinstance(engine.info, dict)
    assert engine.detect(None) == []


def test_detection_finds_no_faces_in_a_plain_colour(cfg):
    """The fixture library is solid-colour rectangles. Finding a face in one
    would mean the quality gate is not doing anything."""
    import numpy as np
    engine = _engine(cfg)
    blank = np.full((480, 640, 3), 128, dtype="uint8")
    assert engine.detect(blank) == []


def test_embeddings_are_unit_length(cfg):
    """Cosine similarity is a dot product only if the vectors are normalised.
    Everything downstream assumes it."""
    from ninaivu.media import faces as faces_mod
    vector = faces_mod.normalise(np.array([3.0, 4.0] + [0.0] * 126, dtype="float32"))
    assert np.isclose(float(np.linalg.norm(vector)), 1.0)


def test_a_face_version_bump_makes_a_rescan_reconsider_everything(scanned):
    """``face_version`` is what makes the pass resumable and re-runnable."""
    from ninaivu.media import faces as faces_mod
    cfg, conn, _ = scanned
    pending = db.assets_needing_faces(conn, cfg.active_root,
                                      faces_mod.FACE_VERSION)
    assert pending, "the fixture library has pictures to look at"

    conn.execute("UPDATE assets SET face_version=?",
                 (faces_mod.FACE_VERSION,))
    conn.commit()
    assert db.assets_needing_faces(conn, cfg.active_root,
                                   faces_mod.FACE_VERSION) == []
    # A newer detector reconsiders them all.
    assert db.assets_needing_faces(conn, cfg.active_root,
                                   faces_mod.FACE_VERSION + 1)


def test_confirmed_assignments_survive_redetection(scanned):
    """Re-running detection must not throw away an afternoon of naming."""
    cfg, conn, _ = scanned
    public_id, _ = _seed_faces(conn, cfg.active_root)
    blob = np.zeros(128, dtype="float32").tobytes()

    db.replace_asset_faces(conn, public_id, [{
        "bbox": [0, 0, 90, 90], "landmarks": [], "det_score": 0.9,
        "sharpness": 40.0, "quality": 0.8, "embedding": blob,
    }], "test-model")

    kept = conn.execute(
        "SELECT person_id, source FROM faces WHERE asset_id=?", (public_id,)
    ).fetchone()
    assert kept["person_id"] == 1
    assert kept["source"] == "confirmed"


def test_detect_pass_pipeline_scans_and_stores(scanned):
    from ninaivu.media.faceindex import FaceIndexer
    from ninaivu.media.faces import DetectedFace
    cfg, conn, _ = scanned

    class MockEngine:
        available = True
        unavailable_reason = None
        model_id = "mock-model"

        def detect(self, image):
            if image is None:
                return []
            return [DetectedFace(
                bbox=(10, 10, 50, 50),
                landmarks=[(15.0, 15.0), (35.0, 15.0), (25.0, 25.0), (18.0, 35.0), (32.0, 35.0)],
                det_score=0.95,
                sharpness=30.0,
                quality=0.85,
                embedding=np.zeros(128, dtype="float32"),
            )]

    indexer = FaceIndexer(cfg, engine=MockEngine())
    progress_calls = []
    result = indexer.detect_pass(
        conn, cfg.active_root,
        on_progress=lambda done, total: progress_calls.append((done, total)),
    )
    assert result["ok"] is True
    assert result["scanned"] > 0
    assert result["faces"] == result["scanned"]
    assert result["failed"] == 0
    assert progress_calls, "progress callback was invoked"


def test_detect_pass_pipeline_stops_early(scanned):
    from ninaivu.media.faceindex import FaceIndexer
    cfg, conn, _ = scanned

    conn.execute("UPDATE assets SET face_version=0")
    conn.commit()

    class MockEngine:
        available = True
        unavailable_reason = None
        model_id = "mock-model"

        def detect(self, image):
            return []

    indexer = FaceIndexer(cfg, engine=MockEngine())
    stopped = [False]

    def should_stop():
        return stopped[0]

    def on_progress(done, _total):
        if done >= 1:
            stopped[0] = True

    result = indexer.detect_pass(
        conn, cfg.active_root,
        should_stop=should_stop,
        on_progress=on_progress,
    )
    assert result["ok"] is True
    assert result["scanned"] < result["remaining"] + result["scanned"]



def test_one_review_queue_reads_one_persons_faces(scanned, rng):
    """The queue for one person was built by reading every person's faces, a
    query each: 155 queries and 400 ms on a library with 150 named people.
    It must cost the same whether there are three people or forty."""
    from ninaivu.media.faceindex import FaceIndexer

    cfg, conn, _ = scanned
    asset_id = conn.execute("SELECT id FROM assets WHERE root=? LIMIT 1",
                            (cfg.active_root,)).fetchone()["id"]
    centres = {}

    def add_people(first, last):
        for person in range(first, last):
            conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(?,?,?)",
                         (person, f"Person {person}", time.time()))
            centres[person] = fm.unit(rng.normal(0, 1, D))
            for source in ("confirmed", "confirmed", "auto"):
                conn.execute(
                    "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, "
                    "quality, created_at) VALUES(?,?,?,?,?,0.8,?)",
                    (asset_id, person, source, json.dumps([0, 0, 90, 90]),
                     near(centres[person], 0.95, rng).astype("float32").tobytes(),
                     time.time()))
        conn.commit()

    def queue():
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            found = FaceIndexer(cfg).suggestions(conn, 2, [cfg.active_root])
        finally:
            conn.set_trace_callback(None)
        return found, len(statements)

    add_people(1, 4)
    conn.execute(
        "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, "
        "created_at) VALUES(?,NULL,'none',?,?,0.9,?)",
        (asset_id, json.dumps([10, 10, 90, 90]),
         near(centres[2], 0.9, rng).astype("float32").tobytes(), time.time()))
    conn.commit()
    found, few = queue()
    assert [s["face_id"] for s in found], "the face that looks like person 2 was not offered"

    add_people(4, 40)
    again, many = queue()
    assert [s["face_id"] for s in again] == [s["face_id"] for s in found]
    assert many == few, f"{few} queries with 3 people, {many} with 39"
