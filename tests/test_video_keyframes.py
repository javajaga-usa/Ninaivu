"""Describing a video by several moments instead of its first second.

A holiday clip is a beach, then a restaurant, then a car park. The poster
frame describes none of them, so searching for "beach" missed the video of
the beach. These tests cover the folding — several verdicts into one — and
the one safety property that folding must not average away.
"""

import json
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.api.admin_api import DEFAULT_VIDEO_KEYFRAMES
from ninaivu.media import media
from ninaivu.media.scanner import AI_VERSION, KEYFRAME_VERSION, Scanner
from ninaivu.storage import db


class StubEngine:
    """Returns a scripted verdict per frame, in order."""

    name = "stub"
    model_id = "stub-v1"
    semantic = False

    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.seen: list[list[Path]] = []

    def analyse(self, paths, **_):
        self.seen.append(list(paths))
        return self.verdicts[:len(paths)]


def scanner_with(cfg, verdicts):
    # The shared fixture ships with the AI layer off, since most of the suite
    # has no use for it. Keyframe tagging is the layer, so it goes back on.
    cfg.ai_enabled = True
    scanner = Scanner(cfg)
    scanner.ai = StubEngine(verdicts)
    return scanner


# ---------------------------------------------------------------------------
# Folding several frames into one description
# ---------------------------------------------------------------------------

def test_tags_from_every_moment_reach_the_clip(cfg):
    merged = scanner_with(cfg, [])._merge_keyframes([
        {"tags": ["beach", "sea"]},
        {"tags": ["restaurant"]},
        {"tags": ["car park"]},
    ])
    assert set(merged["tags"]) == {"beach", "sea", "restaurant", "car park"}


def test_a_subject_that_persists_outranks_one_that_flickered(cfg):
    merged = scanner_with(cfg, [])._merge_keyframes([
        {"tags": ["dog", "lens flare"]},
        {"tags": ["dog"]},
        {"tags": ["dog"]},
    ])
    assert merged["tags"][0] == "dog"


def test_the_tag_cap_is_respected(cfg):
    cfg.max_tags = 3
    merged = scanner_with(cfg, [])._merge_keyframes(
        [{"tags": [f"t{i}" for i in range(10)]}])
    assert len(merged["tags"]) == 3


def test_the_worst_moment_governs_the_content_score(cfg):
    """A clip that is fine for thirty seconds and not for one is still that
    clip. An average would bury exactly the moment worth catching."""
    cfg.nsfw_filter = True
    cfg.nsfw_threshold = 0.6
    merged = scanner_with(cfg, [])._merge_keyframes([
        {"nsfw_score": 0.01}, {"nsfw_score": 0.95}, {"nsfw_score": 0.02},
    ])
    assert merged["nsfw_score"] == 0.95
    assert merged["nsfw"] == 1


def test_the_score_is_left_alone_when_screening_is_off(cfg):
    cfg.nsfw_filter = False
    merged = scanner_with(cfg, [])._merge_keyframes([{"nsfw_score": 0.95}])
    assert "nsfw_score" not in merged


def test_the_middle_of_the_clip_represents_it(cfg):
    """Either end of a clip is a fade or a lens cap more often than a subject."""
    merged = scanner_with(cfg, [])._merge_keyframes([
        {"caption": "black frame"},
        {"caption": "the actual subject"},
        {"caption": "credits"},
    ])
    assert merged["caption"] == "the actual subject"


def test_nothing_is_invented_from_empty_frames(cfg):
    merged = scanner_with(cfg, [])._merge_keyframes([{}, {}, {}])
    assert merged == {"ai_version": AI_VERSION}


# ---------------------------------------------------------------------------
# The pass itself
# ---------------------------------------------------------------------------

@pytest.fixture()
def video_row(scanned):
    """A video asset in the index, with no file behind it."""
    cfg, conn, _ = scanned
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, duration, "
        "keyframe_version) VALUES (?,?,?,?,?,0)",
        (cfg.active_root, "clip.mp4", "clip.mp4", "video", 60.0),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM assets WHERE rel_path='clip.mp4'").fetchone()
    return cfg, conn, int(row["id"])


