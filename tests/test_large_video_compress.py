"""Compress and Replace on the largest-files worklist.

Beside Keep and Delete, a large *video* can be compressed: into a smaller copy
beside it (Compress), or into a smaller file in its place (Replace). The
original is never lost: Compress does not touch it, and Replace keeps it in
the library's bin before anything is swapped, and asks for the password as
deleting does. Without ffmpeg (or an H.264 encoder) the routes say why and do
nothing.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu.media import media, video_compress as vc
from ninaivu.storage import db, recycle

HAS_X264 = bool(media.FFMPEG) and vc.encoder() == "libx264"
needs_encoder = pytest.mark.skipif(not HAS_X264, reason="needs ffmpeg with libx264")


@pytest.fixture(autouse=True)
def _fresh_jobs():
    vc._reset_for_tests()
    yield
    vc._reset_for_tests()


def make_video(path: Path, seconds: int = 3) -> None:
    """A deliberately wasteful video: high bitrate MPEG-4 Part 2, so the
    preset always makes it smaller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [media.FFMPEG, "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "mpeg4", "-q:v", "1", "-c:a", "pcm_s16le" if path.suffix == ".avi" else "aac",
         "-metadata", "creation_time=2015-06-01T10:00:00Z", str(path)],
        check=True, capture_output=True, timeout=120)


def index_video(scanned, name: str) -> int:
    cfg, conn, scanner = scanned
    path = Path(cfg.active_root) / "videos" / name
    make_video(path)
    scanner._run(Path(cfg.active_root), full=True)
    row = conn.execute("SELECT id FROM assets WHERE rel_path=?",
                       (f"videos/{name}",)).fetchone()
    assert row, "the scan did not index the test video"
    return int(row["id"])


def run(client, body):
    response = client.post("/api/admin/large-files/compress", json=body)
    assert response.status_code == 202, response.get_json()
    job = vc.wait(response.get_json()["job"]["id"])
    assert job["state"] == "done", job
    return job["result"]


# --- who may ask, and when it cannot run ----------------------------------------

def test_only_an_admin_can_compress(app, people):
    client = login(app.test_client(), *FAMILY)
    response = client.post("/api/admin/large-files/compress", json={"id": 1})
    assert response.status_code in (401, 403)


def test_without_ffmpeg_it_says_why_and_does_nothing(as_admin, scanned, monkeypatch):
    monkeypatch.setattr(media, "FFMPEG", None)
    vc._reset_for_tests()
    _, conn, _ = scanned
    any_id = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()["id"]
    response = as_admin.post("/api/admin/large-files/compress", json={"id": any_id})
    assert response.status_code == 409
    assert response.get_json()["unavailable"] is True
    assert "ffmpeg" in response.get_json()["error"]
    listing = as_admin.get("/api/admin/large-files/compress").get_json()
    assert "ffmpeg" in listing["unavailable"]


@needs_encoder
def test_a_photograph_is_not_compressed(as_admin, scanned):
    _, conn, _ = scanned
    photo = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()["id"]
    response = as_admin.post("/api/admin/large-files/compress", json={"id": photo})
    assert response.status_code == 404


# --- Compress: a copy beside it --------------------------------------------------

@needs_encoder
def test_compress_adds_a_smaller_copy_and_leaves_the_original(as_admin, scanned):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "party.avi")
    original = db.get_asset(conn, vid)
    db.update_asset(conn, vid, visibility=2, vis_source="item")
    path = Path(cfg.active_root) / original["rel_path"]
    before = path.read_bytes()

    result = run(as_admin, {"id": vid, "mode": "copy"})

    assert path.read_bytes() == before                     # untouched
    assert result["name"] == "party-compressed.mp4"
    copy = db.get_asset(conn, result["id"])
    assert copy["kind"] == "video" and copy["ext"] == "mp4"
    assert copy["size"] < original["size"]
    assert copy["visibility"] == 2               # carried, never the default
    assert copy["date_key"] == original["date_key"]
    assert (Path(cfg.active_root) / copy["rel_path"]).is_file()
    assert not list(path.parent.glob("*.tmp"))


# --- Replace: in its place, original kept ----------------------------------------

@needs_encoder
def test_replace_asks_for_the_password(as_admin, scanned):
    vid = index_video(scanned, "trip.mp4")
    response = as_admin.post("/api/admin/large-files/compress",
                             json={"id": vid, "mode": "replace"})
    assert response.status_code == 401
    assert response.get_json()["needs_password"] is True
    response = as_admin.post("/api/admin/large-files/compress",
                             json={"id": vid, "mode": "replace", "password": "nope"})
    assert response.status_code == 401


