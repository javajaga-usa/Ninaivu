"""Several files copied at once, with the two guarantees that used to forbid it.

Every decision — already done, duplicate, folder, name — is made on the walk
thread in walk order; the workers only copy, verify and record. So identical
photos are stored once however many workers there are, and a dry run names
exactly the files the real run then writes.
"""

import os
import shutil
import sys
import threading
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


def _photo(path, shade, day=1, pad=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new("RGB", (80, 60), (shade % 256, 90, (shade * 7) % 256))
    exif = img.getexif()
    exif[0x9003] = f"2018:06:{day:02d} 10:00:00"
    img.save(path, "JPEG", exif=exif)
    with open(path, "ab") as handle:
        handle.write(b"\xfe" * pad)
    return str(path)


def _drive(root):
    """A drive with every awkward shape at once.

    Twins in other folders, same names on the same day in different folders,
    runs of same-sized different photos, and twins that sit next to each other
    so they are in flight together.
    """
    paths = []
    for folder in range(4):
        for i in range(10):
            paths.append(_photo(root / f"album{folder}" / f"IMG_{i:03}.jpg",
                                shade=folder * 10 + i, day=1 + i % 3, pad=i * 50))
    # Same-named, same day, different bytes: one gets _1, _2, _3.
    for folder in range(4):
        _photo(root / f"album{folder}" / "clash.jpg", shade=200 + folder, day=5, pad=folder * 10)
    # Twins: the same bytes under other names and folders, beside each other
    # and far apart.
    (root / "backup").mkdir()
    shutil.copy(paths[3], root / "album0" / "IMG_003 copy.jpg")
    shutil.copy(paths[3], root / "album3" / "zz_twin_of_003.jpg")
    shutil.copy(paths[17], root / "backup" / "IMG_017.jpg")
    shutil.copy(paths[17], root / "backup" / "IMG_017 (1).jpg")
    shutil.copy(paths[25], root / "backup" / "x.jpg")
    # Same size, different bytes, same day: a size collision that is not a twin.
    _photo(root / "backup" / "same_size_a.jpg", shade=240, day=9, pad=333)
    _photo(root / "backup" / "same_size_b.jpg", shade=241, day=9, pad=333)
    return root


def _unique_contents(root):
    import hashlib
    seen = set()
    for folder, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith(".jpg"):
                with open(os.path.join(folder, name), "rb") as handle:
                    seen.add(hashlib.sha256(handle.read()).hexdigest())
    return seen


def _archived(dest):
    return [n for _f, _d, names in os.walk(dest) for n in names if n.endswith(".jpg")]


def _rows():
    return {r["source_path"]: r for r in db.get_recent_files(10000)}


@pytest.mark.parametrize("workers", [1, 4])
def test_identical_photos_are_stored_once_however_many_workers(work, workers):
    drive = _drive(work / "drive")
    dest = work / "dest"
    job = ArchiveJob([str(drive)], str(dest), workers=workers)
    job.run()
    assert db.get_job(job.job_id)["state"] == "completed"
    rows = _rows()
    verified = [r for r in rows.values() if r["status"] == "verified"]
    duplicates = [r for r in rows.values() if r["status"] == "duplicate"]
    assert len(verified) == len(_unique_contents(drive)), "every content once"
    assert len(duplicates) == 5, "the five twins are duplicates"
    assert len(verified) + len(duplicates) == len(rows)
    assert _unique_contents(dest) == _unique_contents(drive)
    assert len(_archived(dest)) == len(verified), "nothing landed twice"
    assert not [n for _f, _d, names in os.walk(dest) for n in names
                if n.startswith(scanner.PARTIAL_PREFIX)]


def test_a_dry_run_names_exactly_what_four_workers_then_write(work):
    drive = _drive(work / "drive")
    dest = work / "dest"
    ArchiveJob([str(drive)], str(dest), mode=scanner.MODE_DRY_RUN, workers=4).run()
    predicted = {p: (r["status"], r["destination_path"]) for p, r in _rows().items()}
    assert {s for s, _p in predicted.values()} == {"planned", "plan-duplicate"}

    ArchiveJob([str(drive)], str(dest), workers=4).run()
    actual = {p: (r["status"], r["destination_path"]) for p, r in _rows().items()}
    assert set(predicted) == set(actual)
    equivalent = {"planned": "verified", "plan-duplicate": "duplicate"}
    for path, (status, landed) in predicted.items():
        assert equivalent[status] == actual[path][0], os.path.basename(path)
        assert landed == actual[path][1], os.path.basename(path)
    suffixed = sorted(os.path.basename(p) for _s, p in actual.values()
                      if os.path.basename(p).startswith("clash_"))
    assert suffixed == ["clash_1.jpg", "clash_2.jpg", "clash_3.jpg"]


def test_the_same_walk_order_gives_the_same_names_as_one_worker(work):
    """Which same-named photo gets the plain name is the walk's to say,
    not the worker that finishes first."""
    drive = _drive(work / "drive")
    one = work / "one"
    four = work / "four"
    ArchiveJob([str(drive)], str(one), workers=1).run()
    by_one = {p: os.path.relpath(r["destination_path"], one) for p, r in _rows().items()}
    db.clear_db()
    db.init_db()
    ArchiveJob([str(drive)], str(four), workers=4).run()
    by_four = {p: os.path.relpath(r["destination_path"], four) for p, r in _rows().items()}
    assert by_one == by_four


def test_twins_in_flight_together_are_waited_for_not_copied_twice(work):
    """Two identical files handed to two workers at once: the second is
    judged only once the first has verified, so it is a duplicate."""
    drive = work / "drive"
    a = _photo(drive / "a" / "one.jpg", shade=1, pad=4000)
    (drive / "b").mkdir()
    shutil.copy(a, drive / "a" / "two.jpg")
    shutil.copy(a, drive / "b" / "three.jpg")
    dest = work / "dest"
    job = ArchiveJob([str(drive)], str(dest), workers=4)
    waited = []
    original = job._await_same_size

    def noticing(size):
        waited.append(size)
        return original(size)
    job._await_same_size = noticing
    slow = ArchiveJob._copy_and_hash

    def slowly(self, src, tmp):
        time.sleep(0.2)
        return slow(self, src, tmp)
    job._copy_and_hash = slowly.__get__(job)
    job.run()
    rows = _rows()
    assert sorted(r["status"] for r in rows.values()) == ["duplicate", "duplicate", "verified"]
    assert waited, "the twin was waited for"
    assert len(_archived(dest)) == 1


def test_a_run_short_of_room_and_a_stop_leave_no_half_files(work):
    drive = _drive(work / "drive")
    dest = work / "dest"
    job = ArchiveJob([str(drive)], str(dest), workers=4)
    original = ArchiveJob._copy_and_hash
    seen = {"n": 0}

    def stop_after_six(self, src, tmp):
        seen["n"] += 1
        if seen["n"] == 6:
            job.gate.cancel()
        return original(self, src, tmp)
    job._copy_and_hash = stop_after_six.__get__(job)
    job.run()
    assert db.get_job(job.job_id)["state"] == "stopped"
    assert not [n for _f, _d, names in os.walk(dest) for n in names
                if n.startswith(scanner.PARTIAL_PREFIX)]
    done = db.finished_count()
    assert 0 < done < 50

    again = ArchiveJob([str(drive)], str(dest), workers=4)
    again.run()
    assert db.get_job(again.job_id)["state"] == "completed"
    assert again.stepped_over == done
    rows = _rows()
    assert len([r for r in rows.values() if r["status"] == "verified"]) == len(_unique_contents(drive))


def test_a_failure_to_decide_is_recorded_and_the_rest_goes_on(work, monkeypatch):
    drive = _drive(work / "drive")
    dest = work / "dest"
    real = scanner.capture_date

    def bad_date(path, st=None, kind=None):
        if path.endswith("IMG_004.jpg"):
            raise OSError(5, "Input/output error")
        return real(path, st=st, kind=kind)
    monkeypatch.setattr(scanner, "capture_date", bad_date)
    job = ArchiveJob([str(drive)], str(dest), workers=4)
    job.run()
    assert db.get_job(job.job_id)["state"] == "completed"
    errors = [r for r in _rows().values() if r["status"] == "error"]
    assert len(errors) == 4 and all(r["source_path"].endswith("IMG_004.jpg") for r in errors)
    assert job.processed == len(_rows())


def test_the_walk_thread_alone_decides(work):
    """No worker ever chooses a name or judges a duplicate."""
    drive = _drive(work / "drive")
    dest = work / "dest"
    job = ArchiveJob([str(drive)], str(dest), workers=4)
    walk = threading.get_ident()
    deciders = set()
    original = job._decide

    def noting(path, name):
        deciders.add(threading.get_ident())
        return original(path, name)
    job._decide = noting
    job.run()
    assert deciders == {walk}
