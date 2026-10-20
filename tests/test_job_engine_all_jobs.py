"""The rest of a scan on the job engine: sound files' pictures, videos
described by their moments, fingerprints, and grouping faces into people.

With ``job_engine`` on, each of these runs as tasks on worker processes (or,
for the grouping, one worker process), and must leave the same index behind
as the 1.x passes do with it off.
"""
from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import threading
import time
import wave
from concurrent.futures import CancelledError
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import engine_helpers
from ninaivu import engine as jobs
from ninaivu.engine import derivations
from ninaivu.engine.engine import JobEngine, _Sent
from ninaivu.engine.pool import BrokenProcessPool, WorkerPool
from ninaivu.storage import db

FFMPEG = shutil.which("ffmpeg")


class Describer:
    """A stand-in image model whose answer depends only on the picture, so
    the same frames give the same description in any process, in any order."""

    name = "stand-in"
    model_id = "stand-in-v1"
    semantic = False
    reads_ahead = False

    def analyse(self, paths, **_):
        out = []
        for path in paths:
            with Image.open(path) as image:
                red, green, blue = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
            out.append({"tags": [f"red{red // 64}", f"blue{blue // 64}"],
                        "caption": f"mostly {red}/{green}/{blue}",
                        "embedding": np.array([red, green, blue, 1], "float32").tobytes()})
        return out


def _tone(path: Path, seconds: float = 1.0, rate: int = 8000) -> Path:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(i / 5)))
                                 for i in range(frames)))
    return path


def _rows(conn):
    skip = {"id", "indexed_at", "added_at"}
    return {r["rel_path"]: {k: r[k] for k in r.keys() if k not in skip}
            for r in conn.execute("SELECT * FROM assets")}


def _by_path(conn, sql):
    return {r[0]: tuple(r[1:]) for r in conn.execute(sql)}


def _thumbs(state: Path) -> list[str]:
    return sorted(p.name for p in (state / "thumbs").rglob("*") if p.is_file())


def test_a_whole_scan_on_the_engine_leaves_the_same_index_as_without(cfg, tmp_path,
                                                                     monkeypatch):
    """Sound files' pictures, videos' descriptions, fingerprints, tags and
    words: the same rows, vectors, fingerprints and thumbnails either way."""
    from ninaivu.media.scanner import Scanner
    from ninaivu.server import auth
    ran: list[str] = []
    real_run = JobEngine.run
    monkeypatch.setattr(JobEngine, "run", lambda self, conn, ctx, kinds, **kw: (
        ran.extend(kinds), real_run(self, conn, ctx, kinds, **kw))[1])

    library = Path(cfg.active_root)
    _tone(library / "misc" / "voice.wav")
    _tone(library / "misc" / "song.wav", seconds=1.5)
    if FFMPEG:
        for name, colour in (("beach.mp4", "blue"), ("lamp.mp4", "orange")):
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                            f"color=c={colour}:size=160x120:duration=2:rate=10",
                            "-pix_fmt", "yuv420p", str(library / "misc" / name)],
                           check=True, timeout=60)
    cfg.ai_enabled = True
    cfg.video_keyframes = 3
    cfg.content_fingerprints = True

    def scan(engine_on: bool, name: str):
        cfg.state_dir = tmp_path / name
        cfg.job_engine = engine_on
        cfg.ensure_dirs()
        conn = db.init_db(cfg.db_path)
        auth.init_auth_schema(conn)
        scanner = Scanner(cfg)
        scanner.ai = Describer()
        scanner._run(library, full=True)
        assert scanner.progress.snapshot()["status"] == "done"
        # A sound file whose picture went: drawn again by the covers pass.
        voice = conn.execute("SELECT thumb FROM assets WHERE filename='voice.wav'").fetchone()[0]
        for path in cfg.thumbs_dir.rglob(f"{voice}_*"):
            path.unlink()
        scanner._draw_audio_art(conn, str(library))
        scanner.close()
        return conn

    before = scan(False, "off")
    assert ran == []
    after = scan(True, "on")
    assert {"covers", "fingerprints"} <= set(ran)
    assert "keyframes" in ran or not FFMPEG
    assert _rows(after) == _rows(before)
    joined = ("SELECT a.rel_path, {cols} FROM {table} t JOIN assets a ON a.id=t.asset_id")
    assert _by_path(after, joined.format(cols="t.algo, t.digest, t.size, t.mtime",
                                         table="content_fingerprints")) == \
        _by_path(before, joined.format(cols="t.algo, t.digest, t.size, t.mtime",
                                       table="content_fingerprints"))
    assert len(_by_path(after, joined.format(cols="t.digest", table="content_fingerprints"))) \
        == after.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    assert _by_path(after, joined.format(cols="t.model, t.vector", table="embeddings")) == \
        _by_path(before, joined.format(cols="t.model, t.vector", table="embeddings"))
    assert _thumbs(tmp_path / "on") == _thumbs(tmp_path / "off")
    if FFMPEG:
        clips = _by_path(after, "SELECT rel_path, tags, keyframe_version FROM assets "
                                "WHERE kind='video'")
        assert all(json.loads(tags) and version for tags, version in clips.values())
    # Nothing is left in the engine's list: every task settled.
    store = jobs.TaskStore(jobs.database_path(cfg))
    assert {k: v for k, v in store.counts().items() if v.get("queued")} == {}
    store.close()