@needs_encoder
def test_replace_keeps_the_original_in_the_bin_and_the_same_item(as_admin, scanned):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "trip.mp4")
    row = db.get_asset(conn, vid)
    path = Path(cfg.active_root) / row["rel_path"]
    before = path.read_bytes()

    result = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})

    assert result["id"] == vid and result["name"] == "trip.mp4"
    kept = recycle.bin_path(cfg.active_root) / recycle.ORIGINALS / row["rel_path"]
    assert kept.read_bytes() == before                     # the original, byte for byte
    assert path.stat().st_size < len(before)
    after = db.get_asset(conn, vid)
    assert after["size"] == path.stat().st_size


@needs_encoder
def test_replace_of_another_format_leaves_an_mp4_in_its_place(as_admin, scanned):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "wedding.avi")
    row = db.get_asset(conn, vid)
    old = Path(cfg.active_root) / row["rel_path"]
    before = old.read_bytes()

    result = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})

    assert result["name"] == "wedding.mp4"
    assert not old.exists()
    new = old.with_suffix(".mp4")
    assert new.is_file() and new.stat().st_size < len(before)
    after = db.get_asset(conn, vid)
    assert after["rel_path"] == "videos/wedding.mp4" and after["ext"] == "mp4"
    kept = recycle.bin_path(cfg.active_root) / recycle.ORIGINALS / row["rel_path"]
    assert kept.read_bytes() == before



@needs_encoder
def test_replace_uses_the_copy_compress_already_made(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "picnic.mp4")
    row = db.get_asset(conn, vid)
    path = Path(cfg.active_root) / row["rel_path"]
    before = path.read_bytes()
    copy = run(as_admin, {"id": vid, "mode": "copy"})
    copy_path = Path(cfg.active_root) / db.get_asset(conn, copy["id"])["rel_path"]
    copy_bytes = copy_path.read_bytes()

    def no_second_encode(*_a, **_k):
        raise AssertionError("Replace compressed the video again")

    monkeypatch.setattr(vc, "encode", no_second_encode)
    result = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})

    assert result["id"] == vid and result["reused"] == "picnic-compressed.mp4"
    assert path.read_bytes() == copy_bytes                 # the copy, now in place
    assert not copy_path.exists()                          # and not left twice
    assert db.get_asset(conn, copy["id"]) is None
    kept = recycle.bin_path(cfg.active_root) / recycle.ORIGINALS / row["rel_path"]
    assert kept.read_bytes() == before


@needs_encoder
def test_replace_compresses_when_the_earlier_copy_is_not_usable(as_admin, scanned):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "beach.mp4")
    copy = run(as_admin, {"id": vid, "mode": "copy"})
    copy_path = Path(cfg.active_root) / db.get_asset(conn, copy["id"])["rel_path"]
    copy_path.write_bytes(b"not a video")                  # spoilt since

    result = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})

    assert "reused" not in result
    assert copy_path.exists()                              # left alone, not used

