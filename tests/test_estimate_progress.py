"""Saying what the estimate is doing while it does it.

Adding a whole drive as a source starts a walk of every folder on it. That can
run for minutes, and until now the console said one static word — "Estimating…"
— for the whole of it. There is no way to tell a walk that is working through
four hundred thousand files from one that has hit a spun-down disk and hung,
which is the difference between waiting and rebooting.

So the walk now publishes what it is doing: how many files it has counted, how
many bytes those come to, which folder it is in at this moment, and how long it
has been going. These tests hold that reporting to three promises — it is live,
it is honest about a walk that has finished, and a walk nobody is waiting for
any more can be called off rather than being left to finish against a disk.
"""

import threading
import time

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu.api import archive_api
from ninaivu.archive.scanner import ArchiveJob
from ninaivu.server import auth
from ninaivu import build_services, create_admin_app

MB = 1024 * 1024

#: Big enough that the walk reports more than once, so a cancellation has
#: somewhere to land. A drive is very much bigger; the point is only that the
#: fixture is not smaller than one reporting interval.
FOLDERS = ("2019", "2021", "2023")
PER_FOLDER = 40
TOTAL = len(FOLDERS) * PER_FOLDER


@pytest.fixture()
def console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    return app, login(app.test_client(), *ADMIN)


@pytest.fixture()
def shoebox(tmp_path):
    """Enough files, in enough folders, for a walk to be observable."""
    src = tmp_path / "OldDrive"
    for folder in FOLDERS:
        (src / folder).mkdir(parents=True)
        for i in range(PER_FOLDER):
            Image.new("RGB", (64, 48), (i * 5 % 255, 80, 120)).save(
                src / folder / f"pic{i}.jpg")
    return src


def body(source, dest, **extra):
    return {"source_dirs": [{"path": str(source), "types": ["image", "video", "audio"]}],
            "destination_dir": str(dest), **extra}


# --- the progress record itself -------------------------------------------

def test_an_unknown_token_is_not_running_rather_than_an_error(console):
    """The browser polls before the walk has started. That is not a fault."""
    _, client = console
    response = client.get("/api/archive/capacity/progress?token=never-existed")
    assert response.status_code == 200
    assert response.get_json()["running"] is False


def test_a_finished_walk_says_so(console, shoebox, tmp_path):
    _, client = console
    result = client.post("/api/archive/capacity",
                         json=body(shoebox, tmp_path / "Master",
                                   progress_token="done-1")).get_json()
    assert result["ok"], result
    assert result["files"] == TOTAL

    snapshot = client.get(
        "/api/archive/capacity/progress?token=done-1").get_json()
    assert snapshot["running"] is False
    assert snapshot["finished"] is True
    assert snapshot["files"] == TOTAL


def test_the_progress_endpoint_needs_an_admin(console):
    app, _ = console
    anonymous = app.test_client()
    assert anonymous.get(
        "/api/archive/capacity/progress?token=x").status_code in (401, 403)


# --- it is live ------------------------------------------------------------

def test_progress_is_readable_while_the_walk_is_still_running(
        console, shoebox, tmp_path, monkeypatch):
    """The count, the folder and the clock, mid-walk — not only at the end.

    The walk is held open at the fourth file so the assertion is about what the
    server publishes rather than about how fast the test machine is.
    """
    app, client = console
    reached = threading.Event()
    release = threading.Event()
    # Held as the walk accepts its fourth file: the counting loop has reported
    # the first three, and has not yet counted this one.
    real_wanted = ArchiveJob._wanted
    counted = {"n": 0}

    def held(self, *args, **kwargs):
        wanted = real_wanted(self, *args, **kwargs)
        if wanted:
            counted["n"] += 1
            if counted["n"] == 4:
                reached.set()
                release.wait(10)
        return wanted

    monkeypatch.setattr(ArchiveJob, "_wanted", held)

    outcome = {}

    def walk():
        worker = app.test_client()
        login(worker, *ADMIN)
        outcome["result"] = worker.post(
            "/api/archive/capacity",
            json=body(shoebox, tmp_path / "Master",
                      progress_token="live-1")).get_json()

    thread = threading.Thread(target=walk, daemon=True)
    thread.start()
    try:
        assert reached.wait(10), "the walk never got going"
        snapshot = client.get(
            "/api/archive/capacity/progress?token=live-1").get_json()
    finally:
        release.set()
        thread.join(15)

    assert snapshot["running"] is True
    assert snapshot["files"] >= 3, snapshot
    assert snapshot["bytes"] > 0, snapshot
    assert snapshot["folder"], "the walk does not say where it is"
    assert "OldDrive" in snapshot["folder"]
    assert snapshot["elapsed"] >= 0
    assert outcome["result"]["ok"] is True