# -- the engine's own parts ----------------------------------------------------

def _owed(conn, root, version):
    return [(int(r[0]), float(r[0])) for r in conn.execute(
        "SELECT id FROM assets WHERE root=? AND ocr_version < ? AND visibility < 2",
        (root, version))]


@pytest.fixture()
def index(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    for i in range(1, 9):
        db.upsert_asset(conn, {"root": "/lib", "rel_path": f"{i}.jpg", "filename": f"{i}.jpg",
                               "folder": "", "ext": "jpg", "kind": "picture", "size": 1,
                               "mtime": float(i)})
    conn.commit()
    yield conn
    db.close_all()


class _Cfg:
    def __init__(self, tmp_path):
        self.log = str(tmp_path / "worked")
        self.db_path = tmp_path / "index.db"
        self.workers = 3


def test_a_kind_held_to_a_limit_never_has_more_in_flight(index, tmp_path, monkeypatch):
    """Fingerprints read a whole file each: two at a time, however many
    workers there are, while the other kinds use the rest."""
    def jobs_for(ctx, conn, items):
        return {i: (engine_helpers.shout, (ctx.cfg.log, i, f"{i}")) for i in items}

    def store(ctx, conn, item, word):
        db.store_ai_fields(conn, item, ocr_text=word, ocr_version=1)
        return True

    monkeypatch.setitem(derivations.DERIVATIONS, "one", derivations.Derivation(
        "one", 5, lambda: 1, _owed, jobs_for, store, lambda *a: None,
        at_once=lambda ctx: 1))
    monkeypatch.setitem(derivations.DERIVATIONS, "free", derivations.Derivation(
        "free", 1, lambda: 1, _owed, jobs_for, store, lambda *a: None))
    cfg = _Cfg(tmp_path)
    engine = JobEngine(cfg)
    ctx = jobs.Context(cfg=cfg, root="/lib")
    engine.fill(index, "/lib", ["one", "free"])
    claimed, capped = engine._claim(ctx, ["one", "free"], [], 6)
    kinds = [t.kind for t in claimed]
    assert kinds.count("one") == 1 and kinds.count("free") == 5 and not capped
    busy = [_Sent(t, None, (), 0) for t in claimed]
    claimed, capped = engine._claim(ctx, ["one", "free"], busy, 6)
    assert capped and all(t.kind == "free" for t in claimed)
    # Only the held kind left, and at its limit: waited for, not "run out".
    engine.store.release([t.id for t in claimed])
    assert engine._claim(ctx, ["one"], busy, 6) == ([], True)
    engine.close()


def test_a_run_with_a_held_kind_still_works_everything_out(index, tmp_path, monkeypatch):
    def jobs_for(ctx, conn, items):
        return {i: (engine_helpers.slow, (ctx.cfg.log, i, f"w{i}")) for i in items}

    def store(ctx, conn, item, word):
        db.store_ai_fields(conn, item, ocr_text=word, ocr_version=1)
        return True

    monkeypatch.setitem(derivations.DERIVATIONS, "one", derivations.Derivation(
        "one", 5, lambda: 1, _owed, jobs_for, store, lambda *a: None,
        at_once=lambda ctx: 2))
    cfg = _Cfg(tmp_path)
    report = JobEngine(cfg).run(index, jobs.Context(cfg=cfg, root="/lib"), ["one"])
    assert report.done == {"one": 8} and report.left == 0
    assert {r[0] for r in index.execute("SELECT ocr_text FROM assets")} == \
        {f"W{i}" for i in range(1, 9)}


def test_ending_a_pool_ends_a_task_mid_way(tmp_path):
    pool = WorkerPool(1)
    future = pool.submit(engine_helpers.nap, 60)
    time.sleep(1.0)                       # started, and asleep
    started = time.monotonic()
    pool.terminate()
    with pytest.raises((BrokenProcessPool, CancelledError)):
        future.result(timeout=30)
    assert time.monotonic() - started < 30
    pool.shutdown()


# -- grouping faces on a worker process -----------------------------------------

def _faces(conn, root):
    """One named person with three confirmed faces, four more faces like
    theirs, six of somebody nobody has named, and a few strangers."""
    rng = np.random.default_rng(7)
    ids = [int(r[0]) for r in conn.execute("SELECT id FROM assets WHERE root=? ORDER BY id",
                                           (root,))]
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES(1,'Maya',?)",
                 (time.time(),))
    maya, other = rng.normal(size=128), rng.normal(size=128)

    def face(asset, vector, person=None, source="none"):
        vector = (vector / np.linalg.norm(vector)).astype("float32")
        conn.execute(
            "INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality, "
            "created_at) VALUES(?,?,?,?,?,0.8,?)",
            (asset, person, source, json.dumps([0, 0, 90, 90]), vector.tobytes(), time.time()))

    for n in range(3):
        face(ids[n], maya + rng.normal(scale=0.05, size=128), 1, "confirmed")
    for n in range(4):
        face(ids[3 + n], maya + rng.normal(scale=0.08, size=128))
    for n in range(6):
        face(ids[n % len(ids)], other + rng.normal(scale=0.08, size=128))
    for n in range(3):
        face(ids[n], rng.normal(size=128))
    conn.commit()


