"""Large files audit, 2026-10-10: Compress, Replace and Tidy copies.

Each test is one finding of that audit, turned round: what the probe showed
going wrong, shown going right.
"""

import errno
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import ADMIN
from ninaivu.media import media, video_compress as vc
from ninaivu.storage import db, recycle

HAS_X264 = bool(media.FFMPEG) and vc.encoder() == "libx264"
needs_encoder = pytest.mark.skipif(not HAS_X264, reason="needs ffmpeg with libx264")


@pytest.fixture(autouse=True)
def _fresh_jobs():
    vc._reset_for_tests()
    yield
    vc._reset_for_tests()


def make_video(path: Path, seconds: int = 3, src: str = "testsrc2") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([media.FFMPEG, "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"{src}=size=1280x720:rate=25:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "mpeg4", "-q:v", "1", "-c:a", "aac", str(path)],
                   check=True, capture_output=True, timeout=120)


def scan(scanned) -> None:
    cfg, _conn, scanner = scanned
    scanner._run(Path(cfg.active_root), full=True)


def idof(conn, rel: str) -> int | None:
    row = conn.execute("SELECT id FROM assets WHERE rel_path=?", (rel,)).fetchone()
    return row and int(row["id"])


def start(client, body):
    response = client.post("/api/admin/large-files/compress", json=body)
    assert response.status_code == 202, response.get_json()
    return response.get_json()["job"]["id"]


def run(client, body):
    return vc.wait(start(client, body))


# --- F1: a copy belongs to its own video, not to one with the same stem ---------

