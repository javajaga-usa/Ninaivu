"""The smaller-videos trial (media/video_trial.py): HEVC tried on a sample of
the library's own videos, measured and shown, and nothing in the library
changed."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ADMIN, login
from ninaivu.media import video_trial
from ninaivu.storage import db


def _video_row(conn, root: Path, rel: str, size: int, *, ext: str = "mp4",
               camera: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO assets (root, rel_path, filename, ext, kind, size, camera, width, height, "
        "duration) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(root), rel, Path(rel).name, ext, "video", size, camera, 640, 360, 4.0))
    conn.commit()
    return int(cur.lastrowid)


def _cfg(tmp_path: Path, root: Path) -> SimpleNamespace:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return SimpleNamespace(state_dir=str(state), roots=[str(root)], active_root=str(root))


def _wait(trial: video_trial.VideoTrial, seconds: float = 120.0) -> None:
    deadline = time.monotonic() + seconds
    while trial._thread is not None and trial._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.1)


def test_the_sample_spreads_from_smallest_to_largest_and_leaves_iphone_videos_out(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    root = tmp_path / "lib"
    for n in range(1, 21):
        _video_row(conn, root, f"v{n}.mp4", n * 1000)
    _video_row(conn, root, "phone.mov", 50_000, ext="mov", camera="Apple iPhone 15")
    _video_row(conn, root, "old.avi", 30_000, ext="avi")
    found = video_trial.pick(conn, [str(root)], 5)
    sizes = [r["size"] for r in found]
    assert len(found) == 5 and sizes == sorted(sizes)
    assert sizes[0] == 1000 and sizes[-1] == 30_000      # the AVI is in; the iPhone video is not
    assert "phone.mov" not in {r["rel_path"] for r in found}
    assert len(video_trial.pick(conn, [str(root)], 500)) == 21


def test_the_findings_add_up_and_a_video_no_smaller_counts_as_no_saving(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    root = tmp_path / "lib"
    a = _video_row(conn, root, "a.mp4", 1000)
    b = _video_row(conn, root, "b.mp4", 1000)
    c = _video_row(conn, root, "c.mp4", 1000)
    told = {a: {"outcome": "made", "copy_size": 400, "seconds": 2.0, "ssim": 0.99,
                "tags_kept": 1, "rotation_kept": 1},
            b: {"outcome": "larger", "copy_size": 1200, "seconds": 1.0, "tags_kept": 1,
                "rotation_kept": 1},
            c: {"outcome": "failed", "why": "ffmpeg"}}
    trial = video_trial.VideoTrial(_cfg(tmp_path, root), lambda: conn,
                                   attempt=lambda _state, row: told[row["asset_id"]])
    trial.start(10)
    _wait(trial)
    found = trial.status()
    assert (found["sampled"], found["made"], found["larger"], found["failed"]) == (3, 1, 1, 1)
    assert found["saving"] == pytest.approx(0.3)          # 600 saved of the 2000 tried
    assert found["library_saving_bytes"] == int(3000 * 0.3)
    assert found["ssim_median"] == 0.99 and not found["running"]
    assert video_trial.judge(trial.cfg.state_dir, a, "same")
    assert not video_trial.judge(trial.cfg.state_dir, b, "same")   # no copy to judge
    assert trial.status()["verdicts"] == {"same": 1, "worse": 0}
    # Started again, it does not take a new sample: Clear does.
    trial.start(10)
    _wait(trial)
    assert trial.status()["sampled"] == 3
    assert trial.clear()["sampled"] == 0


def test_a_paused_trial_carries_on_after_a_restart(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    root = tmp_path / "lib"
    ids = [_video_row(conn, root, f"v{n}.mp4", 1000 + n) for n in range(3)]
    tried = []

    def slow(_state, row):
        tried.append(row["asset_id"])
        return {"outcome": "made", "copy_size": 10}
    cfg = _cfg(tmp_path, root)
    first = video_trial.VideoTrial(cfg, lambda: conn, hold=lambda: "busy", attempt=slow)
    first.start(10)
    first.stop(join=True)                                   # Ninaivu stopped while it waited
    assert tried == []
    again = video_trial.VideoTrial(cfg, lambda: conn, attempt=slow)
    again.keep()                                            # the next start
    _wait(again)
    assert sorted(tried) == ids
    assert again.status()["made"] == 3
    paused = video_trial.VideoTrial(cfg, lambda: conn, attempt=slow)
    paused.pause()
    paused.keep()
    assert paused._thread is None


needs_hevc = pytest.mark.skipif(
    not shutil.which("ffmpeg") or b"libx265" not in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, check=False).stdout
    if shutil.which("ffmpeg") else True, reason="needs ffmpeg with libx265")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@needs_hevc
def test_a_real_trial_copy_is_smaller_keeps_its_tags_and_leaves_the_original_alone(tmp_path):
    from ninaivu.mugil import display
    display._hevc = False                                   # look for the encoder again
    root = tmp_path / "lib"
    root.mkdir()
    source = root / "wedding.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=641x361:rate=25:duration=3", "-f", "lavfi", "-i",
                    "sine=duration=3", "-c:v", "libx264", "-crf", "8", "-c:a", "aac",
                    "-metadata", "creation_time=2019-05-01T10:00:00Z", "-metadata",
                    "title=Wedding", str(source)], check=True)
    before, stamp = _digest(source), source.stat().st_mtime_ns
    conn = db.init_db(tmp_path / "index.db")
    asset = _video_row(conn, root, "wedding.mp4", source.stat().st_size)
    trial = video_trial.VideoTrial(_cfg(tmp_path, root), lambda: conn)
    assert trial.available() is None
    trial.start()
    _wait(trial)
    row = video_trial.sample(trial.cfg.state_dir, asset)
    assert row["outcome"] == "made", row
    assert row["copy_size"] < source.stat().st_size
    assert row["tags_kept"] == 1 and row["rotation_kept"] == 1
    assert row["ssim"] > 0.9
    copy = video_trial.copy_path(trial.cfg.state_dir, asset)
    assert copy.is_file() and copy.stat().st_mtime_ns == stamp
    assert _digest(source) == before and source.stat().st_mtime_ns == stamp
    assert sorted(p.name for p in root.iterdir()) == ["wedding.mp4"]
    trial.clear()
    assert not copy.exists()


def test_the_console_runs_the_trial_and_plays_both(scanned, monkeypatch):
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth
    cfg, conn, _ = scanned
    cfg.watch = False
    root = Path(cfg.active_root)
    (root / "clip.mp4").write_bytes(b"original video")
    asset = _video_row(conn, root, "clip.mp4", 14)
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    trial = services.video_trial

    def made(state, row):
        video_trial.copy_path(state, row["asset_id"]).parent.mkdir(parents=True, exist_ok=True)
        video_trial.copy_path(state, row["asset_id"]).write_bytes(b"copy")
        return {"outcome": "made", "copy_size": 4, "ssim": 0.98}
    monkeypatch.setattr(trial, "attempt", made)
    try:
        client = login(create_admin_app(services).test_client(), *ADMIN)
        monkeypatch.setattr(trial, "available", lambda: "this ffmpeg has no HEVC encoder")
        refused = client.post("/api/admin/video-trial/start", json={})
        assert refused.status_code == 409 and refused.get_json()["detail"]
        monkeypatch.setattr(trial, "available", lambda: None)
        assert client.post("/api/admin/video-trial/start", json={"count": "x"}).status_code == 400
        assert client.post("/api/admin/video-trial/start", json={"count": 50}).status_code == 200
        _wait(trial)
        status = client.get("/api/admin/video-trial").get_json()
        assert status["made"] >= 1
        assert asset in {r["asset_id"] for r in status["samples"]}
        assert client.get(f"/api/admin/video-trial/{asset}/copy").data == b"copy"
        assert client.get(f"/api/admin/video-trial/{asset}/original").data == b"original video"
        assert client.get(f"/api/admin/video-trial/{asset}/other").status_code == 404
        assert client.get("/api/admin/video-trial/999999/copy").status_code == 404
        judged = client.post(f"/api/admin/video-trial/{asset}/verdict", json={"verdict": "worse"})
        assert judged.get_json()["verdict"] == "worse"
        assert client.post(f"/api/admin/video-trial/{asset}/verdict",
                           json={"verdict": "maybe"}).status_code == 400
        assert client.post("/api/admin/video-trial/clear").get_json()["sampled"] == 0
        assert (root / "clip.mp4").read_bytes() == b"original video"
    finally:
        services.stop(timeout=5.0)
