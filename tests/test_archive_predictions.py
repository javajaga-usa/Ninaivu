"""What an import predicts before it copies, and going ahead short of room.

The estimate on the Import page says how many of the files found are new and
how many are already in the archive, how much the new ones need and how long
they should take, and what was held back. The free-space check counts the
new files only, and a run short of room can be told to continue anyway. A
dry run reads only the files that could be duplicates, so it is quick.
"""

import os
import sys
import time

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ninaivu import archive                                     # noqa: E402
from ninaivu.archive import database as db                      # noqa: E402
from ninaivu.archive import scanner                             # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                  # noqa: E402


@pytest.fixture()
def work(tmp_path, monkeypatch):
    archive.configure(tmp_path / "state")
    db.close_db()
    db.init_db()
    scanner.forget_estimate()
    monkeypatch.setattr(scanner, "RETRY_PAUSE_SECONDS", 0)
    try:
        yield tmp_path
    finally:
        scanner.forget_estimate()
        db.close_db()


def _photo(path, shade, size=(80, 60)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new("RGB", size, (shade % 256, 90, (shade * 7) % 256))
    exif = img.getexif()
    exif[0x9003] = f"2017:03:{1 + shade % 27:02d} 10:00:00"
    img.save(path, "JPEG", exif=exif)
    # Distinct sizes, so no two photos could be duplicates by size alone.
    with open(path, "ab") as handle:
        handle.write(b"\xff" * (shade * 97))
    return str(path)


def _source(root, n=6):
    return [_photo(root / "DCIM" / f"IMG_{i:04}.jpg", 100 + i) for i in range(n)]


def _run(sources, dest, mode=scanner.MODE_COPY, **kw):
    job = ArchiveJob([str(s) for s in sources], str(dest), mode=mode, **kw)
    job.run()
    return db.get_job(job.job_id), job


def _estimate(job):
    """What the console's estimate works out, from the same walk."""
    files = total = done = done_bytes = 0
    batch = {}
    for root, name in job._walk():
        files += 1
        total += job.last_size
        batch[os.path.join(root, name)] = job.last_size
    done, done_bytes = job._finished_among(batch)
    return files, total, done, done_bytes


# ---------------------------------------------------------------------------
# New against already done
# ---------------------------------------------------------------------------

def test_finished_files_are_known_by_path_size_and_archive(work):
    dest = work / "dest"
    _source(work / "card")
    _run([work / "card"], dest)
    rows = {os.path.basename(r["source_path"]): r for r in db.get_recent_files(100)}
    path = rows["IMG_0001.jpg"]["source_path"]
    size = rows["IMG_0001.jpg"]["size"]

    assert db.finished_sizes_among({path: size}, destination_root=str(dest)) == (1, size)
    assert db.finished_sizes_among({path: None}, destination_root=str(dest)) == (1, size)
    # A different file now at that path is still to do.
    assert db.finished_sizes_among({path: size + 1}, destination_root=str(dest)) == (0, 0)
    # Finished into another archive is not finished here.
    assert db.finished_sizes_among({path: size}, destination_root=str(work / "other")) == (0, 0)
    # The old count-only question still answers.
    assert db.finished_among([path]) == 1
    assert db.finished_among([]) == 0


def test_the_estimate_tells_new_files_from_those_already_in(work):
    dest = work / "dest"
    paths = _source(work / "card")
    whole = sum(os.path.getsize(p) for p in paths)

    files, total, done, done_bytes = _estimate(ArchiveJob([str(work / "card")], str(dest)))
    assert (files, total, done, done_bytes) == (6, whole, 0, 0)

    _run([work / "card"], dest)
    # Two more photos land on the card: the estimate must say two, not eight.
    _photo(work / "card" / "DCIM" / "IMG_9001.jpg", 300)
    _photo(work / "card" / "DCIM" / "IMG_9002.jpg", 301)
    files, total, done, done_bytes = _estimate(ArchiveJob([str(work / "card")], str(dest)))
    assert files == 8 and done == 6 and done_bytes == whole
    assert total - done_bytes == sum(
        os.path.getsize(work / "card" / "DCIM" / n) for n in ("IMG_9001.jpg", "IMG_9002.jpg"))


def test_the_count_before_copying_knows_the_bytes_already_done(work):
    dest = work / "dest"
    _source(work / "card")
    _, first = _run([work / "card"], dest)
    assert first.done_bytes == 0 and first.bytes_to_copy() == first.total_bytes > 0

    _photo(work / "card" / "DCIM" / "IMG_9001.jpg", 300)
    _, again = _run([work / "card"], dest)
    assert again.finished_in_run == 6
    assert again.done_bytes == first.total_bytes
    assert again.bytes_to_copy() == os.path.getsize(work / "card" / "DCIM" / "IMG_9001.jpg")


def test_start_takes_the_bytes_already_done_from_the_estimate(work, monkeypatch):
    dest = work / "dest"
    _source(work / "card")
    _run([work / "card"], dest)
    _photo(work / "card" / "DCIM" / "IMG_9001.jpg", 300)

    probe = ArchiveJob([str(work / "card")], str(dest))
    files, total, _done, done_bytes = _estimate(probe)
    scanner.remember_estimate(probe, files, total, done_bytes)
    assert scanner.recall_estimate(probe)[:3] == (files, total, done_bytes)

    monkeypatch.setattr(ArchiveJob, "preflight",
                        lambda self: pytest.fail("Start counted again"))
    _, job = _run([work / "card"], dest)
    assert job.done_bytes == done_bytes
    assert job.bytes_to_copy() == total - done_bytes


# ---------------------------------------------------------------------------
# Room for the new files, and continuing anyway
# ---------------------------------------------------------------------------

def _no_room(monkeypatch):
    seen = {}

    def tight(destination, required, headroom=1.10):
        seen["required"] = required
        return False, 1000, int(required * headroom)
    monkeypatch.setattr(scanner, "check_free_space", tight)
    return seen


def test_short_of_room_the_run_stops_before_copying_and_says_so(work, monkeypatch):
    dest = work / "dest"
    _source(work / "card")
    seen = _no_room(monkeypatch)
    job, _ = _run([work / "card"], dest)
    assert job["state"] == "failed" and job["phase"] == "needs-space"
    assert "continue anyway" in job["message"]
    assert "not yet in the archive" in job["message"]
    assert not os.path.isdir(dest / "2017"), "nothing was copied"
    assert seen["required"] > 0


def test_the_space_check_counts_only_the_new_files(work, monkeypatch):
    dest = work / "dest"
    _source(work / "card")
    _run([work / "card"], dest)
    new = _photo(work / "card" / "DCIM" / "IMG_9001.jpg", 300)
    seen = _no_room(monkeypatch)
    job, _ = _run([work / "card"], dest)
    assert job["phase"] == "needs-space"
    assert seen["required"] == os.path.getsize(new), \
        "the whole card was counted, not the one new photo"


def test_continue_anyway_copies_past_the_space_check(work, monkeypatch):
    dest = work / "dest"
    _source(work / "card")
    _no_room(monkeypatch)
    job, _ = _run([work / "card"], dest, continue_anyway=True)
    assert job["state"] == "completed", job["message"]
    assert db.finished_count() == 6
    with open(os.path.join(scanner.LOG_DIR, sorted(os.listdir(scanner.LOG_DIR))[-1])) as log:
        assert "CONTINUING ANYWAY" in log.read()


def test_a_drive_that_really_fills_still_stops_the_run(work, monkeypatch):
    import errno
    dest = work / "dest"
    _source(work / "card", n=3)
    _no_room(monkeypatch)
    original = ArchiveJob._copy_and_hash
    hits = {"n": 0}

    def full(self, src, tmp):
        hits["n"] += 1
        if hits["n"] == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(self, src, tmp)
    monkeypatch.setattr(ArchiveJob, "_copy_and_hash", full)
    job, _ = _run([work / "card"], dest, continue_anyway=True, workers=1)
    assert job["state"] == "failed" and "drive is full" in job["message"]
    assert db.finished_count() == 1, "what was copied before the drive filled is kept"


# ---------------------------------------------------------------------------
# A quick dry run
# ---------------------------------------------------------------------------

def _count_hashes(monkeypatch):
    hashed = []
    original = scanner.hash_file

    def counting(path, gate=None):
        hashed.append(os.path.basename(path))
        return original(path, gate)
    monkeypatch.setattr(scanner, "hash_file", counting)
    return hashed


def test_a_dry_run_reads_no_file_that_cannot_be_a_duplicate(work, monkeypatch):
    dest = work / "dest"
    _source(work / "card")
    hashed = _count_hashes(monkeypatch)
    job, _ = _run([work / "card"], dest, mode=scanner.MODE_DRY_RUN)
    assert job["state"] == "completed"
    assert hashed == [], "every photo has a size of its own, so none needed reading"
    rows = {os.path.basename(r["source_path"]): r for r in db.get_recent_files(100)}
    assert {r["status"] for r in rows.values()} == {"planned"}
    assert all(r["destination_path"].startswith(str(dest)) for r in rows.values())


def test_a_dry_run_still_finds_the_duplicates_a_real_run_would(work, monkeypatch):
    import shutil
    dest = work / "dest"
    paths = _source(work / "card")
    shutil.copy(paths[2], work / "card" / "again.jpg")            # exact twin
    hashed = _count_hashes(monkeypatch)
    job, _ = _run([work / "card"], dest, mode=scanner.MODE_DRY_RUN)
    assert job["state"] == "completed"
    rows = {os.path.basename(r["source_path"]): r for r in db.get_recent_files(100)}
    twins = {rows["again.jpg"]["status"], rows["IMG_0002.jpg"]["status"]}
    assert twins == {"planned", "plan-duplicate"}, twins
    second = "again.jpg" if rows["again.jpg"]["status"] == "plan-duplicate" else "IMG_0002.jpg"
    first = "IMG_0002.jpg" if second == "again.jpg" else "again.jpg"
    assert rows[second]["duplicate_of"].endswith(first)
    assert sorted(hashed) == ["IMG_0002.jpg", "again.jpg"], \
        "only the twins were read, each once, whichever the walk met first"

    # The real run then agrees with the prediction file for file.
    job, _ = _run([work / "card"], dest)
    rows = {os.path.basename(r["source_path"]): r for r in db.get_recent_files(100)}
    assert rows[second]["status"] == "duplicate"
    assert rows[first]["status"] == "verified"
    assert sum(1 for r in rows.values() if r["status"] == "verified") == 6


def test_a_dry_run_recognises_a_copy_already_at_the_target_name(work, monkeypatch):
    """A same-named, same-sized file at the destination is read, once, to tell
    the archive's own copy from a stranger with the same name."""
    import shutil
    dest = work / "dest"
    paths = _source(work / "card", n=2)
    _run([work / "card"], dest)
    db.clear_db()                       # the archive is on disk, the record gone
    db.init_db()
    hashed = _count_hashes(monkeypatch)
    job, _ = _run([work / "card"], dest, mode=scanner.MODE_DRY_RUN)
    assert job["state"] == "completed"
    rows = {os.path.basename(r["source_path"]): r for r in db.get_recent_files(100)}
    assert {r["status"] for r in rows.values()} == {"plan-duplicate"}
    assert sorted(hashed) == sorted(["IMG_0000.jpg", "IMG_0001.jpg"] * 2), hashed
    del shutil, paths


# ---------------------------------------------------------------------------
# How long it should take
# ---------------------------------------------------------------------------

def test_the_promise_is_the_assumed_rate_until_a_run_has_measured_one(work, monkeypatch):
    seconds, measured = scanner.predict_copy_seconds(scanner.ASSUMED_COPY_RATE * 90)
    assert (seconds, measured) == (90, False)
    assert scanner.predict_copy_seconds(0) == (0, False)
    # Tiny files still cost something each.
    assert scanner.predict_copy_seconds(10, new_files=2000)[0] >= 100

    db.remember_copy_rate(10 * 1024 * 1024)
    assert scanner.predict_copy_seconds(60 * 10 * 1024 * 1024) == (60, True)


def test_a_real_run_records_its_rate_for_the_next_estimate(work, monkeypatch):
    dest = work / "dest"
    _source(work / "card")
    assert db.copy_rate() is None
    monkeypatch.setattr(ArchiveJob, "RATE_MIN_SECONDS", 0.0)
    monkeypatch.setattr(ArchiveJob, "RATE_MIN_BYTES", 1)
    job, _ = _run([work / "card"], dest)
    assert job["state"] == "completed"
    assert db.copy_rate() > 0

    # A run that moved nothing new teaches nothing and keeps the figure.
    before = db.copy_rate()
    monkeypatch.setattr(ArchiveJob, "RATE_MIN_BYTES", 1)
    _run([work / "card"], dest)
    assert db.copy_rate() == before


def test_a_short_run_does_not_set_the_rate(work):
    dest = work / "dest"
    _source(work / "card")
    started = time.time()
    _run([work / "card"], dest)
    assert time.time() - started < ArchiveJob.RATE_MIN_SECONDS
    assert db.copy_rate() is None
