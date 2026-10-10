"""The slow passes use more of a machine that has room, and no more than the
Tuning page allows: the orientation survey judges several photographs at once,
tagging opens the next batch while the graphics processor works, and the face
pass reads ahead only as far as the chosen workers."""

from types import SimpleNamespace

import pytest

from ninaivu.media import faceindex, orientnet, straighten
from ninaivu.media.scanner import AI_VERSION, CLAIM_STRAIGHTEN as CLAIM, Scanner


@pytest.mark.parametrize("workers, readers", [
    (1, 1),     # Power saving
    (2, 1),     # a Raspberry Pi's small profile
    (4, 2),
    (12, 6),    # the Mac mini in Performance
    (14, 7),
    (19, 8),    # never more than MAX_READERS
    (None, 1),
    ("x", 1),
])
def test_the_survey_judges_as_many_at_once_as_the_tuning_allows(workers, readers):
    assert straighten.readers_for(SimpleNamespace(workers=workers)) == readers
    assert straighten.readers_for(SimpleNamespace()) == 1


def test_beside_the_scan_the_survey_takes_what_the_tuning_left_it():
    """Once tuned, the survey beside the scan judges as many at once as the
    plan left room for (tuning.survey_beside), so the scan and the survey
    together stay within the ceiling; 0 is no room, so it holds the scan."""
    cfg = SimpleNamespace(workers=15, _survey_beside=2)
    assert straighten.readers_for(cfg) == 2
    assert straighten.readers_for(cfg, held=True) == 7
    cfg._survey_beside = 0
    assert straighten.readers_for(cfg) == 0
    assert straighten.readers_for(cfg, held=True) == 7




def _survey(scanned, monkeypatch, workers):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = False
    cfg.workers = workers
    straighten.init_schema(conn)
    conn.execute("DELETE FROM orientation_proposals")
    conn.execute("DELETE FROM orientation_seen")
    conn.commit()
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: True)
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (90, 0.99))
    job = straighten.Straightener(cfg)
    job.survey([cfg.active_root])
    job._thread.join(30)
    snap = job.progress.snapshot()
    proposals = [tuple(r) for r in conn.execute(
        "SELECT asset_id, rotation, status FROM orientation_proposals ORDER BY asset_id")]
    seen = [r[0] for r in conn.execute("SELECT asset_id FROM orientation_seen ORDER BY asset_id")]
    return snap, proposals, seen


def test_judging_several_at_once_finds_exactly_what_one_at_a_time_did(scanned, monkeypatch):
    one, one_proposals, one_seen = _survey(scanned, monkeypatch, workers=1)
    many, many_proposals, many_seen = _survey(scanned, monkeypatch, workers=12)
    assert one["status"] == many["status"] == "done"
    assert one_proposals and one_proposals == many_proposals
    assert one_seen == many_seen
    for key in ("processed", "proposed", "skipped", "errors", "total"):
        assert one[key] == many[key], key


class _GpuEngine:
    model_id = "fake/gpu"
    semantic = False
    name = "fake"
    device = "mps"
    reads_ahead = True

    def __init__(self):
        self.prepared, self.analysed = [], []

    def prepare(self, paths):
        self.prepared.append(list(paths))
        return [("opened", p) for p in paths]

    def analyse(self, paths, prepared=None):
        assert prepared == [("opened", p) for p in paths], "the batch opened ahead is the one analysed"
        self.analysed.append(list(paths))
        return [{"tags": ["beach"], "caption": "beach"} for _ in paths]


def test_tagging_opens_the_next_batch_ahead_on_a_graphics_processor(scanned):
    cfg, conn, _ = scanned
    cfg.clip_batch_size = 2
    conn.execute("UPDATE assets SET ai_version=0")
    conn.commit()
    engine = _GpuEngine()
    Scanner(cfg, ai=engine)._tag(conn, str(cfg.active_root))

    assert engine.analysed and engine.prepared == engine.analysed
    done = conn.execute(
        "SELECT COUNT(*) FROM assets WHERE ai_version=? AND kind != 'audio'",
        (AI_VERSION,)).fetchone()[0]
    assert done == sum(len(b) for b in engine.analysed)


