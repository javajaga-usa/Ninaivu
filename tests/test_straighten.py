"""The orientation model, the policy around it, and the library-wide pass.

The rules being protected here are the ones that decide whether this feature
is safe to run on somebody's family photographs:

* a camera's own orientation tag is never second-guessed;
* a tag of 1 may be, but only when the model is close to certain;
* a survey changes nothing;
* whatever an applied batch changed, an undo puts back.
"""


import pytest
from PIL import Image

from ninaivu.media import orientnet, straighten, upright


# ---------------------------------------------------------------------------
# The policy in decide()
# ---------------------------------------------------------------------------

@pytest.fixture()
def picture():
    return Image.new("RGB", (60, 40), (120, 90, 60))


def _model(monkeypatch, rotation: int, confidence: float):
    """Stand in for the network, so the tests need no 77 MB download."""
    monkeypatch.setattr(orientnet, "predict",
                        lambda img, state_dir=None: (rotation, confidence))


def test_a_camera_tag_is_never_overruled_by_the_model(monkeypatch, picture):
    """Tags 2-8 can only have been written by something that knew. They win."""
    _model(monkeypatch, 180, 0.99)
    verdict = upright.decide(picture, exif_orientation=6)
    assert verdict.source == "exif"
    assert verdict.rotation == 90          # what the tag asks for, not the model


def test_every_real_tag_is_honoured_verbatim(monkeypatch, picture):
    _model(monkeypatch, 90, 0.99)
    for tag, expected in ((3, 180), (5, 90), (6, 90), (7, 270), (8, 270)):
        verdict = upright.decide(picture, exif_orientation=tag)
        assert (verdict.source, verdict.rotation) == ("exif", expected), tag


def test_a_tag_of_one_is_overruled_only_when_the_model_is_nearly_certain(
        monkeypatch, picture):
    """Tag 1 is what an app leaves behind after stripping the real answer."""
    _model(monkeypatch, 270, upright.MODEL_TAG1_CONFIDENCE - 0.01)
    assert upright.decide(picture, exif_orientation=1).source == "none"

    _model(monkeypatch, 270, upright.MODEL_TAG1_CONFIDENCE)
    verdict = upright.decide(picture, exif_orientation=1)
    assert (verdict.source, verdict.rotation) == ("model", 270)


def test_a_file_with_no_tag_is_judged_at_the_ordinary_bar(monkeypatch, picture):
    _model(monkeypatch, 90, upright.MODEL_CONFIDENCE)
    verdict = upright.decide(picture, exif_orientation=None)
    assert (verdict.source, verdict.rotation) == ("model", 90)

    _model(monkeypatch, 90, upright.MODEL_CONFIDENCE - 0.01)
    assert upright.decide(picture, exif_orientation=None).source == "none"


def test_a_confident_model_saying_upright_still_turns_nothing(monkeypatch, picture):
    _model(monkeypatch, 0, 0.99)
    assert upright.decide(picture, exif_orientation=None).turns is False


def test_detection_switched_off_decides_nothing(monkeypatch, picture):
    _model(monkeypatch, 90, 0.99)
    assert upright.decide(picture, exif_orientation=None,
                          enabled=False).source == "none"


def test_a_tag_of_one_is_left_alone_when_there_is_no_model(monkeypatch, picture):
    """The face detector is not steady enough to overrule even a weak answer."""
    monkeypatch.setattr(upright, "detect_rotation",
                        lambda img, engine=None: (90, 0.99))
    _model(monkeypatch, 0, 0.0)            # model present but says nothing
    assert upright.decide(picture, exif_orientation=1).source == "none"


def test_the_face_detector_still_covers_untagged_files_without_a_model(
        monkeypatch, picture):
    _model(monkeypatch, 0, 0.0)
    monkeypatch.setattr(upright, "detect_rotation",
                        lambda img, engine=None: (90, 0.8))
    verdict = upright.decide(picture, exif_orientation=None, use_model=False)
    assert (verdict.source, verdict.rotation) == ("faces", 90)


# ---------------------------------------------------------------------------
# The model wrapper
# ---------------------------------------------------------------------------

# `configure` and these calls take the folder the model file is in — the
# shared AI model folder in a real install, a temporary one here. It used to
# be the state directory with "models" appended, from when the orientation
# model lived there rather than with the rest of them.

