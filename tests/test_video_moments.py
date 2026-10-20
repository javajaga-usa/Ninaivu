"""Moments inside videos: search finds the second inside a long clip.

Off until switched on; a long clip is read once, a frame every few seconds,
and frames that look alike are folded into scenes, each kept with the second
it starts at; a search answers with the clips and those seconds, only those
the viewer may see; a clip hidden from AI loses its moments, and one changed
on disk is read again; a scene's picture is read only for a scene that was
found.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from ninaivu.media import media
from ninaivu.media import video_moments as finder_mod
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db, video_moments

needs_ffmpeg = pytest.mark.skipif(not media.FFMPEG, reason="ffmpeg is not installed")

RED, BLUE, GREEN = np.eye(4, dtype="float32")[:3]


class Colours:
    """A picture-search model that knows three colours: a frame's vector is
    its main colour, and the words "red", "blue" and "green" theirs."""

    semantic = True
    model_id = "colours/test"
    search_floor = 0.5
    info = {"engine": "test", "semantic": True, "model": "colours/test"}

    def __init__(self) -> None:
        self.frames = 0

    @staticmethod
    def _of(image) -> np.ndarray:
        r, g, b = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
        return {0: RED, 1: GREEN, 2: BLUE}[int(np.argmax([r, g, b]))]

    def embed_images(self, images):
        self.frames += len(images)
        return np.stack([self._of(i) for i in images])

    def encode_text(self, query: str):
        return {"red": RED, "blue": BLUE, "green": GREEN}.get(query.strip().lower())


def _clip(path: Path, colours: list[str], seconds: int) -> None:
    inputs, labels = [], []
    for n, colour in enumerate(colours):
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:size=160x120:duration={seconds}:rate=5"]
        labels.append(f"[{n}]")
    subprocess.run([media.FFMPEG, "-v", "error", "-y", *inputs, "-filter_complex",
                    f"{''.join(labels)}concat=n={len(colours)}:v=1[v]", "-map", "[v]",
                    "-g", "5", "-pix_fmt", "yuv420p", str(path)], check=True, timeout=60)


@pytest.fixture()
def clips(app, people, scanned):
    """A party clip (12 s red, 12 s blue) and a short one (6 s green), in
    the library and indexed; the colour model loaded; the switch on."""
    if not media.FFMPEG:
        pytest.skip("ffmpeg is not installed")
    cfg, conn, scanner = scanned
    folder = Path(cfg.active_root) / "videos"
    folder.mkdir()
    _clip(folder / "party.mp4", ["red", "blue"], 12)
    _clip(folder / "short.mp4", ["green"], 6)
    scanner._run(Path(cfg.active_root), full=False)
    party = conn.execute("SELECT id, duration FROM assets WHERE filename='party.mp4'").fetchone()
    short = conn.execute("SELECT id FROM assets WHERE filename='short.mp4'").fetchone()
    assert party and party["duration"] >= 20
    cfg.video_moments = True
    cfg.ai_enabled = True
    engine = Colours()
    services = app.config["MV_SERVICES"]
    services.engine = engine
    finder = finder_mod.MomentFinder(cfg, lambda: db.connect(cfg.db_path),
                                     engine=lambda: engine)
    return {"party": int(party["id"]), "short": int(short["id"]), "conn": conn, "cfg": cfg,
            "engine": engine, "finder": finder, "services": services}


def test_off_by_default_and_under_advanced():
    assert Config().video_moments is False
    assert "video_moments" in GROUPS["Advanced"]


def test_every_route_is_404_until_switched_on(as_family):
    assert as_family.get("/api/moments?q=red").status_code == 404
    assert as_family.get("/api/moments/1").status_code == 404
    assert as_family.get("/api/moments/1/frame?at=0").status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["video_moments"] is False


def test_frames_that_look_alike_are_one_scene():
    frames = [(0, RED), (2, RED), (4, RED * 0.95 + GREEN * 0.05), (6, BLUE), (8, BLUE), (10, RED)]
    found = finder_mod.scenes(frames)
    assert [at for at, _ in found] == [0, 6, 10]
    assert all(abs(float(np.linalg.norm(v)) - 1) < 1e-5 for _, v in found)
    many = finder_mod.scenes([(i, np.eye(200, dtype="float32")[i]) for i in range(200)], most=10)
    assert len(many) == 10 and many[0][0] == 0 and many[-1][0] == 199


def test_a_long_clip_is_read_sparsely():
    assert finder_mod.step_for(30) == finder_mod.MIN_STEP
    assert finder_mod.step_for(1200) == 1200 / finder_mod.MAX_FRAMES


@needs_ffmpeg
def test_frames_come_with_their_seconds(tmp_path):
    _clip(tmp_path / "c.mp4", ["red", "blue"], 6)
    read = list(finder_mod.read_frames(tmp_path / "c.mp4", 12))
    assert [at for at, _ in read] == [0, 2, 4, 6, 8, 10]
    assert [Colours._of(image).tolist() for _, image in read] == [RED.tolist()] * 3 + [BLUE.tolist()] * 3


def test_a_long_clip_becomes_its_scenes_and_a_short_one_is_left(clips):
    state = clips["finder"].run()
    assert state["done"] == 1 and state["failed"] == 0 and state["owed"] == 0
    conn = clips["conn"]
    assert video_moments.of_clip(conn, clips["party"]) == [0.0, 12.0]
    assert video_moments.of_clip(conn, clips["short"]) == []
    assert video_moments.counts(conn) == {"clips": 1, "moments": 2}
    read = clips["engine"].frames
    clips["finder"].run()
    assert clips["engine"].frames == read, "a clip is read once"


def test_a_search_opens_the_clip_at_the_second(clips, as_family):
    clips["finder"].run()
    got = as_family.get("/api/moments?q=blue").get_json()
    assert got["ready"] is True and got["looked_clips"] == 1
    assert [(c["id"], c["moments"][0]["at"]) for c in got["clips"]] == [(clips["party"], 12.0)]
    assert got["clips"][0]["name"] == "party.mp4" and got["clips"][0]["duration"] >= 20
    assert as_family.get("/api/moments?q=green").get_json()["clips"] == []
    assert as_family.get("/api/moments?q=").get_json()["clips"] == []
    assert as_family.get(f"/api/moments/{clips['party']}").get_json()["moments"] == [0.0, 12.0]
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["video_moments"] is True


def test_a_scene_picture_only_for_a_scene_that_was_found(clips, as_family):
    clips["finder"].run()
    party = clips["party"]
    picture = as_family.get(f"/api/moments/{party}/frame?at=12")
    assert picture.status_code == 200 and picture.mimetype == "image/jpeg"
    assert picture.data[:2] == b"\xff\xd8"
    assert as_family.get(f"/api/moments/{party}/frame?at=7").status_code == 404
    assert as_family.get(f"/api/moments/{party}/frame?at=x").status_code == 400
    assert as_family.get(f"/api/moments/{clips['short']}/frame?at=0").status_code == 404


def test_only_what_the_viewer_may_see(clips, people, as_family, as_admin):
    clips["finder"].run()
    conn, party = clips["conn"], clips["party"]
    # Kept "only to me" by the administrator: the family is told nothing of it.
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET visibility=2, private_to=? WHERE id=?",
                     (people["admin"].id, party))
        conn.commit()
    assert as_family.get("/api/moments?q=red").get_json()["clips"] == []
    assert as_family.get(f"/api/moments/{party}").status_code == 404
    assert as_family.get(f"/api/moments/{party}/frame?at=0").status_code == 404
    assert [c["id"] for c in as_admin.get("/api/moments?q=red").get_json()["clips"]] == [party]


def test_hidden_from_ai_forgets_its_moments_and_is_not_read_again(clips):
    clips["finder"].run()
    conn, party = clips["conn"], clips["party"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (party,))
        conn.commit()
    db.forget_ai_reading(conn, ids=[party])
    assert video_moments.of_clip(conn, party) == []
    assert video_moments.count_owed(conn, Colours.model_id) == 0
    clips["finder"].run()
    assert video_moments.of_clip(conn, party) == []


def test_a_clip_changed_on_disk_or_a_new_model_reads_it_again(clips):
    clips["finder"].run()
    conn, party = clips["conn"], clips["party"]
    assert video_moments.count_owed(conn, Colours.model_id) == 0
    assert video_moments.count_owed(conn, "another/model") == 1
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET mtime=mtime+10 WHERE id=?", (party,))
        conn.commit()
    assert video_moments.count_owed(conn, Colours.model_id) == 1
    clips["finder"].run()
    assert video_moments.count_owed(conn, Colours.model_id) == 0


def test_a_clip_that_cannot_be_read_is_set_aside_after_two_tries(clips, monkeypatch):
    monkeypatch.setattr(finder_mod, "read_frames", lambda *a, **k: iter(()))
    conn = clips["conn"]
    assert clips["finder"].run()["failed"] == 1
    assert video_moments.count_owed(conn, Colours.model_id) == 1
    assert clips["finder"].run()["failed"] == 1
    assert video_moments.count_owed(conn, Colours.model_id) == 0


def test_it_waits_for_the_household_and_needs_a_model(clips):
    cfg, conn = clips["cfg"], clips["conn"]
    none = finder_mod.MomentFinder(cfg, lambda: db.connect(cfg.db_path), engine=lambda: None)
    assert "picture-search" in none.run()["message"]
    calls = []

    def hold():
        calls.append(1)
        if len(calls) > 2:
            finder.stop()
        return "someone is watching a video"

    finder = finder_mod.MomentFinder(cfg, lambda: db.connect(cfg.db_path),
                                     engine=lambda: clips["engine"], hold=hold)
    finder.run()
    assert video_moments.of_clip(conn, clips["party"]) == []
    assert clips["engine"].frames == 0


def test_deleting_the_clip_takes_its_moments(clips):
    clips["finder"].run()
    conn, party = clips["conn"], clips["party"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("DELETE FROM assets WHERE id=?", (party,))
        conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM video_moments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM video_moments_done").fetchone()[0] == 0