def _groups(plan):
    keyed = {}
    for face_id, key in plan["pairs"]:
        keyed.setdefault(key, set()).add(face_id)
    return sorted(sorted(v) for k, v in keyed.items() if k), sorted(keyed.get(None, ()))


def test_faces_are_grouped_on_a_worker_as_they_are_here(scanned):
    from ninaivu.media import faceindex
    cfg, conn, scanner = scanned
    root = cfg.active_root
    _faces(conn, root)
    here = faceindex.plan_regroup(conn, root)
    there = scanner._plan_faces_on_a_worker(root, lambda: False)
    assert there["assigned"] == here["assigned"] and len(here["assigned"]) == 4
    assert _groups(there) == _groups(here) and _groups(here)[0]
    assert there["people"] == here["people"] == [1]

    # Written by the server: the faces like Maya's are hers, the others a group.
    indexer = faceindex.FaceIndexer.__new__(faceindex.FaceIndexer)
    indexer.cfg = cfg
    result = indexer.regroup(conn, root, planner=scanner._plan_faces_on_a_worker)
    assert result["auto_assigned"] == 4 and result["clusters"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id=1").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE cluster_key IS NOT NULL").fetchone()[0] >= 3


def test_a_stop_ends_the_grouping_on_its_worker(scanned, monkeypatch):
    from ninaivu.media import faceindex
    cfg, conn, scanner = scanned
    monkeypatch.setattr(jobs.work, "plan_faces", engine_helpers.plan_slowly)
    asked = threading.Event()

    def stop():
        if asked.is_set():
            return True
        asked.set()
        return False

    started = time.monotonic()
    with pytest.raises(faceindex._Stopped):
        scanner._plan_faces_on_a_worker(cfg.active_root, stop)
    assert time.monotonic() - started < 30


def test_a_worker_that_dies_grouping_faces_leaves_it_to_the_server(scanned, monkeypatch):
    from ninaivu.media import faceindex
    cfg, conn, scanner = scanned
    _faces(conn, cfg.active_root)
    monkeypatch.setattr(jobs.work, "plan_faces", engine_helpers.die)
    plan = scanner._plan_faces_on_a_worker(cfg.active_root, lambda: False)
    assert plan["assigned"] == faceindex.plan_regroup(conn, cfg.active_root)["assigned"]