def test_the_clip_is_sampled_across_its_length(video_row, monkeypatch):
    cfg, conn, asset_id = video_row
    (Path(cfg.active_root) / "clip.mp4").write_bytes(b"not really a video")

    asked: list[float] = []

    def fake_frame(path, offset=1.0):
        asked.append(offset)
        return Image.new("RGB", (320, 240), (10, 20, 30))

    monkeypatch.setattr(media, "extract_video_frame", fake_frame)
    scanner = scanner_with(cfg, [{"tags": ["a"]}] * 5)
    scanner._tag_video_keyframes(conn, cfg.active_root)

    assert asked == [6.0, 18.0, 30.0, 42.0, 54.0], "10%..90% of sixty seconds"


def test_a_tagged_clip_is_not_sampled_twice(video_row, monkeypatch):
    cfg, conn, asset_id = video_row
    (Path(cfg.active_root) / "clip.mp4").write_bytes(b"not really a video")
    monkeypatch.setattr(
        media, "extract_video_frame",
        lambda path, offset=1.0: Image.new("RGB", (320, 240)))

    scanner = scanner_with(cfg, [{"tags": ["a"]}] * 5)
    scanner._tag_video_keyframes(conn, cfg.active_root)
    stamped = conn.execute("SELECT keyframe_version FROM assets WHERE id=?",
                           (asset_id,)).fetchone()["keyframe_version"]
    assert stamped == KEYFRAME_VERSION

    scanner.ai.seen.clear()
    scanner._tag_video_keyframes(conn, cfg.active_root)
    assert scanner.ai.seen == [], "the version gate stops a second pass"


def test_a_clip_whose_frames_cannot_be_read_is_marked_done(video_row, monkeypatch):
    """Otherwise every scan retries the same unreadable file forever."""
    cfg, conn, asset_id = video_row
    (Path(cfg.active_root) / "clip.mp4").write_bytes(b"broken")
    monkeypatch.setattr(media, "extract_video_frame",
                        lambda path, offset=1.0: None)

    scanner_with(cfg, [])._tag_video_keyframes(conn, cfg.active_root)
    stamped = conn.execute("SELECT keyframe_version FROM assets WHERE id=?",
                           (asset_id,)).fetchone()["keyframe_version"]
    assert stamped == KEYFRAME_VERSION


def test_a_missing_file_is_marked_done_rather_than_retried(video_row):
    cfg, conn, asset_id = video_row
    scanner_with(cfg, [])._tag_video_keyframes(conn, cfg.active_root)
    stamped = conn.execute("SELECT keyframe_version FROM assets WHERE id=?",
                           (asset_id,)).fetchone()["keyframe_version"]
    assert stamped == KEYFRAME_VERSION


def test_single_frame_sampling_keeps_the_old_behaviour(video_row, monkeypatch):
    """`video_keyframes` of 1 or 0 means the poster frame alone, as before."""
    cfg, conn, asset_id = video_row
    cfg.video_keyframes = 1
    calls: list[float] = []
    monkeypatch.setattr(media, "extract_video_frame",
                        lambda path, offset=1.0: calls.append(offset))

    scanner_with(cfg, [])._tag_video_keyframes(conn, cfg.active_root)
    assert calls == []


def test_the_tags_land_on_the_asset(video_row, monkeypatch):
    cfg, conn, asset_id = video_row
    (Path(cfg.active_root) / "clip.mp4").write_bytes(b"not really a video")
    monkeypatch.setattr(
        media, "extract_video_frame",
        lambda path, offset=1.0: Image.new("RGB", (320, 240)))

    scanner_with(cfg, [
        {"tags": ["beach"]}, {"tags": ["restaurant"]}, {"tags": ["beach"]},
        {"tags": ["car park"]}, {"tags": ["beach"]},
    ])._tag_video_keyframes(conn, cfg.active_root)

    stored = db.get_asset(conn, asset_id)
    assert stored["tags"][0] == "beach"
    assert "restaurant" in stored["tags"]