@needs_encoder
def test_replace_leaves_the_original_when_its_safety_copy_fails(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "trip.mp4")
    path = Path(cfg.active_root) / db.get_asset(conn, vid)["rel_path"]
    before = path.read_bytes()

    def no_room(*_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(recycle, "keep_original", no_room)

    response = as_admin.post("/api/admin/large-files/compress",
                             json={"id": vid, "mode": "replace", "password": ADMIN[1]})
    job = vc.wait(response.get_json()["job"]["id"])
    assert job["state"] == "error" and "could not be kept" in job["error"]
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


@needs_encoder
def test_a_video_that_would_not_shrink_is_left_alone(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "clip.mp4")
    path = Path(cfg.active_root) / db.get_asset(conn, vid)["rel_path"]
    before = path.read_bytes()
    monkeypatch.setattr(vc, "verify", lambda *_a: (_ for _ in ()).throw(vc.CompressError(
        "Compressing would not make this video any smaller, so nothing was changed.")))

    response = as_admin.post("/api/admin/large-files/compress",
                             json={"id": vid, "mode": "replace", "password": ADMIN[1]})
    job = vc.wait(response.get_json()["job"]["id"])
    assert job["state"] == "error" and "smaller" in job["error"]
    assert path.read_bytes() == before


# --- the preset ------------------------------------------------------------------

def test_the_preset_never_scales_up_and_keeps_the_date(tmp_path, monkeypatch):
    # Only the arguments are checked, so it runs where ffmpeg is not installed.
    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")
    cmd = vc.command(tmp_path / "in.mov", tmp_path / "out.tmp", "libx264")
    joined = " ".join(cmd)
    assert "min(ih,1080)" in joined and "min(iw,1080)" in joined
    assert cmd[cmd.index("-map_metadata") + 1] == "0"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-f", cmd.index("-nostats")) + 1] == "mp4"


# --- the Mac's video engine ----------------------------------------------------

def test_the_video_engine_is_given_a_bitrate_and_reads_on_the_engine_too(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")
    cmd = vc.command(tmp_path / "in.mov", tmp_path / "out.tmp", vc.HARDWARE, None, 5_000_000)
    assert cmd[cmd.index("-c:v") + 1] == vc.HARDWARE
    assert cmd[cmd.index("-b:v") + 1] == "5000000"
    assert cmd.index("-hwaccel") < cmd.index("-i")
    assert "-crf" not in cmd
    # The software command is as it was: no hardware decode.
    assert "-hwaccel" not in vc.command(tmp_path / "in.mov", tmp_path / "out.tmp", "libx264")


def test_the_video_engines_bitrate_follows_the_picture_and_the_original():
    hour = 3600.0
    # 4K at 57 Mbit/s (a phone's): the full 1080p rate.
    assert vc.hardware_bitrate(3840, 2160, int(57e6 / 8 * hour), hour) == vc.HARDWARE_1080P_BPS
    # A portrait clip is judged by its short side, like the scale filter.
    assert vc.hardware_bitrate(2160, 3840, int(57e6 / 8 * hour), hour) == vc.HARDWARE_1080P_BPS
    # 720p gets less than 1080p.
    assert vc.hardware_bitrate(1280, 720, int(57e6 / 8 * hour), hour) < vc.HARDWARE_1080P_BPS
    # Never more than half of what a lean original spends, nor below the floor.
    assert vc.hardware_bitrate(1920, 1080, int(4e6 / 8 * hour), hour) == 2_000_000
    assert vc.hardware_bitrate(1920, 1080, int(0.5e6 / 8 * hour), hour) == vc.HARDWARE_MIN_BPS
    # Unknown size: treated as 1080p.
    assert vc.hardware_bitrate(0, 0, 0, 0) == vc.HARDWARE_1080P_BPS


def _two_encoders(monkeypatch, software="libx264", hardware=vc.HARDWARE):
    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(vc, "_encoder", software or "")
    monkeypatch.setattr(vc, "_hardware", hardware or "")
    monkeypatch.setattr(media, "probe_video", lambda _p: {"width": 3840, "height": 2160, "duration": 60})


def test_a_mac_compresses_on_the_video_engine(tmp_path, monkeypatch):
    _two_encoders(monkeypatch)
    used = []
    monkeypatch.setattr(vc, "_ffmpeg", lambda cmd, *_a: used.append(cmd[cmd.index("-c:v") + 1]))
    vc.encode(tmp_path / "in.mov", tmp_path / "out.tmp", 60, lambda _f: None, lambda: False)
    assert used == [vc.HARDWARE]


def test_a_video_the_engine_cannot_do_is_done_on_the_processor(tmp_path, monkeypatch):
    _two_encoders(monkeypatch)
    used, progress = [], []

    def run(cmd, *_a):
        used.append(cmd[cmd.index("-c:v") + 1])
        if len(used) == 1:
            raise vc.CompressError("hardware said no")

    monkeypatch.setattr(vc, "_ffmpeg", run)
    vc.encode(tmp_path / "in.mov", tmp_path / "out.tmp", 60, progress.append, lambda: False)
    assert used == [vc.HARDWARE, "libx264"]
    assert progress == [0.0]                  # the bar starts again for the second go


def test_stopping_during_the_engines_go_does_not_start_the_processor(tmp_path, monkeypatch):
    _two_encoders(monkeypatch)
    used = []

    def run(cmd, *_a):
        used.append(cmd[cmd.index("-c:v") + 1])
        raise vc.CompressError("Stopped.")

    monkeypatch.setattr(vc, "_ffmpeg", run)
    with pytest.raises(vc.CompressError):
        vc.encode(tmp_path / "in.mov", tmp_path / "out.tmp", 60, lambda _f: None, lambda: True)
    assert used == [vc.HARDWARE]


def test_elsewhere_only_the_processor_is_used(tmp_path, monkeypatch):
    _two_encoders(monkeypatch, hardware=None)
    used = []
    monkeypatch.setattr(vc, "_ffmpeg", lambda cmd, *_a: used.append(cmd[cmd.index("-c:v") + 1]))
    vc.encode(tmp_path / "in.mov", tmp_path / "out.tmp", 60, lambda _f: None, lambda: False)
    assert used == ["libx264"]


# --- one copy per video, and tidying the extras -----------------------------------

@needs_encoder
def test_compress_does_not_make_a_second_copy(as_admin, scanned):
    vid = index_video(scanned, "dance.mp4")
    run(as_admin, {"id": vid, "mode": "copy"})
    again = as_admin.post("/api/admin/large-files/compress", json={"id": vid, "mode": "copy"})
    assert again.status_code == 409
    assert "Replace" in again.get_json()["error"]


@needs_encoder
def test_a_compressed_copy_is_not_compressed_again(as_admin, scanned):
    vid = index_video(scanned, "song.mp4")
    copy = run(as_admin, {"id": vid, "mode": "copy"})
    again = as_admin.post("/api/admin/large-files/compress", json={"id": copy["id"], "mode": "copy"})
    assert again.status_code == 409


def _second_copy(scanned, first_rel: str, name: str) -> Path:
    cfg, _, scanner = scanned
    first = Path(cfg.active_root) / first_rel
    extra = first.with_name(name)
    extra.write_bytes(first.read_bytes())
    scanner._run(Path(cfg.active_root), full=True)
    return extra


@needs_encoder
def test_tidy_keeps_the_newest_copy_and_bins_the_rest(as_admin, scanned):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "garden.mp4")
    copy = run(as_admin, {"id": vid, "mode": "copy"})
    first_rel = db.get_asset(conn, copy["id"])["rel_path"]
    older = _second_copy(scanned, first_rel, "garden-compressed-2.mp4")
    import os
    os.utime(older, (1, 1))                                 # made long ago
    conn.execute("UPDATE assets SET mtime=1 WHERE rel_path=?", ("videos/garden-compressed-2.mp4",))
    conn.commit()

    plan = as_admin.get("/api/admin/large-files/extra-copies").get_json()
    assert [i["filename"] for i in plan["items"]] == ["garden-compressed-2.mp4"]
    assert plan["kept"] == 1

    refused = as_admin.post("/api/admin/large-files/extra-copies", json={"password": "nope"})
    assert refused.status_code == 401
    done = as_admin.post("/api/admin/large-files/extra-copies", json={"password": ADMIN[1]})
    assert done.get_json()["deleted"] == 1
    assert not older.exists()
    assert (Path(cfg.active_root) / first_rel).exists()     # the kept copy stays
    assert as_admin.get("/api/admin/large-files/extra-copies").get_json()["items"] == []


@needs_encoder
def test_tidy_bins_every_copy_of_a_video_already_replaced(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    vid = index_video(scanned, "river.mp4")
    copy = run(as_admin, {"id": vid, "mode": "copy"})
    copy_rel = db.get_asset(conn, copy["id"])["rel_path"]
    # Replaced the slow way (as builds before the reuse did), leaving the copy.
    from ninaivu.api import admin_api
    monkeypatch.setattr(admin_api, "_earlier_copy", lambda *_a: None)
    run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert (Path(cfg.active_root) / copy_rel).exists()

    plan = as_admin.get("/api/admin/large-files/extra-copies").get_json()
    assert [i["filename"] for i in plan["items"]] == ["river-compressed.mp4"]


@needs_encoder
def test_tidy_never_takes_the_only_copy(as_admin, scanned):
    vid = index_video(scanned, "lake.mp4")
    run(as_admin, {"id": vid, "mode": "copy"})
    plan = as_admin.get("/api/admin/large-files/extra-copies").get_json()
    assert plan["items"] == [] and plan["kept"] == 1


# --- a video damaged at its source ------------------------------------------------
#
# A clip cut short where it came from has no index ("moov atom not found"):
# nothing can open it, so it is said plainly, never tried twice, and the
# list marks it so Keep or Delete is the answer.

REAL_FFMPEG, REAL_FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (REAL_FFMPEG and REAL_FFPROBE),
                                  reason="needs ffmpeg and ffprobe")


@pytest.fixture
def real_tools(monkeypatch):
    """The installed ffmpeg and ffprobe, whatever an earlier test (the
    components installer's) left in media's module globals."""
    monkeypatch.setattr(media, "FFMPEG", REAL_FFMPEG)
    monkeypatch.setattr(media, "FFPROBE", REAL_FFPROBE)


def cut_short(path: Path) -> None:
    """A video whose index never got written, as when a camera lost power:
    an MP4 keeps it at the end unless asked otherwise, so keeping only the
    first half of the bytes leaves frames with nothing that says where."""
    make_video(path)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])