def test_a_missing_model_file_is_a_shrug_not_a_crash(tmp_path, picture):
    orientnet.reset()
    orientnet.configure(tmp_path)
    assert orientnet.predict(picture, tmp_path) == (0, 0.0)
    assert orientnet.model_status(tmp_path)["present"] is False
    assert orientnet.available(tmp_path) is False


def test_the_model_is_pinned_by_hash(tmp_path):
    """A substituted or truncated download must be refused, not used."""
    assert len(orientnet.MODEL["sha256"]) == 64
    directory = tmp_path / "models"
    directory.mkdir(parents=True)
    (directory / orientnet.MODEL["file"]).write_bytes(b"not a model")
    status = orientnet.model_status(directory)
    assert status["present"] is True        # present, but see the next test
    orientnet.reset()
    assert orientnet.predict(Image.new("RGB", (10, 10)), directory) == (0, 0.0)


def test_the_class_map_matches_the_rotation_convention():
    """Ninaivu's rotation is a clockwise turn; so is the model's class map."""
    assert orientnet._CLASS_ROTATION == {0: 0, 1: 90, 2: 180, 3: 270}


# ---------------------------------------------------------------------------
# The library-wide pass
# ---------------------------------------------------------------------------

@pytest.fixture()
def straightener(scanned, monkeypatch):
    """The pass itself, with the people-only gate off.

    The gate has its own tests below; these are about survey, apply and undo.
    """
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = False
    straighten.init_schema(conn)
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: True)
    return straighten.Straightener(cfg), cfg, conn


def _run(job):
    job.join = None
    thread = job._thread
    if thread:
        thread.join(30)


def test_a_survey_proposes_without_changing_anything(straightener, monkeypatch):
    job, cfg, conn = straightener
    before = conn.execute(
        "SELECT id, rotation, rot_source, indexed_at FROM assets").fetchall()
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))

    job.survey([cfg.active_root])
    _run(job)

    assert job.progress.snapshot()["status"] == "done"
    proposals = conn.execute("SELECT * FROM orientation_proposals").fetchall()
    assert proposals, "the survey should have proposed something"
    assert all(p["status"] == "pending" for p in proposals)

    after = conn.execute(
        "SELECT id, rotation, rot_source, indexed_at FROM assets").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after], \
        "a survey must not touch the index"


def test_a_survey_refuses_to_run_without_the_model(straightener, monkeypatch):
    job, cfg, _ = straightener
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: False)
    job.survey([cfg.active_root])
    _run(job)
    snapshot = job.progress.snapshot()
    assert snapshot["status"] == "error"
    assert "model" in snapshot["message"].lower()


def test_applying_turns_the_index_and_undo_puts_it_back(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    job.survey([cfg.active_root]); _run(job)

    ids = [r["asset_id"] for r in
           conn.execute("SELECT asset_id FROM orientation_proposals").fetchall()]
    assert ids

    job.apply(None, 0.0); _run(job)
    rows = conn.execute(
        f"SELECT id, rotation, rot_source FROM assets "
        f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    assert all(r["rotation"] == 90 and r["rot_source"] == "model" for r in rows)

    result = job.undo()
    assert result["restored"] == len(ids)
    rows = conn.execute(
        f"SELECT rotation, rot_source FROM assets "
        f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    assert all(r["rotation"] == 0 and r["rot_source"] == "none" for r in rows)


def test_a_turn_somebody_set_by_hand_is_never_surveyed(straightener, monkeypatch):
    """The whole point is that this never overwrites a human decision."""
    job, cfg, conn = straightener
    first = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE assets SET rotation=180, rot_source='manual' WHERE id=?",
                 (first,))
    conn.commit()
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))

    job.survey([cfg.active_root]); _run(job)

    proposed = {r["asset_id"] for r in
                conn.execute("SELECT asset_id FROM orientation_proposals").fetchall()}
    assert first not in proposed


