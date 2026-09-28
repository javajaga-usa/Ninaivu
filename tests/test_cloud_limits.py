"""How fast the upload goes, and when it is allowed to go at all.

Two limits, and between them one behaviour that matters more than either: an
upload caught by the end of the hours it was allowed to run in has to stop
where it is, keep what already went, and carry on the next night — without
being recorded as a failure. Get that wrong in the obvious way (reuse the
failure path) and a household that sets an overnight window turns its whole
library into five failed attempts over five nights.

So the tests here are in three parts:

* the arithmetic, on its own, with no thread and no network — a window that
  crosses midnight, and a token bucket that paces bytes
* the engine under both limits, driven through ``tests/fake_drive.py``
* the console, refusing a time nobody could have meant

The clock tests build their timestamps with :func:`time.mktime` from a local
date rather than hard-coding epoch seconds, so they mean the same thing in
every timezone the suite might run in.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from fake_drive import FakeDrive
from ninaivu.server import auth
from ninaivu import build_services, create_admin_app
from ninaivu.storage import db
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import limits, store

from conftest import ADMIN, login


# --- fixtures --------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "index.db"
    store.init_schema(db.init_db(path))
    return path


@pytest.fixture()
def conn(db_path):
    return db.connect(db_path)


@pytest.fixture()
def fake():
    return FakeDrive()


@pytest.fixture()
def client(fake):
    creds = drive_mod.Credentials(
        client_id="cid", client_secret="secret", refresh_token="r",
        access_token="a", expires_at=9e9, folder_name="Ninaivu")
    return drive_mod.DriveClient(creds=creds, transport=fake)


#: Big enough to cross several chunks once the chunk size is cut down, small
#: enough that a paced upload of it does not slow the suite down.
BEACH = 3 * 1024 * 1024
REL = os.path.join("2019", "07", "beach.jpg")


@pytest.fixture()
def library(tmp_path):
    root = tmp_path / "lib"
    (root / "2019" / "07").mkdir(parents=True)
    (root / "2019" / "07" / "beach.jpg").write_bytes(b"B" * BEACH)
    return root


def build(db_path, client, **kwargs):
    return engine_mod.SyncEngine(
        connect=lambda: client, open_db=lambda: db.connect(db_path), **kwargs)


def queue_beach(conn, library):
    store.remember(conn, str(library), REL, size=BEACH, filename="beach.jpg")


def run_to_completion(engine, timeout=60):
    engine.start()
    engine._thread.join(timeout)
    assert not engine.running, "the engine did not finish"


def wait_until(predicate, timeout=15.0):
    """Poll until it is true. Returns whether it became true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def session_puts(fake):
    """The chunk PUTs — how many round trips a file actually took."""
    return [c for c in fake.calls
            if c[0] == "PUT" and "upload.example/session" in c[1]]


def at(hour, minute=0):
    """A timestamp for today at this local time, whatever the timezone."""
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                        hour, minute, 0, 0, 0, -1))


# --- the window, as arithmetic --------------------------------------------

def test_no_window_means_any_hour():
    window = limits.Window("", "")
    assert window.always_open
    assert window.is_open(at(3))
    assert window.is_open(at(14))
    assert window.seconds_until_open(at(14)) == 0.0
    assert window.label() == ""


def test_a_daytime_window_is_open_only_inside_it():
    window = limits.Window("09:00", "17:00")
    assert not window.is_open(at(8, 59))
    assert window.is_open(at(9, 0))
    assert window.is_open(at(16, 59))
    assert not window.is_open(at(17, 0))


def test_an_overnight_window_crosses_midnight():
    """The case the feature exists for. "22:00 to 07:00" is what a household
    means by overnight, and it is not two windows."""
    window = limits.Window("22:00", "07:00")
    assert window.is_open(at(22, 0))
    assert window.is_open(at(23, 30))
    assert window.is_open(at(0, 30))
    assert window.is_open(at(6, 59))
    assert not window.is_open(at(7, 0))
    assert not window.is_open(at(12, 0))
    assert not window.is_open(at(21, 59))


def test_a_window_that_starts_when_it_ends_is_read_as_any_hour():
    """Somebody who has not finished typing. Of the two readings, a backup
    that never runs again is the worse one, and it is silent."""
    window = limits.Window("23:00", "23:00")
    assert window.always_open
    assert window.is_open(at(4))