def test_a_regroup_written_from_a_plan_is_the_regroup_of_1x(scanned):
    """In 1.x the regroup wrote the people's faces, then read the loose ones
    again to group them. Worked out in one go, the groups are the same."""
    from ninaivu.media import faceindex
    cfg, conn, _ = scanned
    _faces(conn, cfg.active_root)
    loose_before = {r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id IS NULL")}
    plan = faceindex.plan_regroup(conn, cfg.active_root)
    taken = {face_id for face_id, *_ in plan["assigned"]}
    grouped = {face_id for face_id, _ in plan["pairs"]}
    assert grouped == loose_before - taken


def test_a_clip_that_brings_its_worker_down_twice_is_stamped_done(index):
    from ninaivu.media.scanner import KEYFRAME_VERSION
    keyframes = derivations.DERIVATIONS["keyframes"]
    item = index.execute("SELECT MIN(id) FROM assets").fetchone()[0]
    keyframes.give_up(None, index, item)
    assert index.execute("SELECT keyframe_version FROM assets WHERE id=?",
                         (item,)).fetchone()[0] == KEYFRAME_VERSION


def test_a_clip_left_for_later_is_not_stamped_and_one_unreadable_is(index):
    keyframes = derivations.DERIVATIONS["keyframes"]
    ctx = jobs.Context(cfg=None, root="/lib")
    first, second = [r[0] for r in index.execute("SELECT id FROM assets ORDER BY id LIMIT 2")]
    later = keyframes.prepare(ctx, index, first, {"fields": {"duration": 4.0}, "frames": None,
                                                  "later": True})
    gone = keyframes.prepare(ctx, index, second, {"fields": {}, "frames": None})
    with db.write_group(index) as group:
        with group.item() as c:
            assert not keyframes.store(ctx, c, first, later)
        with group.item() as c:
            assert not keyframes.store(ctx, c, second, gone)
    rows = dict(index.execute("SELECT id, keyframe_version FROM assets WHERE id IN (?,?)",
                              (first, second)).fetchall())
    assert rows[first] == 0 and rows[second] == keyframes.version()
    # What the probe found is kept either way, as in the 1.x pass.
    assert index.execute("SELECT duration FROM assets WHERE id=?", (first,)).fetchone()[0] == 4.0


def test_a_fingerprint_of_a_file_that_changed_since_it_was_listed_is_not_kept(index):
    fingerprints = derivations.DERIVATIONS["fingerprints"]
    item = index.execute("SELECT id FROM assets WHERE filename='2.jpg'").fetchone()[0]
    ctx = jobs.Context(cfg=None, root="/lib")
    with db.write_group(index) as group:
        with group.item() as c:
            assert not fingerprints.store(ctx, c, item, ("ab" * 32, 99, 2.0))
        with group.item() as c:
            assert fingerprints.store(ctx, c, item, ("cd" * 32, 1, 2.0))
        with group.item() as c:
            assert not fingerprints.store(ctx, c, item, "missing")
    assert index.execute("SELECT digest FROM content_fingerprints WHERE asset_id=?",
                         (item,)).fetchone()[0] == "cd" * 32


def test_work_that_could_not_be_done_this_time_is_left_owed_not_set_aside(
        index, tmp_path, monkeypatch):
    def jobs_for(ctx, conn, items):
        return {i: (engine_helpers.again, (ctx.cfg.log, i, "x")) for i in items}

    monkeypatch.setitem(derivations.DERIVATIONS, "full", derivations.Derivation(
        "full", 1, lambda: 1, _owed, jobs_for, lambda *a: True, lambda *a: None))
    cfg = _Cfg(tmp_path)
    engine = JobEngine(cfg)
    for _ in range(3):
        report = engine.run(index, jobs.Context(cfg=cfg, root="/lib"), ["full"])
        assert report.total_done() == 0 and report.failed == {"full": 0}
        assert report.left == 8
    assert engine.store.counts("/lib") == {"full": {"queued": 8}}
    engine.close()