def test_a_dismissed_proposal_is_not_offered_again(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    job.survey([cfg.active_root]); _run(job)
    ids = [r["asset_id"] for r in
           conn.execute("SELECT asset_id FROM orientation_proposals").fetchall()]

    assert job.dismiss(ids[:1]) == 1
    job.survey([cfg.active_root], rescan=False); _run(job)
    row = conn.execute("SELECT status FROM orientation_proposals WHERE asset_id=?",
                       (ids[0],)).fetchone()
    assert row["status"] == "dismissed"


def test_an_unsure_model_proposes_nothing(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.5))
    job.survey([cfg.active_root]); _run(job)
    assert conn.execute(
        "SELECT COUNT(*) n FROM orientation_proposals").fetchone()["n"] == 0


def test_the_summary_bands_proposals_by_confidence(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.97))
    job.survey([cfg.active_root]); _run(job)
    summary = job.summary()
    assert summary["counts"]["pending"] > 0
    assert summary["confidence"]["very_sure"] == summary["counts"]["pending"]


# ---------------------------------------------------------------------------
# The permission surface
# ---------------------------------------------------------------------------

def test_straightening_is_absent_from_the_family_port(scanned):
    """Absent, not forbidden — the same rule the rest of library management follows."""
    from ninaivu import create_home_app, build_services

    cfg, _, _ = scanned
    services = build_services(cfg)
    client = create_home_app(services).test_client()
    for path in ("/api/straighten/status", "/api/straighten/proposals"):
        assert client.get(path).status_code == 404, path
    for path in ("/api/straighten/survey", "/api/straighten/apply",
                 "/api/straighten/undo", "/api/straighten/model"):
        assert client.post(path, json={}).status_code == 404, path


def test_a_family_member_cannot_straighten_the_library(as_family):
    assert as_family.get("/api/straighten/status").status_code == 403
    assert as_family.post("/api/straighten/survey", json={}).status_code == 403
    assert as_family.post("/api/straighten/apply",
                          json={"min_confidence": 0.9}).status_code == 403


def test_an_admin_sees_the_status_including_the_model(as_admin):
    body = as_admin.get("/api/straighten/status").get_json()
    assert "counts" in body and "model" in body
    assert body["model"]["file"].endswith(".onnx")


def test_apply_refuses_an_empty_ask(as_admin):
    """Neither a selection nor a confidence floor means nothing to do."""
    assert as_admin.post("/api/straighten/apply", json={}).status_code == 400


# ---------------------------------------------------------------------------
# Only photographs with people in them
# ---------------------------------------------------------------------------

class _Engine:
    """Stands in for the YuNet detector."""

    def __init__(self, faces, available=True):
        self._faces = faces
        self.available = available

    def detect_image(self, img):
        return self._faces


@pytest.fixture()
def gated(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = True
    straighten.init_schema(conn)
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: True)
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    return straighten.Straightener(cfg), cfg, conn


def test_a_confident_turn_is_declined_when_nobody_is_in_the_picture(gated):
    """A landscape it is sure about is still left alone. That is the point."""
    job, cfg, conn = gated
    job._faces = _Engine(faces=[])

    job.survey([cfg.active_root]); _run(job)

    assert conn.execute(
        "SELECT COUNT(*) n FROM orientation_proposals").fetchone()["n"] == 0
    snapshot = job.progress.snapshot()
    assert snapshot["no_person"] > 0, "it should say why it declined"
    assert snapshot["proposed"] == 0


def test_a_photograph_with_a_person_in_it_is_proposed(gated):
    job, cfg, conn = gated
    job._faces = _Engine(faces=[object()])

    job.survey([cfg.active_root]); _run(job)

    assert conn.execute(
        "SELECT COUNT(*) n FROM orientation_proposals").fetchone()["n"] > 0
    assert job.progress.snapshot()["no_person"] == 0


def test_the_face_is_looked_for_the_way_up_we_propose(gated):
    """Not in the photograph as it stands — in the photograph as it would be.

    A detector finds upright faces, so a face that only appears after the turn
    is evidence for both halves at once: somebody is here, and this is the way
    up they are standing.
    """
    job, cfg, _ = gated
    sizes = []

    class Watcher(_Engine):
        def detect_image(self, img):
            sizes.append(img.size)
            return [object()]

    job._faces = Watcher(faces=[object()])
    job.survey([cfg.active_root]); _run(job)
    assert sizes, "the detector should have been consulted"
    # a 90-degree turn swaps the sides, so a landscape working copy arrives
    # here taller than it is wide
    assert any(h > w for w, h in sizes)