# ---------------------------------------------------------------------------
# A clip the decoders cannot read
# ---------------------------------------------------------------------------

def test_an_unreadable_clip_is_only_attempted_once(video_row, monkeypatch):
    """"moov atom not found" means the container has no index.

    Every offset will fail, so trying the other four costs four more decoder
    start-ups and four more complaints on the console for a file that was
    never going to answer.
    """
    cfg, conn, asset_id = video_row
    (Path(cfg.active_root) / "clip.mp4").write_bytes(b"truncated download")

    attempts: list[float] = []

    def refuses(path, offset=1.0):
        attempts.append(offset)
        return None

    monkeypatch.setattr(media, "extract_video_frame", refuses)
    scanner_with(cfg, [])._tag_video_keyframes(conn, cfg.active_root)

    assert len(attempts) == 1, f"gave up after {len(attempts)} attempts"


def test_a_clip_that_reads_partway_still_uses_what_it_got(video_row, monkeypatch):
    """One bad moment is not a bad file — a seek past the end fails alone."""
    cfg, conn, asset_id = video_row
    (Path(cfg.active_root) / "clip.mp4").write_bytes(b"not really a video")

    calls = {"n": 0}

    def flaky(path, offset=1.0):
        calls["n"] += 1
        if calls["n"] == 2:
            return None
        return Image.new("RGB", (320, 240))

    monkeypatch.setattr(media, "extract_video_frame", flaky)
    scanner = scanner_with(cfg, [{"tags": ["beach"]}] * 5)
    scanner._tag_video_keyframes(conn, cfg.active_root)

    assert calls["n"] == 5, "the remaining moments are still sampled"
    assert scanner.ai.seen and len(scanner.ai.seen[0]) == 4


def test_the_ffmpeg_backend_is_quietened_before_opencv_loads():
    """OpenCV's FFmpeg writes to stderr past Python; only the env var stops it."""
    import os

    import ninaivu.media  # noqa: F401 - imported for the side effect being tested

    assert os.environ.get("OPENCV_FFMPEG_LOGLEVEL") == "-8"


# ---------------------------------------------------------------------------
# Turning the pass off from the console
# ---------------------------------------------------------------------------

def test_the_switch_turns_the_pass_off(cfg, as_admin):
    response = as_admin.post("/api/admin/settings",
                             json={"video_keyframes": False})
    assert response.status_code == 200
    assert response.get_json()["settings"]["video_keyframes"] == 0
    assert cfg.video_keyframes == 0


def test_the_switch_turns_the_pass_back_on(cfg, as_admin):
    cfg.video_keyframes = 0
    as_admin.post("/api/admin/settings", json={"video_keyframes": True})
    assert cfg.video_keyframes == DEFAULT_VIDEO_KEYFRAMES


def test_the_choice_survives_an_unrelated_settings_change(cfg, as_admin):
    """The regression this was written for.

    ``Config.save()`` writes a named subset of the settings, and a key left
    out of it is dropped the next time anything at all is saved. A household
    that had turned the keyframe pass off got it back — and another fifteen
    hours of it — the first time somebody touched any other switch.
    """
    as_admin.post("/api/admin/settings", json={"video_keyframes": False})
    as_admin.post("/api/admin/settings", json={"watch": False})

    stored = json.loads((cfg.state_dir / "config.json").read_text("utf-8"))
    assert stored["video_keyframes"] == 0


def test_a_number_of_moments_can_be_asked_for(cfg, as_admin):
    """A switch in the console, but the setting itself is how many moments."""
    as_admin.post("/api/admin/settings", json={"video_keyframes": 3})
    assert cfg.video_keyframes == 3