@pytest.mark.parametrize("start,end", [
    ("", "07:00"), ("22:00", ""), ("nonsense", "07:00"),
    ("22:00", "half past six"), ("25:00", "07:00"), ("22:70", "07:00"),
    ("2200", "0700"),
])
def test_a_time_that_is_not_a_time_leaves_it_unrestricted(start, end):
    """Unparseable has to fail open. A typo in a settings box must not be able
    to stop the backup at an hour nobody chose."""
    assert limits.Window(start, end).always_open


def test_how_long_until_it_opens():
    window = limits.Window("22:00", "07:00")
    assert window.seconds_until_open(at(21, 0)) == pytest.approx(3600, abs=61)
    assert window.seconds_until_open(at(12, 0)) == pytest.approx(10 * 3600,
                                                                abs=61)
    # Inside the window there is nothing to wait for.
    assert window.seconds_until_open(at(23, 0)) == 0.0
    assert window.seconds_until_open(at(3, 0)) == 0.0


def test_midnight_written_as_the_end_of_a_day():
    assert limits.parse_clock("24:00") == 0
    assert limits.format_clock(limits.parse_clock("24:00")) == "00:00"
    assert limits.format_clock(limits.parse_clock("7:5")) == "07:05"


def test_the_window_says_what_it_is_for_the_console():
    assert limits.Window("22:00", "07:00").label() == "22:00–07:00"


# --- the ceiling, as arithmetic -------------------------------------------

def test_no_limit_costs_nothing():
    limiter = limits.RateLimiter(0)
    assert not limiter.limited
    started = time.monotonic()
    limiter.take(500 * 1024 * 1024)
    assert time.monotonic() - started < 0.1