def index_damaged(scanned, name: str) -> int:
    cfg, conn, scanner = scanned
    cut_short(Path(cfg.active_root) / "videos" / name)
    scanner._run(Path(cfg.active_root), full=True)
    row = conn.execute("SELECT id FROM assets WHERE rel_path=?",
                       (f"videos/{name}",)).fetchone()
    if row is None:
        pytest.skip("this scan does not index a video it cannot open")
    return int(row["id"])


def test_ffmpegs_words_for_a_damaged_file_are_known():
    assert vc.is_damage("[in#0 @ 0x7a60c34000] moov atom not found\nError opening input: "
                        "Invalid data found when processing input")
    assert vc.is_damage("Error opening input files: Invalid data found when processing input")
    assert not vc.is_damage("Error opening input file x.mp4: Permission denied")
    assert not vc.is_damage("No space left on device")


@needs_ffmpeg
def test_a_cut_short_video_is_told_apart(tmp_path, real_tools):
    whole, short = tmp_path / "whole.mp4", tmp_path / "short.mp4"
    make_video(whole)
    cut_short(short)
    assert vc.damaged(whole) is False
    assert vc.damaged(short) is True
    assert vc.damaged(tmp_path / "missing.mp4") is False


def test_a_damaged_video_is_not_tried_again_on_the_processor(tmp_path, monkeypatch):
    _two_encoders(monkeypatch)
    used = []

    def run(cmd, *_a):
        used.append(cmd[cmd.index("-c:v") + 1])
        raise vc.DamagedVideo(vc.DAMAGED)

    monkeypatch.setattr(vc, "_ffmpeg", run)
    with pytest.raises(vc.DamagedVideo):
        vc.encode(tmp_path / "in.mov", tmp_path / "out.tmp", 60, lambda _f: None, lambda: False)
    assert used == [vc.HARDWARE]


