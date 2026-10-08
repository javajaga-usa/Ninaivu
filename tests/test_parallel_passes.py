"""The slow passes use more of a machine that has room, and no more than the
Tuning page allows: the orientation survey judges several photographs at once,
tagging opens the next batch while the graphics processor works, and the face
pass reads ahead only as far as the chosen workers."""

from types import SimpleNamespace

import pytest

from ninaivu.media import faceindex, orientnet, straighten
from ninaivu.media.scanner import AI_VERSION, Scanner


@pytest.mark.parametrize("workers, readers", [
    (1, 1),     # Power saving
    (2, 1),     # a Raspberry Pi's small profile
    (4, 2),
    (12, 6),    # the Mac mini in Performance
    (19, 6),    # never more than MAX_READERS
    (None, 1),
    ("x", 1),
])
def test_the_survey_judges_as_many_at_once_as_the_tuning_allows(workers, readers):
    assert straighten.readers_for(SimpleNamespace(workers=workers)) == readers
    assert straighten.readers_for(SimpleNamespace()) == 1


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


@pytest.mark.parametrize("workers, readers", [(1, 1), (2, 2), (12, 4), (0, None), (None, None)])
def test_the_face_pass_reads_ahead_only_as_far_as_the_tuning_allows(workers, readers):
    got = faceindex.readers_for(SimpleNamespace(workers=workers))
    if readers is None:     # not set: the processor count decides, as before
        assert 2 <= got <= 4
    else:
        assert got == readers