def test_more_moments_than_there_are_points_is_clamped(cfg, as_admin):
    as_admin.post("/api/admin/settings", json={"video_keyframes": 99})
    assert cfg.video_keyframes == len(Scanner.KEYFRAME_POINTS)


def test_nonsense_is_refused_and_takes_nothing_with_it(cfg, as_admin):
    """A bad number leaves the whole request alone, switches included."""
    before = (cfg.video_keyframes, cfg.watch)
    response = as_admin.post(
        "/api/admin/settings", json={"video_keyframes": "lots", "watch": False})
    assert response.status_code == 400
    assert (cfg.video_keyframes, cfg.watch) == before


def test_only_an_admin_can_turn_it_off(app, people):
    from conftest import FAMILY, GUEST, login

    for who in (FAMILY, GUEST):
        client = login(app.test_client(), *who)
        response = client.post("/api/admin/settings",
                               json={"video_keyframes": False})
        assert response.status_code in (401, 403, 404)


def test_turning_it_off_stops_a_pass_already_running(video_row, monkeypatch):
    """Otherwise the switch means "next time", and next time is fifteen hours
    away — the only way out would be stopping the whole scan, losing the
    passes that come after this one."""
    cfg, conn, _ = video_row
    for name in ("b.mp4", "c.mp4"):
        conn.execute(
            "INSERT INTO assets(root, rel_path, filename, kind, duration, "
            "keyframe_version) VALUES (?,?,?,?,?,0)",
            (cfg.active_root, name, name, "video", 60.0),
        )
    conn.commit()
    for name in ("clip.mp4", "b.mp4", "c.mp4"):
        (Path(cfg.active_root) / name).write_bytes(b"not really a video")

    monkeypatch.setattr(
        media, "extract_video_frame",
        lambda path, offset=1.0: Image.new("RGB", (320, 240)))

    scanner = scanner_with(cfg, [{"tags": ["a"]}] * 5)
    described = []

    original = scanner._tag_one_video

    def watched(conn_, root, row, points):
        described.append(row["rel_path"])
        cfg.video_keyframes = 0          # somebody hits the switch
        return original(conn_, root, row, points)

    scanner._tag_one_video = watched
    scanner._tag_video_keyframes(conn, cfg.active_root)

    assert len(described) == 1, f"kept going through {described}"


# ---------------------------------------------------------------------------
# A clip with no duration: written off, and then rescued
# ---------------------------------------------------------------------------
#
# Duration is read when a file is *indexed*, and a file whose size and mtime
# have not moved is never indexed again. So a video indexed on a machine with
# no ffmpeg has no duration for the rest of its life — and this pass used to
# read that as "nothing to sample" and stamp it done for ever. Installing
# ffmpeg afterwards rescued nothing. On the library this was found on, 83
# videos were in that state and 3 had already been written off.

@pytest.fixture()
def undated_video(scanned):
    """A video in the index with no duration, as an ffmpeg-less scan leaves."""
    cfg, conn, _ = scanned
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, duration, "
        "keyframe_version) VALUES (?,?,?,?,NULL,0)",
        (cfg.active_root, "nolength.mp4", "nolength.mp4", "video"),
    )
    conn.commit()
    (Path(cfg.active_root) / "nolength.mp4").write_bytes(b"not really a video")
    row = conn.execute(
        "SELECT id FROM assets WHERE rel_path='nolength.mp4'").fetchone()
    return cfg, conn, int(row["id"])


def test_a_clip_with_no_duration_is_asked_again(undated_video, monkeypatch):
    cfg, conn, asset_id = undated_video
    monkeypatch.setattr(media, "probe_video",
                        lambda path: {"duration": 60.0, "width": 1920,
                                      "height": 1080})
    monkeypatch.setattr(
        media, "extract_video_frame",
        lambda path, offset=1.0: Image.new("RGB", (320, 240)))

    scanner = scanner_with(cfg, [{"tags": ["beach"]}] * 5)
    scanner._tag_video_keyframes(conn, cfg.active_root)

    assert scanner.ai.seen, "never described, so the probe was not retried"
    stored = db.get_asset(conn, asset_id)
    assert stored["tags"][0] == "beach"