# --- a walk nobody is waiting for can be called off ------------------------

def test_cancelling_stops_the_walk_instead_of_letting_it_finish(
        console, shoebox, tmp_path, monkeypatch):
    """Changing the sources must not leave the old walk grinding the disk."""
    app, client = console
    reached = threading.Event()
    release = threading.Event()
    # Held as the walk accepts its fourth file: the counting loop has reported
    # the first three, and has not yet counted this one.
    real_wanted = ArchiveJob._wanted
    counted = {"n": 0}

    def held(self, *args, **kwargs):
        wanted = real_wanted(self, *args, **kwargs)
        if wanted:
            counted["n"] += 1
            if counted["n"] == 4:
                reached.set()
                release.wait(10)
        return wanted

    monkeypatch.setattr(ArchiveJob, "_wanted", held)

    outcome = {}

    def walk():
        worker = app.test_client()
        login(worker, *ADMIN)
        outcome["result"] = worker.post(
            "/api/archive/capacity",
            json=body(shoebox, tmp_path / "Master",
                      progress_token="cancel-1")).get_json()

    thread = threading.Thread(target=walk, daemon=True)
    thread.start()
    try:
        assert reached.wait(10)
        stop = client.post("/api/archive/capacity/cancel",
                           json={"token": "cancel-1"})
        assert stop.status_code == 200
    finally:
        release.set()
        thread.join(15)

    result = outcome["result"]
    assert result["ok"] is False
    assert result["cancelled"] is True
    # It stopped where it was, rather than walking the whole folder.
    assert result["files"] < TOTAL, result


def test_cancelling_a_token_nobody_knows_is_harmless(console):
    _, client = console
    assert client.post("/api/archive/capacity/cancel",
                       json={"token": "not-a-real-walk"}).status_code == 200


# --- the record does not grow without bound --------------------------------

def test_old_progress_records_are_forgotten(console, shoebox, tmp_path):
    """One entry per estimate, and the console makes one per keystroke."""
    _, client = console
    for n in range(archive_api.MAX_ESTIMATE_RECORDS + 6):
        client.post("/api/archive/capacity",
                    json=body(shoebox, tmp_path / "Master",
                              progress_token=f"many-{n}"))
    assert len(archive_api._estimates) <= archive_api.MAX_ESTIMATE_RECORDS

    # The newest is still there — trimming must drop the oldest, not the last.
    last = f"many-{archive_api.MAX_ESTIMATE_RECORDS + 5}"
    assert client.get(
        f"/api/archive/capacity/progress?token={last}").get_json()["finished"]


# --- and none of this changed the answer -----------------------------------

def test_an_estimate_without_a_token_still_works(console, shoebox, tmp_path):
    """Every existing caller passes no token at all."""
    _, client = console
    result = client.post("/api/archive/capacity",
                         json=body(shoebox, tmp_path / "Master")).get_json()
    assert result["ok"] and result["files"] == TOTAL


# --- one walk per job, however many ask -------------------------------------

def _hold_at_fourth_file(monkeypatch):
    """Hold the walk as it accepts its fourth file, and count what it accepts."""
    reached = threading.Event()
    release = threading.Event()
    real_wanted = ArchiveJob._wanted
    counted = {"n": 0}

    def held(self, *args, **kwargs):
        wanted = real_wanted(self, *args, **kwargs)
        if wanted:
            counted["n"] += 1
            if counted["n"] == 4:
                reached.set()
                release.wait(10)
        return wanted

    monkeypatch.setattr(ArchiveJob, "_wanted", held)
    return reached, release, counted


def _estimate_in_thread(app, outcome, key, payload):
    def walk():
        worker = app.test_client()
        login(worker, *ADMIN)
        outcome[key] = worker.post("/api/archive/capacity", json=payload).get_json()
    thread = threading.Thread(target=walk, daemon=True)
    thread.start()
    return thread


