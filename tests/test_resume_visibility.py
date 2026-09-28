"""Saying that a run is picking up where the last one stopped.

"Start is also Resume" has been true since the engine was written — progress
lives in SQLite, so pressing Start after a power cut continues rather than
starting over. What it has never done is *say so*. A run resuming three hundred
thousand files into a job looks exactly like a run beginning: the same bar from
the same end, and the only way to tell was to watch the counter move
impossibly fast for a while and infer it.

That inference is the thing being replaced here. The engine already knows how
much was done before this run touched anything, and these tests hold it to
reporting that number.
"""

import pytest

from ninaivu.archive import database as adb
from ninaivu.archive.scanner import ArchiveJob, job_progress


@pytest.fixture()
def archive_db(tmp_path, monkeypatch):
    from ninaivu import archive
    archive.configure(tmp_path / "state", 0)
    adb.init_db()
    yield tmp_path
    adb.close_db()


def register(path, status):
    """Put a file into the archive's record in a given state."""
    adb.claim_file(path, path.rsplit("/", 1)[-1], 10, 1.0, job_id=1)
    adb.set_status(path, status)


# --- what the record can answer -------------------------------------------

def test_a_fresh_archive_has_nothing_done(archive_db):
    assert adb.finished_count() == 0


def test_finished_means_every_terminal_state_not_only_verified(archive_db):
    """A duplicate and a skip are as done as a copy. Counting only the copies
    would tell somebody resuming that thousands of decided files were still
    outstanding."""
    register("/src/a.jpg", "verified")
    register("/src/b.jpg", "duplicate")
    register("/src/c.jpg", "skipped")
    assert adb.finished_count() == 3


def test_files_still_in_flight_do_not_count_as_finished(archive_db):
    register("/src/a.jpg", "verified")
    register("/src/b.jpg", "pending")
    register("/src/c.jpg", "copying")
    register("/src/d.jpg", "error")
    assert adb.finished_count() == 1


def test_a_dry_runs_predictions_are_not_progress(archive_db):
    """Planned rows describe what *would* happen. Counting them would make a
    dry run look like a job three-quarters finished."""
    register("/src/a.jpg", "planned")
    register("/src/b.jpg", "plan-duplicate")
    register("/src/c.jpg", "plan-skip")
    assert adb.finished_count() == 0


# --- what the job reports --------------------------------------------------

def test_a_job_records_what_was_already_done_before_it_started(archive_db, tmp_path):
    for name in "abc":
        register(f"/src/{name}.jpg", "verified")

    job = ArchiveJob([{"path": str(tmp_path / "src"), "types": {"image"}}],
                     str(tmp_path / "Master"))
    job.note_prior_progress()
    assert job.resumed_from == 3


def test_a_first_run_is_not_reported_as_a_resume(archive_db, tmp_path):
    job = ArchiveJob([{"path": str(tmp_path / "src"), "types": {"image"}}],
                     str(tmp_path / "Master"))
    job.note_prior_progress()
    assert job.resumed_from == 0


def test_progress_says_nothing_about_resuming_when_idle(archive_db):
    assert job_progress()["resumed_from"] == 0
    assert job_progress()["is_resume"] is False


# --- end to end: a real run, stopped and started again ---------------------

def read_log():
    """Everything the engine wrote to its run logs in this test."""
    import glob
    from ninaivu.archive.scanner import LOG_DIR
    out = []
    for path in sorted(glob.glob(f"{LOG_DIR}/*.log")):
        with open(path, encoding="utf-8") as fh:
            out.append(fh.read())
    return "\n".join(out)