def test_the_survey_refuses_rather_than_straightening_everything(gated):
    """With the gate on and no detector, doing nothing is the safe answer."""
    job, cfg, conn = gated
    job._faces = _Engine(faces=[], available=False)

    job.survey([cfg.active_root]); _run(job)

    snapshot = job.progress.snapshot()
    assert snapshot["status"] == "error"
    assert "people" in snapshot["message"]
    assert conn.execute(
        "SELECT COUNT(*) n FROM orientation_proposals").fetchone()["n"] == 0


def test_turning_the_gate_off_lets_a_landscape_through(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = False
    straighten.init_schema(conn)
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: True)
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    job = straighten.Straightener(cfg)
    job._faces = _Engine(faces=[])

    job.survey([cfg.active_root]); _run(job)

    assert conn.execute(
        "SELECT COUNT(*) n FROM orientation_proposals").fetchone()["n"] > 0


def test_straightening_holds_the_indexer_while_it_runs(tmp_path):
    """Both read every original from the same disk and want every core."""
    import threading
    from ninaivu.media import straighten
    from ninaivu.media.scanner import CLAIM_STRAIGHTEN

    class Held:
        def __init__(self):
            self.log = []

        def held(self, reason):
            from contextlib import contextmanager

            @contextmanager
            def cm():
                self.log.append(("take", reason))
                yield
                self.log.append(("give", reason))
            return cm()

    fake = Held()
    s = straighten.Straightener(type("C", (), {})(), scanner=fake)
    ran = threading.Event()
    assert s._start(lambda: (fake.log.append(("work", "")), ran.set()), "t")
    s._thread.join(5)
    assert ran.is_set()
    assert fake.log == [("take", CLAIM_STRAIGHTEN), ("work", ""),
                        ("give", CLAIM_STRAIGHTEN)]


# ---------------------------------------------------------------------------
# Looking by itself, and remembering how far it looked
# ---------------------------------------------------------------------------

def test_a_second_survey_does_not_look_at_the_same_photographs_again(straightener, monkeypatch):
    """A photograph the model found upright leaves no row behind, so without a
    memory of how far it got a second survey put the whole library through the
    model again. Now it starts where the last one stopped."""
    job, cfg, conn = straightener
    looked = []
    monkeypatch.setattr(orientnet, "predict",
                        lambda img, state_dir=None: (looked.append(1), (0, 0.99))[1])
    job.survey([cfg.active_root])
    _run(job)
    first = len(looked)
    assert first == job.progress.snapshot()["total"] > 0

    job.survey([cfg.active_root])
    _run(job)
    assert len(looked) == first, "nothing new to look at"
    assert job.progress.snapshot()["total"] == 0

    # Start over and look again forgets.
    job.survey([cfg.active_root], rescan=True)
    _run(job)
    assert len(looked) == first * 2


def test_a_stopped_survey_remembers_only_what_it_reached(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (0, 0.99))
    job.survey([cfg.active_root], limit=1)
    _run(job)
    total = len(conn.execute("SELECT id FROM assets WHERE kind='picture' AND trashed=0").fetchall())
    job.survey([cfg.active_root])
    _run(job)
    assert job.progress.snapshot()["total"] == total - 1, "the rest, and only the rest"


def test_after_a_scan_the_survey_runs_by_itself_when_asked_to(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    cfg.straighten_auto = True
    assert job.after_scan([cfg.active_root]) is True
    _run(job)
    assert conn.execute("SELECT COUNT(*) FROM orientation_proposals").fetchone()[0] > 0

    # Nothing new since: it does not even start.
    assert job.after_scan([cfg.active_root]) is False


def test_after_a_scan_the_survey_stays_put_when_switched_off(straightener, monkeypatch):
    job, cfg, conn = straightener
    cfg.straighten_auto = False
    assert job.after_scan([cfg.active_root]) is False
    assert conn.execute("SELECT COUNT(*) FROM orientation_proposals").fetchone()[0] == 0


def test_after_a_scan_without_the_model_nothing_happens_quietly(straightener, monkeypatch):
    job, cfg, _ = straightener
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: False)
    assert job.after_scan([cfg.active_root]) is False
    assert job.progress.snapshot()["status"] == "idle", "no error for a switch left on"