@needs_encoder
def test_replace_never_uses_the_copy_of_another_video_with_the_same_name(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "clip.mov", src="testsrc2")
    make_video(root / "v" / "clip.mp4", src="smptebars")       # another clip, same length
    scan(scanned)
    mov, mp4 = idof(conn, "v/clip.mov"), idof(conn, "v/clip.mp4")
    job = run(as_admin, {"id": mp4, "mode": "copy"})
    assert job["state"] == "done", job
    other_copy = (root / "v/clip-compressed.mp4").read_bytes()
    assert vc.copy_source(root / "v/clip-compressed.mp4") == "clip.mp4"

    # clip.mov has no copy of its own yet, so Compress is not refused.
    job = run(as_admin, {"id": mov, "mode": "copy"})
    assert job["state"] == "done", job
    assert job["result"]["name"] == "clip-compressed-2.mp4"

    job = run(as_admin, {"id": mov, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "done", job
    assert job["result"].get("reused") == "clip-compressed-2.mp4"
    placed = root / db.get_asset(conn, mov)["rel_path"]
    assert placed.read_bytes() != other_copy
    assert vc.copy_source(placed) == "clip.mov"
    assert (root / "v/clip-compressed.mp4").read_bytes() == other_copy   # left alone


@needs_encoder
def test_replace_does_not_reuse_a_copy_with_no_sign_of_where_it_came_from(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "clip.mov")
    scan(scanned)
    mov = idof(conn, "v/clip.mov")
    # A smaller video that only has the name (made outside Ninaivu).
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                    "-i", "smptebars=size=320x240:rate=25:duration=3",
                    "-c:v", "libx264", str(root / "v/clip-compressed.mp4")],
                   check=True, capture_output=True, timeout=120)
    scan(scanned)
    job = run(as_admin, {"id": mov, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "done", job
    assert "reused" not in job["result"]
    assert (root / "v/clip-compressed.mp4").exists()


def test_the_copy_source_falls_back_to_the_audit_and_the_caption(scanned, tmp_path):
    _cfg, conn, _ = scanned
    nothing = tmp_path / "not-a-video-compressed.mp4"
    nothing.write_bytes(b"x")
    from ninaivu.api import admin_api
    from ninaivu.server import auth
    assert admin_api._copy_source(conn, {"rel_path": "a/x-compressed.mp4"}, nothing) is None
    assert admin_api._copy_source(
        conn, {"rel_path": "a/x-compressed.mp4", "caption": "Compressed copy of x.avi"},
        nothing) == "x.avi"
    auth.audit(conn, None, "compress_video", "a/x.mkv → a/x-compressed.mp4 (9 MB → 3 MB)")
    assert admin_api._copy_source(
        conn, {"rel_path": "a/x-compressed.mp4", "caption": "beach, sea"}, nothing) == "x.mkv"


@needs_encoder
def test_a_video_with_no_recorded_length_is_measured_before_the_check(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "long.mp4", seconds=4)
    scan(scanned)
    vid = idof(conn, "v/long.mp4")
    conn.execute("UPDATE assets SET duration=NULL WHERE id=?", (vid,))
    conn.commit()
    seen = []
    real = vc.verify

    def watch(duration, size, target):
        seen.append(duration)
        return real(duration, size, target)

    monkeypatch.setattr(vc, "verify", watch)
    job = run(as_admin, {"id": vid, "mode": "copy"})
    assert job["state"] == "done", job
    assert seen and seen[0] == pytest.approx(4, abs=0.5)


# --- F2: Replace's original is an ordinary entry in Recently deleted --------------

@needs_encoder
def test_replaces_original_is_listed_restorable_and_swept(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "long.mp4", seconds=4)
    scan(scanned)
    vid = idof(conn, "v/long.mp4")
    before = (root / "v/long.mp4").read_bytes()
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "done", job

    listed = recycle.listing(conn)
    assert len(listed) == 1
    entry = listed[0]
    kept = root / "_deleted/_originals/v/long.mp4"
    assert entry["bin_path"] == str(kept) and entry["present"]
    assert entry["asset_id"] is None and entry["thumb"] is None
    assert entry["size"] == len(before) and entry["filename"] == "long.mp4"
    assert recycle.count(conn)["bytes"] == len(before)

    # Put back: beside the smaller one, which keeps its place.
    result = recycle.restore(conn, [entry["id"]])
    assert result["restored"] == 1, result
    assert (root / "v/long_1.mp4").read_bytes() == before
    assert db.get_asset(conn, vid)["rel_path"] == "v/long.mp4"
    restored = conn.execute("SELECT * FROM assets WHERE rel_path='v/long_1.mp4'").fetchone()
    assert restored is not None and restored["id"] != vid


@needs_encoder
def test_the_bins_retention_erases_replaces_original_and_nothing_else(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "trip.mp4")
    scan(scanned)
    vid = idof(conn, "v/trip.mp4")
    item_thumb = db.get_asset(conn, vid)["thumb"]
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "done", job
    kept = root / "_deleted/_originals/v/trip.mp4"
    assert kept.exists()
    assert recycle.sweep(conn, 30)["purged"] == 0          # not old enough yet
    conn.execute("UPDATE recycled SET deleted_at=?", (time.time() - 31 * 86400,))
    conn.commit()
    swept = recycle.sweep(conn, 30)
    assert swept["purged"] == 1
    assert not kept.exists()
    assert item_thumb not in swept["thumbs"]                 # the item's own stay
    assert swept["asset_ids"] == []                          # nor its face crops
    assert (root / "v/trip.mp4").exists() and db.get_asset(conn, vid) is not None
    assert recycle.listing(conn) == []


# --- F3: a Delete during Replace --------------------------------------------------

@needs_encoder
def test_a_delete_during_the_safety_copy_stops_the_replace(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "secret.mp4")
    scan(scanned)
    vid = idof(conn, "v/secret.mp4")
    db.update_asset(conn, vid, visibility=2, vis_source="item")
    real_keep = recycle.keep_original

    def keep_then_deleted(r, rel, **kw):
        out = real_keep(r, rel, **kw)
        # Meanwhile, another request deletes the same video.
        recycle.recycle(db.connect(cfg.db_path), [vid], user_id=1, roots=[str(root)])
        return out

    monkeypatch.setattr(recycle, "keep_original", keep_then_deleted)
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "error", job
    assert "no longer where it was" in job["error"]
    assert not (root / "v/secret.mp4").exists()
    assert not (root / "_deleted/_originals/v/secret.mp4").exists()   # its copy taken back
    assert not list((root / "v").glob(".*.compress.tmp"))
    scan(scanned)
    assert idof(conn, "v/secret.mp4") is None


@needs_encoder
def test_an_item_trashed_during_the_safety_copy_is_not_replaced(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "trip.mp4")
    scan(scanned)
    vid = idof(conn, "v/trip.mp4")
    before = (root / "v/trip.mp4").read_bytes()
    real_keep = recycle.keep_original

    def keep_then_trashed(r, rel, **kw):
        out = real_keep(r, rel, **kw)
        other = db.connect(cfg.db_path)
        other.execute("UPDATE assets SET trashed=1 WHERE id=?", (vid,))
        other.commit()
        return out

    monkeypatch.setattr(recycle, "keep_original", keep_then_trashed)
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "error" and "deleted or moved" in job["error"], job
    assert (root / "v/trip.mp4").read_bytes() == before
    assert not (root / "_deleted/_originals/v/trip.mp4").exists()
    assert recycle.listing(conn) == []


def test_delete_refuses_a_video_that_is_being_compressed(as_admin, scanned):
    _cfg, conn, _ = scanned
    vid = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()["id"]
    release = threading.Event()

    def work(job, report, cancelled):
        release.wait(30)
        return {}

    job = vc.start(vid, "replace", "x.mp4", work)
    try:
        response = as_admin.post("/api/delete", json={"ids": [vid], "password": ADMIN[1]})
        assert response.status_code == 409, response.get_json()
        assert response.get_json()["busy"] == [vid]
        assert db.get_asset(conn, vid) is not None
    finally:
        release.set()
        vc.wait(job["id"])
    response = as_admin.post("/api/delete", json={"ids": [vid], "password": ADMIN[1]})
    assert response.status_code == 200 and response.get_json()["deleted"] == 1


# --- F4: the safety copy on a nearly full disk -------------------------------------

def test_a_safety_copy_that_fills_the_disk_leaves_no_part_file(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    (root / "v").mkdir(parents=True)
    (root / "v" / "big.mp4").write_bytes(os.urandom(2_000_000))

    def copy2_fills_disk(a, b, *k, **kw):
        with open(a, "rb") as i, open(b, "wb") as o:
            o.write(i.read(1_000_000))
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(recycle.shutil, "copy2", copy2_fills_disk)
    with pytest.raises(OSError):
        recycle.keep_original(root, "v/big.mp4")
    assert [p for p in (root / "_deleted").rglob("*") if p.is_file()] == []


def test_a_safety_copy_is_refused_up_front_without_room_for_all_of_it(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    (root / "v").mkdir(parents=True)
    (root / "v" / "big.mp4").write_bytes(b"x" * 1000)
    real = recycle.shutil.disk_usage
    monkeypatch.setattr(recycle.shutil, "disk_usage",
                        lambda p: real(p)._replace(free=recycle.COPY_MARGIN + 500))
    copied = []
    monkeypatch.setattr(recycle.shutil, "copy2", lambda *a, **k: copied.append(a))
    with pytest.raises(OSError) as raised:
        recycle.keep_original(root, "v/big.mp4")
    assert raised.value.errno == errno.ENOSPC
    assert copied == []


def test_replaces_safety_copy_is_a_link_when_the_disk_allows(tmp_path):
    root = tmp_path / "lib"
    (root / "v").mkdir(parents=True)
    source = root / "v" / "big.mp4"
    source.write_bytes(b"original bytes")
    kept = recycle.keep_original(root, "v/big.mp4", link=True)
    assert kept is not None and os.path.samefile(kept, source)
    # The swap replaces the name; the kept link still holds the original.
    fresh = root / "v" / ".new.tmp"
    fresh.write_bytes(b"smaller")
    os.replace(fresh, source)
    assert kept.read_bytes() == b"original bytes"


def test_replaces_safety_copy_is_copied_when_links_fail(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    (root / "v").mkdir(parents=True)
    source = root / "v" / "big.mp4"
    source.write_bytes(b"original bytes")

    def no_links(*_a, **_k):
        raise OSError(errno.EPERM, "no hard links on exFAT")

    monkeypatch.setattr(recycle.os, "link", no_links)
    kept = recycle.keep_original(root, "v/big.mp4", link=True)
    assert kept.read_bytes() == b"original bytes" and not os.path.samefile(kept, source)


def test_a_rewrite_in_place_never_gets_a_linked_copy(tmp_path):
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    (root / "a" / "p.jpg").write_bytes(b"camera bytes")
    kept = recycle.keep_original(root, "a/p.jpg")
    assert not os.path.samefile(kept, root / "a" / "p.jpg")


@needs_encoder
def test_replace_without_links_needs_room_for_the_whole_original(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "trip.mp4")
    scan(scanned)
    vid = idof(conn, "v/trip.mp4")
    size = (root / "v/trip.mp4").stat().st_size
    asked = []
    monkeypatch.setattr(vc, "links_work", lambda _f: False)
    monkeypatch.setattr(vc, "same_disk_space", lambda _f, needed: asked.append(needed) or False)
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "error" and "free space" in job["error"]
    assert asked and asked[0] >= size + size // 2
    assert (root / "v/trip.mp4").stat().st_size == size


# --- F5: unfinished temporary files --------------------------------------------------

def test_stale_temporaries_are_swept_and_nothing_else(tmp_path):
    folder = tmp_path / "v"
    folder.mkdir()
    old = time.time() - vc.STALE_TEMP_SECONDS - 60
    orphan = vc.temporary_for(folder, "trip", "deadbeefcafe0000")
    orphan.write_bytes(b"x" * 1000)
    os.utime(orphan, (old, old))
    fresh = vc.temporary_for(folder, "other", "0123456789ab0000")
    fresh.write_bytes(b"x")                                   # still being written
    users = [folder / "trip.compress.tmp", folder / ".notes.compress.tmp",
             folder / ".trip.NOTHEXNOTHEX.compress.tmp", folder / "trip.mp4"]
    for path in users:
        path.write_bytes(b"mine")
        os.utime(path, (old, old))
    removed = vc.sweep_temporaries(folder)
    assert removed == [orphan.name]
    assert not orphan.exists() and fresh.exists()
    assert all(path.exists() for path in users)


def test_a_running_jobs_temporary_is_never_swept(tmp_path):
    folder = tmp_path / "v"
    folder.mkdir()
    release = threading.Event()
    job = vc.start(1, "copy", "x.mp4", lambda *_a: release.wait(30) and {})
    try:
        mine = vc.temporary_for(folder, "x", job["id"])
        mine.write_bytes(b"x")
        old = time.time() - vc.STALE_TEMP_SECONDS - 60
        os.utime(mine, (old, old))
        assert vc.sweep_temporaries(folder) == []
        assert mine.exists()
    finally:
        release.set()
        vc.wait(job["id"])


@needs_encoder
def test_a_job_sweeps_what_a_killed_job_left(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "trip.mp4")
    scan(scanned)
    vid = idof(conn, "v/trip.mp4")
    orphan = vc.temporary_for(root / "v", "trip", "deadbeefcafe0000")
    orphan.write_bytes(b"x" * 1_000_000)
    old = time.time() - vc.STALE_TEMP_SECONDS - 60
    os.utime(orphan, (old, old))
    job = run(as_admin, {"id": vid, "mode": "copy"})
    assert job["state"] == "done", job
    assert not orphan.exists()


# --- F6: Stop and a silent ffmpeg -------------------------------------------------------

def _silent_ffmpeg(tmp_path: Path, ignore_term: bool = False) -> Path:
    script = tmp_path / "hung-ffmpeg.py"
    lines = ["import signal, time"]
    if ignore_term:
        lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    lines.append("time.sleep(60)")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return script


def _run_in_thread(cmd, cancelled):
    out = {}

    def go():
        started = time.monotonic()
        try:
            vc._ffmpeg(cmd, 10, lambda _f: None, cancelled)
        except vc.CompressError as exc:
            out["error"] = str(exc)
        out["took"] = time.monotonic() - started

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, out


def test_stop_ends_an_ffmpeg_that_writes_nothing(tmp_path):
    flag = {"stop": False}
    thread, out = _run_in_thread([sys.executable, str(_silent_ffmpeg(tmp_path))],
                                 lambda: flag["stop"])
    time.sleep(1)
    flag["stop"] = True
    thread.join(timeout=vc.STOP_GRACE + 5)
    assert not thread.is_alive()
    assert out["error"] == "Stopped."


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is a kill on Windows anyway")
def test_stop_kills_an_ffmpeg_that_ignores_being_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "STOP_GRACE", 0.5)
    flag = {"stop": False}
    thread, out = _run_in_thread(
        [sys.executable, str(_silent_ffmpeg(tmp_path, ignore_term=True))], lambda: flag["stop"])
    time.sleep(1)
    flag["stop"] = True
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert out["error"] == "Stopped."


def test_an_ffmpeg_that_makes_no_progress_is_ended_and_said_so(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "STALL_SECONDS", 1)
    monkeypatch.setattr(vc, "STOP_GRACE", 0.5)
    thread, out = _run_in_thread([sys.executable, str(_silent_ffmpeg(tmp_path))], lambda: False)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "made no progress" in out["error"]


def test_an_ffmpeg_that_keeps_moving_is_not_taken_for_stuck(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "STALL_SECONDS", 1)
    script = tmp_path / "slow-ffmpeg.py"
    script.write_text("import sys, time\n"
                      "for i in range(8):\n"
                      "    print(f'out_time_us={i * 1000000}', flush=True)\n"
                      "    time.sleep(0.4)\n", encoding="utf-8")
    progress = []
    vc._ffmpeg([sys.executable, str(script)], 10, progress.append, lambda: False)
    assert progress and progress[-1] == pytest.approx(0.7)


# --- F7: Stop during "Checking…" --------------------------------------------------------

@needs_encoder
def test_stop_during_the_check_stops_a_replace_before_the_swap(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "trip.mp4")
    scan(scanned)
    vid = idof(conn, "v/trip.mp4")
    before = (root / "v/trip.mp4").read_bytes()
    real_keep = recycle.keep_original

    def keep_then_stop(r, rel, **kw):
        out = real_keep(r, rel, **kw)
        for job in vc.active():
            vc.cancel(job["id"])                  # the Stop button, while "Checking…"
        return out

    monkeypatch.setattr(recycle, "keep_original", keep_then_stop)
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "cancelled", job
    assert (root / "v/trip.mp4").read_bytes() == before
    assert not (root / "_deleted/_originals/v/trip.mp4").exists()
    assert recycle.listing(conn) == []


# --- F8: Tidy copies only tidies Ninaivu's own copies, per library ----------------------

@needs_encoder
def test_tidy_leaves_a_users_own_file_named_like_a_copy(as_admin, scanned):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "holiday.mp4")
    scan(scanned)
    vid = idof(conn, "v/holiday.mp4")
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "done", job
    make_video(root / "v" / "holiday-compressed.mp4", src="smptebars", seconds=5)
    scan(scanned)
    plan = as_admin.get("/api/admin/large-files/extra-copies").get_json()
    assert plan["items"] == []


@needs_encoder
def test_tidy_groups_copies_by_library(as_admin, scanned, tmp_path):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    other = tmp_path / "other-library"
    make_video(root / "v" / "holiday.mp4")
    scan(scanned)
    vid = idof(conn, "v/holiday.mp4")
    assert run(as_admin, {"id": vid, "mode": "copy"})["state"] == "done"
    from ninaivu.api import admin_api
    real_earlier = admin_api._earlier_copy
    admin_api._earlier_copy = lambda *_a: None              # replaced the slow way
    try:
        assert run(as_admin, {"id": vid, "mode": "replace",
                              "password": ADMIN[1]})["state"] == "done"
    finally:
        admin_api._earlier_copy = real_earlier
    # Another library with its own holiday.mp4 (never replaced) and a copy.
    make_video(other / "v" / "holiday.mp4")
    (other / "v" / "holiday-compressed.mp4").write_bytes(b"a copy made by an older build")
    cfg.add_library(str(other))
    for name, caption in (("holiday.mp4", None),
                          ("holiday-compressed.mp4", "Compressed copy of holiday.mp4")):
        path = other / "v" / name
        db.upsert_asset(conn, {"root": str(other.resolve()), "rel_path": f"v/{name}",
                               "filename": name, "folder": "v", "ext": "mp4",
                               "kind": "video", "size": path.stat().st_size,
                               "mtime": path.stat().st_mtime, "caption": caption,
                               "visibility": 1})
    plan = as_admin.get("/api/admin/large-files/extra-copies").get_json()
    assert [(i["filename"], i["folder"]) for i in plan["items"]] == [("holiday-compressed.mp4", "v")]
    ids = {i["id"] for i in plan["items"]}
    roots = {r["root"] for r in conn.execute(
        f"SELECT root FROM assets WHERE id IN ({','.join('?' * len(ids))})", list(ids))}
    assert roots == {cfg.active_root}


# --- F12: an original another program holds open (Windows) ----------------------------

@needs_encoder
def test_a_video_held_open_is_left_as_it_was_with_no_copy_behind(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "trip.mp4")
    scan(scanned)
    vid = idof(conn, "v/trip.mp4")
    source = root / "v/trip.mp4"
    before = source.read_bytes()
    real_replace = os.replace

    def sharing_violation(a, b, *k, **kw):
        if Path(b) == source:
            raise PermissionError(32, "The process cannot access the file because it is "
                                      "being used by another process")
        return real_replace(a, b, *k, **kw)

    monkeypatch.setattr(os, "replace", sharing_violation)
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    assert job["state"] == "error" and "open in another program" in job["error"], job
    assert source.read_bytes() == before
    assert not (root / "_deleted/_originals/v/trip.mp4").exists()
    assert recycle.listing(conn) == []
    assert not list((root / "v").glob(".*.compress.tmp"))


@needs_encoder
def test_an_original_of_another_format_held_open_is_not_left_twice(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    make_video(root / "v" / "wedding.mov")
    scan(scanned)
    vid = idof(conn, "v/wedding.mov")
    source = root / "v/wedding.mov"
    before = source.read_bytes()
    real_unlink = Path.unlink

    def held(self, *a, **kw):
        if self == source:
            raise PermissionError(32, "being used by another process")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", held)
    job = run(as_admin, {"id": vid, "mode": "replace", "password": ADMIN[1]})
    monkeypatch.undo()
    assert job["state"] == "error" and "open in another program" in job["error"], job
    assert source.read_bytes() == before
    assert not (root / "v/wedding.mp4").exists()
    row = db.get_asset(conn, vid)
    assert row["rel_path"] == "v/wedding.mov" and row["ext"] == "mov"
    assert not (root / "_deleted/_originals/v/wedding.mov").exists()
    scan(scanned)
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE rel_path LIKE 'v/wedding%'"
                        ).fetchone()[0] == 1