def jpeg(path, marker):
    """A distinct little JPEG, big enough not to be filtered as a thumbnail."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0" + marker * 400 + b"\xff\xd9")
    return path


def test_a_second_run_over_the_same_drive_reports_the_first_ones_work(
        archive_db, tmp_path):
    """The whole point, exercised for real rather than through a stub.

    Run a job to completion, then press Start again on the same sources — the
    engine's own definition of resuming — and it must say how much was already
    settled instead of presenting itself as a job beginning.
    """
    source = tmp_path / "OldDrive"
    for n in range(7):
        jpeg(str(source / "2019" / f"IMG_{n}.jpg"), bytes([65 + n]))
    dest = str(tmp_path / "Master")

    first = ArchiveJob([{"path": str(source), "types": {"image"}}], dest)
    first.run()
    assert first.resumed_from == 0, "a first run is not a resume"
    done = adb.finished_count()
    assert done == 7, done

    second = ArchiveJob([{"path": str(source), "types": {"image"}}], dest)
    second.run()
    assert second.resumed_from == 7

    # The job's *message* is whatever is true now, and by the time a run has
    # finished that is "complete" — so the durable evidence is the run log,
    # which outlives the database and the browser tab.
    assert "RESUMING: 7 files already finished" in read_log(), read_log()


def test_a_resumed_run_tells_finished_files_apart_from_new_ones(
        archive_db, tmp_path):
    """The counter a resume is judged by.

    One number for "checked" made a correct resume look like the archive being
    copied again: thousands of files counted while only a handful were new.
    """
    source = tmp_path / "OldDrive"
    for n in range(7):
        jpeg(str(source / "2019" / f"IMG_{n}.jpg"), bytes([65 + n]))
    dest = str(tmp_path / "Master")

    first = ArchiveJob([{"path": str(source), "types": {"image"}}], dest)
    first.run()
    assert (first.processed, first.stepped_over) == (7, 0)

    for n in range(7, 9):
        jpeg(str(source / "2020" / f"IMG_{n}.jpg"), bytes([65 + n]))
    second = ArchiveJob([{"path": str(source), "types": {"image"}}], dest)
    second.run()
    assert (second.processed, second.stepped_over) == (9, 7)
    assert adb.finished_count() == 9


def test_the_estimate_is_timed_on_new_work_not_on_stepping_over(
        archive_db, tmp_path, monkeypatch):
    """Stepping over a finished file is a lookup. Timing the rest of a resumed
    run at that speed promised hours of copying in minutes."""
    import time
    from ninaivu.archive import scanner as scanner_mod

    job = ArchiveJob([{"path": str(tmp_path / "src"), "types": {"image"}}],
                     str(tmp_path / "Master"))
    job.total_files, job.resumed_from = 1000, 600
    job.processed, job.stepped_over = 500, 400      # 100 new files in 100 s
    job.started_at = time.time() - 100
    monkeypatch.setattr(scanner_mod, "_job", job)
    monkeypatch.setattr(scanner_mod, "is_scanning", lambda: True)

    progress = job_progress()
    assert progress["stepped_over"] == 400
    # 500 files ahead, 200 of them already finished: 300 new at 1 file/s.
    assert 290 <= progress["eta_seconds"] <= 310, progress

    job.stepped_over = 500                          # nothing new timed yet
    assert job_progress()["eta_seconds"] is None


def test_the_estimate_is_not_zero_when_the_archive_holds_more_than_the_run(
        archive_db, tmp_path, monkeypatch):
    """Seen live: 171,885 files done in the archive, a 170,050-file run,
    160,121 checked - and "about 0s left" with ten thousand still to go.
    The archive-wide count includes other sources' files, so it cannot say
    how much of what is ahead is finished."""
    import time
    from ninaivu.archive import scanner as scanner_mod

    job = ArchiveJob([{"path": str(tmp_path / "src"), "types": {"image"}}],
                     str(tmp_path / "Master"))
    job.total_files, job.resumed_from = 170_050, 171_885
    job.processed, job.stepped_over = 160_121, 158_444
    job.started_at = time.time() - 1677             # 1 new file a second
    monkeypatch.setattr(scanner_mod, "_job", job)
    monkeypatch.setattr(scanner_mod, "is_scanning", lambda: True)

    # Totals from an estimate: only the archive-wide figure is known.
    assert 9_900 <= job_progress()["eta_seconds"] <= 9_960

    # Counted by the run's own walk: exact.
    job.finished_in_run = 162_444                   # 4,000 finished still ahead
    progress = job_progress()
    assert 5_900 <= progress["eta_seconds"] <= 5_960, progress
    assert progress["finished_in_run"] == 162_444


def test_the_count_learns_which_of_the_runs_own_files_are_finished(
        archive_db, tmp_path):
    source = tmp_path / "OldDrive"
    for n in range(5):
        jpeg(str(source / f"IMG_{n}.jpg"), bytes([65 + n]))
    dest = str(tmp_path / "Master")
    ArchiveJob([{"path": str(source), "types": {"image"}}], dest).run()

    for n in range(5, 8):
        jpeg(str(source / f"IMG_{n}.jpg"), bytes([65 + n]))
    register("/elsewhere/a.jpg", "verified")        # another source's work

    second = ArchiveJob([{"path": str(source), "types": {"image"}}], dest)
    second.preflight()
    assert (second.total_files, second.finished_in_run) == (8, 5)


def test_the_resume_note_is_absent_on_a_first_run(archive_db, tmp_path):
    source = tmp_path / "OldDrive"
    for n in range(3):
        jpeg(str(source / f"IMG_{n}.jpg"), bytes([65 + n]))

    ArchiveJob([{"path": str(source), "types": {"image"}}],
               str(tmp_path / "Master")).run()
    assert "RESUMING" not in read_log()


def test_the_running_job_carries_the_resume_note_in_its_message(
        archive_db, tmp_path):
    """What the console reads while the run is still going.

    Caught at the enumerating phase, because the message is replaced as the
    run moves on — a status line reports now, not history.
    """
    source = tmp_path / "OldDrive"
    for n in range(4):
        jpeg(str(source / f"IMG_{n}.jpg"), bytes([65 + n]))
    dest = str(tmp_path / "Master")
    ArchiveJob([{"path": str(source), "types": {"image"}}], dest).run()

    seen = {}
    real_update = adb.update_job

    def spy(job_id, **fields):
        if fields.get("phase") == "enumerating":
            seen["message"] = fields.get("message", "")
        return real_update(job_id, **fields)

    from ninaivu.archive import scanner as scanner_mod
    scanner_mod.db.update_job = spy
    try:
        ArchiveJob([{"path": str(source), "types": {"image"}}], dest).run()
    finally:
        scanner_mod.db.update_job = real_update

    assert "resuming" in seen.get("message", "").lower(), seen
    assert "4 files were already done" in seen["message"], seen