def test_ffmpeg_failing_on_a_damaged_file_says_so_plainly(tmp_path, monkeypatch):
    """ffmpeg's own words are replaced by the sentence, so the row does not
    show "moov atom not found" and an address in memory."""
    script = tmp_path / "fake-ffmpeg"
    script.write_text("#!/bin/sh\necho '[in#0 @ 0x1] moov atom not found' >&2\n"
                      "echo 'Error opening input: Invalid data found when processing input' >&2\n"
                      "exit 1\n")
    script.chmod(0o755)
    with pytest.raises(vc.DamagedVideo) as raised:
        vc._ffmpeg([str(script)], 10, lambda _f: None, lambda: False)
    assert str(raised.value) == vc.DAMAGED


def test_a_damaged_job_is_marked(monkeypatch):
    def work(job, report, cancelled):
        raise vc.DamagedVideo(vc.DAMAGED)

    job = vc.wait(vc.start(7, "copy", "MAH09831.MP4", work)["id"])
    assert job["state"] == "error"
    assert job["damaged"] is True
    assert job["error"] == vc.DAMAGED


@needs_ffmpeg
def test_the_list_marks_a_damaged_video_and_compress_refuses_it(as_admin, scanned, real_tools):
    vid = index_damaged(scanned, "MAH09831.MP4")
    good = index_video(scanned, "fine.mp4")
    listing = as_admin.get("/api/admin/large-files?min_mb=0.001").get_json()
    marked = {item["id"]: item["damaged"] for item in listing["items"]}
    assert marked.get(vid) is True
    assert marked.get(good) is False

    response = as_admin.post("/api/admin/large-files/compress", json={"id": vid, "mode": "copy"})
    assert response.status_code == 409
    assert response.get_json()["damaged"] is True
    assert response.get_json()["error"] == vc.DAMAGED
    assert vc.active() == []                        # nothing queued to fail later