def test_the_length_it_finds_is_kept(undated_video, monkeypatch):
    """So no later pass has to ask the same question again."""
    cfg, conn, asset_id = undated_video
    monkeypatch.setattr(media, "probe_video", lambda path: {"duration": 42.5})
    monkeypatch.setattr(
        media, "extract_video_frame",
        lambda path, offset=1.0: Image.new("RGB", (320, 240)))

    scanner_with(cfg, [{"tags": ["a"]}] * 5)._tag_video_keyframes(
        conn, cfg.active_root)

    assert db.get_asset(conn, asset_id)["duration"] == 42.5


def test_a_clip_that_still_has_no_duration_is_marked_done(undated_video,
                                                          monkeypatch):
    """Genuinely unreadable — a truncated download. Asked once, not for ever."""
    cfg, conn, asset_id = undated_video
    asked = []
    monkeypatch.setattr(media, "probe_video",
                        lambda path: asked.append(path) or {})

    scanner_with(cfg, [])._tag_video_keyframes(conn, cfg.active_root)

    assert len(asked) == 1
    stamped = conn.execute("SELECT keyframe_version FROM assets WHERE id=?",
                           (asset_id,)).fetchone()["keyframe_version"]
    assert stamped == KEYFRAME_VERSION


def test_clips_written_off_before_are_given_another_go(scanned, monkeypatch):
    """The 83 already on disk. They were stamped for a reason that has since
    stopped being true, and nothing else would ever go back for them."""
    cfg, conn, _ = scanned
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, duration, "
        "keyframe_version) VALUES (?,?,?,?,NULL,?)",
        (cfg.active_root, "writtenoff.mp4", "writtenoff.mp4", "video",
         KEYFRAME_VERSION),
    )
    conn.commit()
    (Path(cfg.active_root) / "writtenoff.mp4").write_bytes(b"not a video")

    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(media, "probe_video", lambda path: {"duration": 30.0})
    monkeypatch.setattr(
        media, "extract_video_frame",
        lambda path, offset=1.0: Image.new("RGB", (320, 240)))

    scanner = scanner_with(cfg, [{"tags": ["rescued"]}] * 5)
    scanner._tag_video_keyframes(conn, cfg.active_root)

    row = conn.execute("SELECT id FROM assets WHERE rel_path='writtenoff.mp4'"
                       ).fetchone()
    assert db.get_asset(conn, int(row["id"]))["tags"] == ["rescued"]


def test_a_described_clip_is_not_dragged_back_in(scanned, monkeypatch):
    """Only the ones with no duration. A clip that was properly described must
    not be re-described on every scan for ever."""
    cfg, conn, _ = scanned
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, duration, "
        "keyframe_version) VALUES (?,?,?,?,60.0,?)",
        (cfg.active_root, "fine.mp4", "fine.mp4", "video", KEYFRAME_VERSION),
    )
    conn.commit()
    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")

    scanner = scanner_with(cfg, [])
    scanner._tag_video_keyframes(conn, cfg.active_root)
    assert scanner.ai.seen == []


def test_an_unreadable_clip_is_not_probed_again_every_scan(undated_video,
                                                           monkeypatch):
    """Lifted once per decoder, not once per scan.

    Lifting the stamp on every scan re-probed the same 46 broken clips every
    ninety seconds, and those reads set off the watcher, which started the
    next scan.
    """
    cfg, conn, _ = undated_video
    asked = []
    monkeypatch.setattr(media, "probe_video",
                        lambda path: asked.append(path) or {})
    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")

    scanner = scanner_with(cfg, [])
    for _ in range(3):
        scanner._tag_video_keyframes(conn, cfg.active_root)

    assert len(asked) == 1, asked