def test_the_switch_is_a_setting_the_console_can_change(scanned):
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    client = login(create_admin_app(services).test_client(), *ADMIN)
    assert client.get("/api/straighten/status").get_json()["auto"] is True, "on by default"
    response = client.post("/api/admin/settings", json={"straighten_auto": False})
    assert response.status_code == 200 and "straighten_auto" in response.get_json()["changed"]
    assert cfg.straighten_auto is False
    assert client.get("/api/straighten/status").get_json()["auto"] is False

    page = client.get("/").get_data(as_text=True)
    assert 'id="st-auto"' in page and 'checked' in page.split('id="st-auto"')[1][:40]


def test_import_is_the_first_library_page(scanned):
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    page = login(create_admin_app(services).test_client(), *ADMIN).get("/").get_data(as_text=True)
    tabs = [t for t in ("archive", "library", "folders", "large-files")]
    order = sorted(tabs, key=lambda t: page.index(f'data-tab="{t}" data-group="library"'))
    assert order == ["archive", "library", "folders", "large-files"]


def test_the_scan_finishing_is_what_starts_it(scanned, monkeypatch):
    from ninaivu import build_services

    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    asked = []
    monkeypatch.setattr(services.straightener, "after_scan", lambda roots: asked.append(roots) or True)
    services._scan_found({"phase": "indexed", "added": 3})
    assert asked == [], "not while the scan is still going"
    services._scan_found({"phase": "done", "added": 0, "updated": 0})
    assert asked == [], "a scan that changed nothing has nothing new to look at"
    services._scan_found({"phase": "done", "added": 3, "updated": 0})
    assert asked == [[cfg.active_root]]


# ---------------------------------------------------------------------------
# Turning what the automatic survey finds, without waiting to be asked
# ---------------------------------------------------------------------------

def _statuses(conn):
    return [r["status"] for r in conn.execute("SELECT status FROM orientation_proposals")]


def test_after_a_scan_what_is_found_is_turned_without_waiting(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    cfg.straighten_auto = True
    cfg.straighten_auto_apply = True

    assert job.after_scan([cfg.active_root]) is True
    _run(job)

    statuses = _statuses(conn)
    assert statuses and set(statuses) == {"applied"}, "nothing should be left waiting"
    turned = conn.execute("SELECT COUNT(*) FROM assets WHERE rotation=90 "
                          "AND rot_source='model'").fetchone()[0]
    assert turned == len(statuses)
    batches = {r[0] for r in conn.execute("SELECT batch FROM orientation_proposals")}
    assert len(batches) == 1 and 0 not in batches, "one run is one batch, so one Undo"


def test_with_auto_apply_off_what_is_found_still_waits(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    cfg.straighten_auto = True
    cfg.straighten_auto_apply = False

    job.after_scan([cfg.active_root])
    _run(job)

    statuses = _statuses(conn)
    assert statuses and set(statuses) == {"pending"}
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE rotation=90").fetchone()[0] == 0


def test_a_survey_somebody_started_is_never_applied_for_them(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    cfg.straighten_auto_apply = True

    job.survey([cfg.active_root])           # the button: they are there to look
    _run(job)

    assert set(_statuses(conn)) == {"pending"}


def test_a_batch_that_was_undone_is_not_turned_again_by_the_next_scan(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    cfg.straighten_auto = True
    cfg.straighten_auto_apply = True
    job.after_scan([cfg.active_root]); _run(job)
    assert job.undo()["restored"] > 0
    assert set(_statuses(conn)) == {"pending"}, "an undone turn goes back to waiting"

    job.survey([cfg.active_root], auto_apply=True); _run(job)

    assert set(_statuses(conn)) == {"pending"}, "it must not be applied a second time"
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE rotation=90").fetchone()[0] == 0


def test_nothing_is_applied_when_the_survey_could_not_run(straightener, monkeypatch):
    job, cfg, conn = straightener
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: False)
    job.survey([cfg.active_root], auto_apply=True)
    _run(job)
    assert job.progress.snapshot()["status"] == "error"
    assert _statuses(conn) == []
