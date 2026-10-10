"""Audit of 10 October 2026: the orientation survey and apply, and the face
pass. Each test was a probe that showed the fault on main."""

import shutil
import sqlite3
import threading
import time
from pathlib import Path

from ninaivu.media import faceindex, faces as faces_mod, media, orientnet, straighten
from ninaivu.storage import db, resume


def _model_says_turn(monkeypatch, looked=None):
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: True)

    def predict(img, state_dir=None):
        if looked is not None:
            looked.append(1)
        return (90, 0.99)
    monkeypatch.setattr(orientnet, "predict", predict)


class _CountingFaces:
    available = True

    def __init__(self):
        self.calls = 0

    def detect_image(self, img):
        self.calls += 1
        return [object()]


def test_the_survey_never_reads_admins_only_photographs(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = True
    straighten.init_schema(conn)
    conn.execute("UPDATE assets SET visibility=2")
    conn.commit()
    looked = []
    _model_says_turn(monkeypatch, looked)
    job = straighten.Straightener(cfg)
    job._faces = _CountingFaces()
    job.survey([cfg.active_root], auto_apply=True)
    job._thread.join(30)
    assert looked == [] and job._faces.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE rot_source='model'").fetchone()[0] == 0


def test_apply_holds_no_write_open_between_photographs(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = False
    straighten.init_schema(conn)
    _model_says_turn(monkeypatch)
    job = straighten.Straightener(cfg)
    job.survey([cfg.active_root])
    job._thread.join(30)
    assert conn.execute("SELECT COUNT(*) FROM orientation_proposals "
                        "WHERE status='pending'").fetchone()[0] >= 3
    real = media.write_thumbnails

    def slow(*a, **k):                     # a large original's decode and resizes
        time.sleep(0.4)
        return real(*a, **k)
    monkeypatch.setattr(media, "write_thumbnails", slow)
    job.apply(None, 0.0)
    time.sleep(0.6)
    other = sqlite3.connect(str(cfg.db_path), timeout=2.0, check_same_thread=False)
    error = None
    with db._write_lock:                   # a web request saving a favourite
        try:
            other.execute("UPDATE assets SET size=size WHERE id=(SELECT MIN(id) FROM assets)")
            other.commit()
        except sqlite3.OperationalError as exc:
            error = exc
    other.close()
    job._thread.join(60)
    assert error is None
    assert job.progress.snapshot()["status"] == "done"


def test_undo_waits_for_a_running_apply(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = False
    straighten.init_schema(conn)
    _model_says_turn(monkeypatch)
    job = straighten.Straightener(cfg)
    job.survey([cfg.active_root])
    job._thread.join(30)
    real = media.write_thumbnails
    gate = threading.Event()

    def held(*a, **k):
        gate.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(media, "write_thumbnails", held)
    job.apply(None, 0.0)
    try:
        time.sleep(0.2)
        assert job.undo()["busy"] is True
    finally:
        gate.set()
        job._thread.join(60)
    out = job.undo()
    assert out["restored"] > 0
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE rot_source='model'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM orientation_proposals "
                        "WHERE status='applied'").fetchone()[0] == 0


def test_a_survey_that_fails_says_so_and_keeps_its_place(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = False
    straighten.init_schema(conn)
    _model_says_turn(monkeypatch)

    def locked(conn, pending):
        if pending:
            raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(straighten.Straightener, "_flush", staticmethod(locked))
    job = straighten.Straightener(cfg)
    job.survey([cfg.active_root])
    job._thread.join(30)
    snap = job.progress.snapshot()
    assert not job.running
    assert snap["status"] == "error" and snap["running"] is False
    assert straighten.RESUME_NAME in resume.wanted(conn)


class _NoFaces:
    available = True
    model_id = "fake"
    unavailable_reason = ""

    def detect(self, image):
        return []


def test_a_library_drive_gone_mid_pass_is_not_marked_face_scanned(scanned):
    cfg, conn, _ = scanned
    root = str(cfg.active_root)
    indexer = faceindex.FaceIndexer.__new__(faceindex.FaceIndexer)
    indexer.cfg = cfg
    indexer.engine = _NoFaces()
    indexer.crops_dir = Path(cfg.state_dir) / "crops"
    version = faces_mod.FACE_VERSION
    before = db.count_assets_needing_faces(conn, root, version)
    assert before > 0
    away = Path(root + "-away")
    shutil.move(root, away)
    try:
        indexer.detect_pass(conn, root)
    finally:
        shutil.move(away, root)
    assert db.count_assets_needing_faces(conn, root, version) == before
    # And with the drive back, the pass reads them.
    indexer.detect_pass(conn, root)
    assert db.count_assets_needing_faces(conn, root, version) == 0
