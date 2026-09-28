"""A consolidation Ninaivu stopped in the middle of carries on when it starts.

Stopping Ninaivu pauses the archive job so it ends cleanly, and nothing started
it again: after every restart the archive sat idle until somebody noticed and
pressed Start. These check that start-up picks it up as Start would, leaves a
paused job paused, waits for folders that are not back yet, and leaves alone
anything that was not interrupted.
"""

import json
import time

import pytest
from PIL import Image

from ninaivu import archive
from ninaivu.archive import database as db
from ninaivu.archive import scanner


@pytest.fixture()
def work(tmp_path):
    archive.configure(tmp_path / "state")
    db.close_db()
    db.init_db()
    # Another module's test may have left a finished job object behind, and
    # "is there a job" is how these tests wait for one to start.
    scanner._job = None
    scanner.forget_estimate()
    try:
        yield tmp_path
    finally:
        scanner.stop_scan()
        if scanner._job is not None and scanner._job.thread is not None:
            scanner._job.gate.resume()
            scanner._job.thread.join(timeout=30)
        scanner._job = None
        scanner.forget_estimate()
        db.close_db()


def photos(root, count=6):
    for n in range(count):
        path = root / "album" / f"IMG_{n:04}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), (n * 30 % 256, 90, 200)).save(path, "JPEG")
    return root


def interrupted(source, dest, mode=scanner.MODE_COPY):
    """The job row a restart leaves behind: running, with no end."""
    sources = [{"path": str(source), "types": ["audio", "image", "video"]}]
    db.save_settings(sources, str(dest), deep_scan=True)
    return db.create_job(sources, str(dest), "YYYY/MM/DD", mode)


def wait_for_the_job():
    job = scanner._job
    assert job is not None, "nothing was started"
    for _ in range(600):
        try:
            job.thread.join(timeout=0.1)
        except RuntimeError:
            # Started in the next instant: the engine publishes the job and
            # then starts its thread.
            time.sleep(0.05)
            continue
        if not job.thread.is_alive():
            return
    raise AssertionError("the resumed job did not finish")


def job_row(job_id):
    return dict(db.get_db().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def test_an_interrupted_consolidation_carries_on(work):
    source, dest = photos(work / "source"), work / "dest"
    old = interrupted(source, dest)

    job, problems = scanner.resume_interrupted()
    assert problems == []
    assert job["id"] == old
    wait_for_the_job()

    assert job_row(old)["state"] == "interrupted"
    assert job_row(old)["ended_at"]
    latest = db.latest_job()
    assert latest["id"] != old and latest["state"] == "completed"
    assert json.loads(latest["sources"]) == json.loads(job_row(old)["sources"])
    archived = [p for p in dest.rglob("*.jpg")]
    assert len(archived) == 6


def test_nothing_to_pick_up(work):
    assert scanner.resume_interrupted() == (None, [])


@pytest.mark.parametrize("state", ["completed", "stopped", "failed"])
def test_a_job_that_ended_is_not_started_again(work, state):
    source, dest = photos(work / "source"), work / "dest"
    old = interrupted(source, dest)
    db.update_job(old, state=state, ended_at=time.time())
    assert scanner.resume_interrupted() == (None, [])
    assert scanner._job is None


def test_a_dry_run_is_not_started_again(work):
    source, dest = photos(work / "source"), work / "dest"
    interrupted(source, dest, mode=scanner.MODE_DRY_RUN)
    assert scanner.resume_interrupted() == (None, [])


def test_a_job_somebody_paused_comes_back_paused(work):
    source, dest = photos(work / "source"), work / "dest"
    interrupted(source, dest)
    scanner.remember_pause(True)

    job, problems = scanner.resume_interrupted()
    assert job is not None and problems == []
    assert scanner.is_scan_paused(), "a job paused by a person came back running"


def test_folders_that_are_not_back_yet_leave_it_to_try_again(work):
    """A network share after a reboot: say why, and keep the row to retry."""
    source, dest = work / "not-mounted-yet", work / "dest"
    old = interrupted(source, dest)

    job, problems = scanner.resume_interrupted()
    assert job["id"] == old and problems
    assert job_row(old)["state"] == "running", "a start that failed closed the job"

    photos(source)                                   # the share is back
    job, problems = scanner.resume_interrupted()
    assert problems == []
    wait_for_the_job()
    assert job_row(old)["state"] == "interrupted"


def test_start_pressed_before_the_pick_up_closes_the_old_job(work):
    """Start pressed in the moments after a restart, before start-up got to the
    interrupted job: the new run went ahead, the pick-up found a job already
    running and did nothing, and the old row said "running" from then on — on
    the machine this was found on, five runs later."""
    source, dest = photos(work / "source"), work / "dest"
    old = interrupted(source, dest)

    ok, problems, _ = scanner.start_scan([str(source)], str(dest))
    assert ok, problems
    wait_for_the_job()

    row = job_row(old)
    assert row["state"] == "interrupted" and row["ended_at"]
    assert db.latest_job()["state"] == "completed"


def test_a_running_job_is_left_alone(work):
    source, dest = photos(work / "source", count=40), work / "dest"
    scanner.start_scan([str(source)], str(dest))
    scanner.pause_scan()
    try:
        assert scanner.resume_interrupted() == (None, [])
    finally:
        scanner.resume_scan()


def test_start_up_keeps_trying_until_the_folders_are_back(work, cfg, monkeypatch):
    """Services._resume_archive, with the waits shortened."""
    from ninaivu import Services

    services = Services(cfg)
    # Services points the engine at Ninaivu's state directory; this test's
    # archive lives there too from here on.
    source, dest = work / "share", work / "dest"
    old = interrupted(source, dest)
    monkeypatch.setattr(Services, "ARCHIVE_RESUME_WAIT", 0.05)
    yielded = []
    monkeypatch.setattr("ninaivu.api.archive_api._yield_the_disk",
                        lambda scanner_, label, power=None: yielded.append(label))

    services._resume_archive()
    assert scanner._job is None, "started although the source was missing"
    photos(source)
    for _ in range(200):
        if scanner._job is not None:
            break
        time.sleep(0.02)
    wait_for_the_job()
    assert yielded == ["a consolidation is running"]
    db.close_db()
    db.init_db()
    assert job_row(old)["state"] == "interrupted"


def test_pause_and_resume_in_the_console_are_remembered(as_admin, app):
    """Shutting down pauses the job too, so the console's own Pause is what
    says a person chose it."""
    def remembered():
        db.close_db()
        db.init_db()
        return bool(db.get_config(scanner.PAUSED_BY_PERSON))

    assert as_admin.post("/api/archive/pause").status_code == 200
    assert remembered()
    assert as_admin.post("/api/archive/resume").status_code == 200
    assert not remembered()
    as_admin.post("/api/archive/pause")
    assert as_admin.post("/api/archive/stop").status_code == 200
    assert not remembered()