@pytest.mark.parametrize("workers, readers", [(1, 1), (2, 2), (4, 4), (12, 6), (15, 7), (17, 8),
                                              (64, 8), (0, None), (None, None)])
def test_the_face_pass_reads_ahead_only_as_far_as_the_tuning_allows(workers, readers):
    got = faceindex.readers_for(SimpleNamespace(workers=workers))
    if readers is None:     # not set: the processor count decides, as before
        assert 2 <= got <= 4
    else:
        assert got == readers


class _Scanner:
    def __init__(self, ai=None):
        self.ai = ai
        self.claims = []
        self._thread = None

    def held(self, reason):
        from contextlib import contextmanager

        @contextmanager
        def hold():
            self.claims.append(reason)
            yield
        return hold()


def _beside(monkeypatch, *, workers=12, gpu=True, solid=True):
    from ninaivu.server import capacity
    monkeypatch.setattr(capacity, "storage",
                        lambda path, role: {"solid_state": solid})
    cfg = SimpleNamespace(workers=workers, library_roots=["/library"])
    ai = SimpleNamespace(reads_ahead=gpu)
    return straighten.Straightener(cfg, scanner=_Scanner(ai))._room_beside_the_scan()


def test_the_survey_runs_beside_the_scan_only_where_there_is_room(monkeypatch):
    assert _beside(monkeypatch) is True                       # the Mac mini
    assert _beside(monkeypatch, gpu=False) is False           # processor only
    assert _beside(monkeypatch, solid=False) is False         # spinning disk
    assert _beside(monkeypatch, solid=None) is False          # disk not known
    assert _beside(monkeypatch, workers=2) is False           # Pi, Power saving


def test_a_survey_beside_the_scan_still_holds_it_to_turn_photographs(monkeypatch, tmp_path):
    from ninaivu.server import capacity
    monkeypatch.setattr(capacity, "storage", lambda path, role: {"solid_state": True})
    cfg = SimpleNamespace(workers=12, library_roots=["/library"])
    scanner = _Scanner(SimpleNamespace(reads_ahead=True))
    job = straighten.Straightener(cfg, scanner=scanner)
    calls = []
    monkeypatch.setattr(job, "_survey_all", lambda *a: calls.append(("look", list(scanner.claims))))
    monkeypatch.setattr(job, "_turn_what_was_found",
                        lambda conn, since: calls.append(("turn", list(scanner.claims))))
    monkeypatch.setattr(straighten.db, "connect", lambda path: None)
    monkeypatch.setattr(straighten, "init_schema", lambda conn: None)
    monkeypatch.setattr(straighten.resume, "want", lambda *a, **k: None)
    monkeypatch.setattr(straighten.resume, "done", lambda *a, **k: None)
    cfg.db_path = tmp_path / "x.db"

    job.survey(["/library"], auto_apply=True)
    job._thread.join(10)
    assert calls == [("look", []), ("turn", [CLAIM])]

    # Applying by hand always holds the scan.
    scanner.claims.clear()
    monkeypatch.setattr(job, "_apply", lambda *a: calls.append(("apply", list(scanner.claims))))
    job.apply([1])
    job._thread.join(10)
    assert calls[-1] == ("apply", [CLAIM])


@pytest.mark.parametrize("left, room", [(0, False), (2, True), (6, True)])
def test_the_survey_beside_the_scan_is_held_when_the_tuning_left_no_room(monkeypatch, left, room):
    from ninaivu.server import capacity
    monkeypatch.setattr(capacity, "storage", lambda path, role: {"solid_state": True})
    cfg = SimpleNamespace(workers=15, library_roots=["/library"], _survey_beside=left)
    scanner = _Scanner(SimpleNamespace(reads_ahead=True))
    module_level = getattr(straighten, "room_beside_the_scan", None)
    if module_level is not None:
        assert module_level(cfg, scanner) is room
    else:
        assert straighten.Straightener(cfg, scanner=scanner)._room_beside_the_scan() is room