def test_a_limit_actually_holds_the_caller_up():
    rate = 200_000
    limiter = limits.RateLimiter(rate)
    limiter.take(rate)                    # empties the bucket, no wait
    started = time.monotonic()
    limiter.take(rate // 2)               # half a second's worth
    elapsed = time.monotonic() - started
    assert elapsed >= 0.35, elapsed


def test_the_average_settles_on_the_ceiling():
    rate = 400_000
    limiter = limits.RateLimiter(rate)
    started = time.monotonic()
    for _ in range(4):
        limiter.take(rate // 2)           # two seconds of data in total
    elapsed = time.monotonic() - started
    # One second of it was in the bucket to begin with, so a second of paced
    # transmission is the floor. Generous at the top: a loaded machine is not
    # a broken limiter.
    assert 0.7 <= elapsed <= 4.0, elapsed


def test_a_chunk_bigger_than_the_bucket_does_not_deadlock():
    """A low cap and a coarse chunk must not wedge the worker for ever. The
    balance is allowed to go negative and the refill works it off."""
    limiter = limits.RateLimiter(50_000)
    started = time.monotonic()
    limiter.take(5_000_000)               # a hundred seconds' worth
    assert time.monotonic() - started < 1.0


def test_pausing_cuts_a_rate_limited_wait_short():
    import threading
    stop = threading.Event()
    limiter = limits.RateLimiter(1000, stop=stop)
    limiter.take(1000)
    stop.set()
    started = time.monotonic()
    limiter.take(1_000_000)               # would be a thousand seconds
    assert time.monotonic() - started < 0.5


# --- the chunk size that goes with a ceiling ------------------------------

def test_without_a_cap_the_chunk_is_left_alone():
    assert limits.chunk_for(0, drive_mod.CHUNK) == drive_mod.CHUNK


def test_a_cap_shrinks_the_chunk_to_about_a_second():
    assert limits.chunk_for(2 * 1024 * 1024, drive_mod.CHUNK) == 2 * 1024 * 1024
    assert limits.chunk_for(512 * 1024, drive_mod.CHUNK) == 512 * 1024


def test_a_very_low_cap_still_gets_one_whole_unit():
    """Google will not take a chunk that is not a multiple of 256 KiB, so that
    is the floor however slow the line is."""
    assert limits.chunk_for(1024, drive_mod.CHUNK) == limits.STEP


def test_a_cap_above_the_default_does_not_enlarge_the_chunk():
    assert limits.chunk_for(500 * 1024 * 1024, drive_mod.CHUNK) == drive_mod.CHUNK


@pytest.mark.parametrize("kbps", [1, 100, 256, 999, 2048, 100_000])
def test_every_chunk_size_is_one_google_will_accept(kbps):
    size = limits.chunk_for(kbps * 1024, drive_mod.CHUNK)
    assert size % limits.STEP == 0 and size > 0


# --- a pause is not a failure ---------------------------------------------

def test_releasing_a_file_keeps_its_place_and_its_resume_point(conn):
    store.remember(conn, "/lib", "a.jpg", size=10)
    store.save_resume(conn, "/lib", "a.jpg", "https://upload.example/session/1")
    store.mark_uploading(conn, "/lib", "a.jpg")
    store.release(conn, "/lib", "a.jpg")

    row = store.recent(conn)[0]
    assert row["state"] == store.PENDING
    assert row["attempts"] == 0
    assert row["resume_url"] == "https://upload.example/session/1"


def test_being_stopped_every_night_never_turns_into_a_failure(conn):
    """The bug this is here to prevent: an overnight window closing on the
    same large video five nights running, and Ninaivu calling it broken."""
    store.remember(conn, "/lib", "big.mov", size=10)
    for _ in range(store.MAX_ATTEMPTS + 3):
        store.mark_uploading(conn, "/lib", "big.mov")
        store.release(conn, "/lib", "big.mov")

    assert store.state_of(conn, "/lib", "big.mov") == store.PENDING
    assert store.recent(conn)[0]["attempts"] == 0
    assert store.summary(conn)["failed"] == 0


# --- the engine under a window --------------------------------------------

class ClosingWindow:
    """Open for a set number of chunks, then shut for the rest of the day.

    Stands in for the end of an overnight window arriving part-way through a
    large file, which is the moment the whole design has to survive and which
    a real clock cannot be asked to produce on demand.
    """

    def __init__(self, chunks: int):
        self.left = chunks
        self.always_open = False

    def is_open(self, now=None) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True

    def seconds_until_open(self, now=None) -> float:
        return 0.0 if self.left > 0 else 3600.0

    def label(self) -> str:
        return "22:00–07:00"


def test_nothing_goes_up_outside_the_hours(conn, db_path, client, fake, library):
    queue_beach(conn, library)
    # Shut whatever the clock happens to say when the suite runs.
    shut = limits.Window("22:00", "07:00")
    shut.is_open = lambda now=None: False
    shut.seconds_until_open = lambda now=None: 1800.0

    engine = build(db_path, client, window=shut)
    engine.start()
    assert wait_until(lambda: engine.state.snapshot()["waiting_for_window"])

    snapshot = engine.state.snapshot()
    assert snapshot["running"] is True, "it should be alive and waiting"
    assert snapshot["window_opens_at"] > 0
    assert fake.uploads == [], "it uploaded outside the hours it was given"

    engine.stop(join=True)
    assert not engine.running
    assert store.state_of(conn, str(library), REL) == store.PENDING


def test_waiting_for_the_window_still_stops_at_once(conn, db_path, client,
                                                    library):
    """A four-hour wait must not become a four-hour Pause button."""
    queue_beach(conn, library)
    shut = limits.Window("22:00", "07:00")
    shut.is_open = lambda now=None: False
    shut.seconds_until_open = lambda now=None: 4 * 3600.0

    engine = build(db_path, client, window=shut)
    engine.start()
    assert wait_until(lambda: engine.state.snapshot()["waiting_for_window"])

    started = time.monotonic()
    engine.stop(join=True, timeout=10)
    assert time.monotonic() - started < 2.0
    assert not engine.running


def test_an_empty_queue_does_not_sit_waiting_for_the_window(db_path, client):
    """Nothing to send is nothing to wait for: the run ends and the thread
    goes, rather than holding a database reader open until 22:00."""
    shut = limits.Window("22:00", "07:00")
    shut.is_open = lambda now=None: False
    shut.seconds_until_open = lambda now=None: 3600.0

    engine = build(db_path, client, window=shut)
    run_to_completion(engine, timeout=15)
    assert engine.state.snapshot()["waiting_for_window"] is False


def test_the_hours_ending_mid_file_keeps_what_already_went(
        conn, db_path, client, fake, library, monkeypatch):
    """Stopped part-way, and honest about it: still pending, no attempt
    counted, and a resume point so the next night sends the remainder."""
    monkeypatch.setattr(engine_mod, "CHUNK", limits.STEP)
    queue_beach(conn, library)

    engine = build(db_path, client, window=ClosingWindow(3))
    engine.start()
    assert wait_until(lambda: engine.state.snapshot()["waiting_for_window"])
    engine.stop(join=True)

    row = store.recent(conn)[0]
    assert row["state"] == store.PENDING, "a pause was recorded as something else"
    assert row["attempts"] == 0, "the end of the hours was counted as a failure"
    assert row["resume_url"], "it threw away the resume point"
    assert fake.uploads == [], "it claimed a part-sent file had landed"

    # Some of it did go, which is the point of stopping between chunks rather
    # than abandoning the file.
    session = next(iter(fake.sessions.values()))
    assert 0 < session["received"] < BEACH


def test_a_file_caught_by_the_hours_carries_on_the_next_night(
        conn, db_path, client, fake, library, monkeypatch):
    """End to end, and the reason any of this is safe: the second run finishes
    the file, and the bytes in Drive are the bytes on disk."""
    monkeypatch.setattr(engine_mod, "CHUNK", limits.STEP)
    queue_beach(conn, library)

    first = build(db_path, client, window=ClosingWindow(3))
    first.start()
    assert wait_until(lambda: first.state.snapshot()["waiting_for_window"])
    first.stop(join=True)
    stopped_at = next(iter(fake.sessions.values()))["received"]

    # The hours come round again.
    run_to_completion(build(db_path, client, window=limits.Window()))

    assert store.state_of(conn, str(library), REL) == store.DONE
    assert fake.uploaded_names() == ["beach.jpg"]
    assert fake.content("beach.jpg") == (library / "2019/07/beach.jpg").read_bytes()
    # It resumed rather than starting the file again.
    assert stopped_at > 0
    assert len(fake.sessions) == 1, "it began a second upload of the same file"


def test_an_open_window_changes_nothing(conn, db_path, client, fake, library):
    queue_beach(conn, library)
    always = limits.Window("00:00", "")          # unparseable end: no window
    run_to_completion(build(db_path, client, window=always))
    assert fake.uploaded_names() == ["beach.jpg"]
    assert store.state_of(conn, str(library), REL) == store.DONE


# --- the engine under a ceiling -------------------------------------------

def test_without_a_cap_a_file_goes_in_one_chunk(conn, db_path, client, fake,
                                                library):
    queue_beach(conn, library)
    run_to_completion(build(db_path, client))
    assert len(session_puts(fake)) == 1


def test_a_cap_cuts_the_file_into_smaller_chunks_and_paces_them(
        conn, db_path, client, fake, library):
    """A cap enforced by sending eight megabytes flat out and then sleeping is
    a cap on the average and no cap on what the household notices, so the
    chunk has to come down with it."""
    queue_beach(conn, library)
    started = time.monotonic()
    run_to_completion(build(db_path, client, rate_kbps=2048))
    elapsed = time.monotonic() - started

    assert len(session_puts(fake)) >= 2, "the chunk size ignored the cap"
    assert elapsed >= 0.25, f"nothing was paced ({elapsed:.2f}s)"
    assert fake.content("beach.jpg") == (library / "2019/07/beach.jpg").read_bytes()
    assert store.state_of(conn, str(library), REL) == store.DONE


def test_a_cap_set_while_it_runs_is_picked_up(db_path, client):
    """Read on every chunk, so the fix for "it is eating the broadband" is not
    "stop the thing eating the broadband"."""
    engine = build(db_path, client, rate_kbps=0)
    assert not engine._limiter().limited
    engine.rate_kbps = 512
    assert engine._limiter().rate == 512 * 1024
    engine.rate_kbps = 0
    assert not engine._limiter().limited


# --- the console ----------------------------------------------------------

@pytest.fixture()
def console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    return login(app.test_client(), *ADMIN), cfg, services


def test_the_limits_start_off(console):
    client, _, _ = console
    body = client.get("/api/cloud/status").get_json()
    assert body["rate_kbps"] == 0
    assert body["window_start"] == "" and body["window_end"] == ""
    assert body["window_label"] == ""
    assert body["window_open"] is True


def test_a_speed_limit_is_saved_and_reported(console):
    client, cfg, _ = console
    body = client.post("/api/cloud/settings", json={"rate_kbps": 750}).get_json()
    assert body["rate_kbps"] == 750
    assert cfg.cloud_rate_kbps == 750
    assert "750 KB/s" in " ".join(body["changed"])


def test_hours_are_saved_and_reported(console):
    client, cfg, _ = console
    body = client.post("/api/cloud/settings",
                       json={"window_start": "22:00",
                             "window_end": "7:00"}).get_json()
    assert cfg.cloud_window_start == "22:00"
    assert cfg.cloud_window_end == "07:00", "it was not normalised"
    assert body["window_label"] == "22:00–07:00"


def test_the_limits_survive_a_restart(console):
    """Read back off disk rather than through ``Config.load()``, which would
    look in the real state directory instead of this test's."""
    client, cfg, _ = console
    client.post("/api/cloud/settings",
                json={"rate_kbps": 512, "window_start": "23:30",
                      "window_end": "06:15"})

    saved = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    assert saved["cloud_rate_kbps"] == 512
    assert saved["cloud_window_start"] == "23:30"
    assert saved["cloud_window_end"] == "06:15"


def test_clearing_the_hours_goes_back_to_any_time(console):
    client, cfg, _ = console
    client.post("/api/cloud/settings",
                json={"window_start": "22:00", "window_end": "07:00"})
    body = client.post("/api/cloud/settings",
                       json={"window_start": "", "window_end": ""}).get_json()
    assert body["window_label"] == ""
    assert body["window_open"] is True
    assert cfg.cloud_window_start == ""


@pytest.mark.parametrize("payload", [
    {"window_start": "half past ten", "window_end": "07:00"},
    {"window_start": "22:00", "window_end": "99:99"},
    {"window_start": "2200", "window_end": "0700"},
])
def test_a_time_nobody_could_have_meant_is_refused(console, payload):
    """Rejected here rather than ignored quietly. Being told the time was
    wrong is the difference between fixing a typo and finding out in a month
    that the nightly backup never ran."""
    client, cfg, _ = console
    response = client.post("/api/cloud/settings", json=payload)
    assert response.status_code == 400
    assert "22:00" in response.get_json()["error"]
    assert cfg.cloud_window_start == "", "a bad value was stored anyway"


def test_a_speed_limit_that_is_not_a_number_is_refused(console):
    client, cfg, _ = console
    response = client.post("/api/cloud/settings", json={"rate_kbps": "fast"})
    assert response.status_code == 400
    assert cfg.cloud_rate_kbps == 0


def test_a_negative_speed_limit_means_no_limit(console):
    """Not an error worth a dialogue — clamped, and reported back as what it
    became so the console cannot show something the engine is not doing."""
    client, cfg, _ = console
    body = client.post("/api/cloud/settings", json={"rate_kbps": -5}).get_json()
    assert body["rate_kbps"] == 0
    assert cfg.cloud_rate_kbps == 0


@pytest.mark.parametrize("invalid", [{"rate_kbps": "fast"}, {"window_end": "99:99"},
                                     {"autostart": "false"}])
def test_rejected_settings_do_not_change_backup_policy(console, invalid, monkeypatch):
    client, cfg, services = console
    cfg.cloud_enabled = True
    before = (cfg.cloud_enabled, cfg.cloud_rate_kbps)
    pauses = []
    monkeypatch.setattr(services.cloud, "pause", lambda: pauses.append(True))
    response = client.post("/api/cloud/settings", json={
        "enabled": False, "rate_kbps": 512, **invalid})
    assert response.status_code == 400
    assert (cfg.cloud_enabled, cfg.cloud_rate_kbps) == before
    assert pauses == []


def test_a_running_upload_is_told_about_a_new_limit(console):
    client, cfg, services = console
    engine = services.cloud.engine()
    client.post("/api/cloud/settings",
                json={"rate_kbps": 256, "window_start": "22:00",
                      "window_end": "07:00"})
    assert engine.rate_kbps == 256
    assert engine.window.label() == "22:00–07:00"