def test_a_second_request_for_the_same_estimate_joins_the_walk_under_way(
        console, shoebox, tmp_path, monkeypatch):
    """A page refreshed mid-count, or a second tab, reads the count where it
    has got to instead of starting a second walk of the same drive."""
    app, client = console
    reached, release, counted = _hold_at_fourth_file(monkeypatch)
    outcome = {}
    first = _estimate_in_thread(app, outcome, "first",
                                body(shoebox, tmp_path / "Master", progress_token="tab-1"))
    try:
        assert reached.wait(10), "the walk never got going"
        second = _estimate_in_thread(app, outcome, "second",
                                     body(shoebox, tmp_path / "Master", progress_token="tab-2"))
        # The second token reports the first walk's progress, not a zero.
        deadline = time.time() + 5
        while time.time() < deadline:
            snapshot = client.get("/api/archive/capacity/progress?token=tab-2").get_json()
            if snapshot["known"]:
                break
            time.sleep(0.05)
        assert snapshot["running"] is True and snapshot["files"] >= 3, snapshot
    finally:
        release.set()
        first.join(15)
    second.join(15)

    assert outcome["first"]["ok"] and outcome["second"]["ok"]
    assert outcome["first"]["files"] == outcome["second"]["files"] == TOTAL
    assert counted["n"] == TOTAL, "the folder was walked once, not twice"


def test_a_walk_nobody_polls_any_more_is_called_off(
        console, shoebox, tmp_path, monkeypatch):
    """The page that asked has gone: finishing the count would be for nobody."""
    app, client = console
    monkeypatch.setattr(archive_api, "ABANDONED_AFTER_SECONDS", 0.05)
    reached, release, _counted = _hold_at_fourth_file(monkeypatch)
    outcome = {}
    thread = _estimate_in_thread(app, outcome, "result",
                                 body(shoebox, tmp_path / "Master", progress_token="gone-1"))
    try:
        assert reached.wait(10)
        time.sleep(0.2)                 # longer than the patience, with no poll
    finally:
        release.set()
        thread.join(15)
    result = outcome["result"]
    assert result["ok"] is False and result["cancelled"] is True, result
    assert result["files"] < TOTAL, result


def test_a_walk_without_a_token_is_never_abandoned(console, shoebox, tmp_path, monkeypatch):
    """A script asks once and waits; nothing polls for it, and that is fine."""
    _, client = console
    monkeypatch.setattr(archive_api, "ABANDONED_AFTER_SECONDS", 0.0)
    result = client.post("/api/archive/capacity",
                         json=body(shoebox, tmp_path / "Master")).get_json()
    assert result["ok"] is True and result["files"] == TOTAL, result


def test_starting_a_run_calls_off_the_estimate_under_way(
        console, shoebox, tmp_path, monkeypatch):
    """The run counts for itself: an estimate still walking beside it would be
    a second reader on the same drive, showing a second number."""
    app, client = console
    reached, release, _counted = _hold_at_fourth_file(monkeypatch)
    outcome = {}
    thread = _estimate_in_thread(app, outcome, "estimate",
                                 body(shoebox, tmp_path / "Master", progress_token="pre-run"))
    try:
        assert reached.wait(10)
        started = client.post("/api/archive/start",
                              json=body(shoebox, tmp_path / "Master", mode="dry-run"))
        assert started.status_code == 200, started.get_json()
    finally:
        release.set()
        thread.join(15)
    assert outcome["estimate"]["ok"] is False and outcome["estimate"]["cancelled"] is True
    deadline = time.time() + 30
    while time.time() < deadline:
        status = client.get("/api/archive/status").get_json()
        if not status["is_scanning"]:
            break
        time.sleep(0.1)
    assert status["is_scanning"] is False, status


# --- the run's own count says as much as the estimate did -------------------

def test_the_runs_count_reports_size_time_and_where_it_is(
        console, shoebox, tmp_path, monkeypatch):
    app, client = console
    reached, release, _counted = _hold_at_fourth_file(monkeypatch)
    try:
        started = client.post("/api/archive/start",
                              json=body(shoebox, tmp_path / "Master", mode="dry-run"))
        assert started.status_code == 200, started.get_json()
        assert reached.wait(10), "the run never started counting"
        status = client.get("/api/archive/status").get_json()
    finally:
        release.set()
    assert status["is_scanning"] is True and status["stage"] == "counting", status
    assert status["counted"] >= 3, status
    assert status["counted_bytes"] > 0, status
    assert status["counting_seconds"] is not None and status["counting_seconds"] >= 0
    assert "OldDrive" in status["counting_folder"], status
    deadline = time.time() + 30
    while time.time() < deadline:
        status = client.get("/api/archive/status").get_json()
        if not status["is_scanning"]:
            break
        time.sleep(0.1)
    assert status["is_scanning"] is False, status
    assert status["counting_folder"] == "" and status["counting_seconds"] is None
