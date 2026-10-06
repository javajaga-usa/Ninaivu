"""Several files go up at once, and each still goes up exactly once.

Measured on this library: every file waited about 2.2 s on Google before any of
it moved, then went at 32 Mbit/s. One at a time, a library of photographs used
a third of the line: 1,000 to 1,600 files an hour. Several at once overlap
those waits. What must not change is the promise: a file goes up once, into
the one folder Ninaivu made, with the folders made once.
"""
import threading
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from fake_drive import FakeDrive
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import limits, store
from ninaivu.server import workload as workload_mod
from ninaivu.storage import db


class GoogleTakesItsTime:
    """The fake, answering several callers at once as Google does.

    FakeDrive keeps plain dicts, so each call is answered under a lock, and the
    wait before it is outside the lock, where the engine's calls overlap.
    """

    def __init__(self, fake, delay=0.02):
        self.fake, self.delay = fake, delay
        self._lock = threading.Lock()
        self.inflight = self.most = 0

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.inflight += 1
            self.most = max(self.most, self.inflight)
        try:
            time.sleep(self.delay)
            with self._lock:
                return self.fake(*args, **kwargs)
        finally:
            with self._lock:
                self.inflight -= 1


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
def google(fake):
    return GoogleTakesItsTime(fake)


def client_for(transport, *, expired=False):
    creds = drive_mod.Credentials(
        client_id="cid", client_secret="secret", refresh_token="r",
        access_token="a", expires_at=0.0 if expired else 9e9, folder_name="Ninaivu")
    return drive_mod.DriveClient(creds=creds, transport=transport)


def library(conn, root, names):
    for rel in names:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode() * 64)
        store.remember(conn, str(root), rel, size=path.stat().st_size,
                       filename=path.name)


def run(db_path, client, **kwargs):
    engine = engine_mod.SyncEngine(connect=lambda: client,
                                   open_db=lambda: db.connect(db_path), **kwargs)
    engine.start()
    engine._thread.join(60)
    assert not engine.running, "the engine did not finish"
    return engine


def test_several_go_up_at_once_and_each_exactly_once(conn, db_path, fake, google, tmp_path):
    names = [f"2019/07/p{n:02}.jpg" for n in range(24)]
    library(conn, tmp_path / "lib", names)
    run(db_path, client_for(google), parallel=3)

    assert google.most >= 2, "nothing overlapped"
    assert google.most <= 3 + 1, "more at once than asked for"
    sent = Counter(fake.uploaded_names())
    assert sorted(sent) == sorted(n.rsplit("/", 1)[1] for n in names)
    assert set(sent.values()) == {1}, "a file went up twice"
    assert store.summary(conn)["done"] == 24


def test_a_folder_is_made_once_however_many_want_it_at_the_same_moment(
        conn, db_path, fake, google, tmp_path):
    library(conn, tmp_path / "lib", [f"2026/09/p{n:02}.jpg" for n in range(12)])
    run(db_path, client_for(google), parallel=4)
    made = Counter(f["name"] for f in fake.folders.values())
    assert made["2026"] == 1 and made["09"] == 1, made
    assert made["Ninaivu"] == 1


def test_an_expired_token_is_refreshed_once_not_by_every_upload(
        conn, db_path, fake, google, tmp_path):
    library(conn, tmp_path / "lib", [f"2019/p{n:02}.jpg" for n in range(9)])
    run(db_path, client_for(google, expired=True), parallel=3)
    refreshes = [c for c in fake.calls if "oauth2" in c[1]]
    assert len(refreshes) == 1, refreshes
    assert store.summary(conn)["done"] == 9


def test_a_hold_stops_files_starting_and_sends_nothing_twice(
        conn, db_path, fake, google, tmp_path):
    """Somebody pressed Pause, or the household needs the machine: what is
    going up stops where it is, and what has not started stays pending."""
    library(conn, tmp_path / "lib", [f"2019/p{n:02}.jpg" for n in range(12)])
    checks = []

    def hold():
        checks.append(1)
        return "someone is using Ninaivu" if len(checks) > 6 else None

    engine = engine_mod.SyncEngine(connect=lambda: client_for(google),
                                   open_db=lambda: db.connect(db_path),
                                   hold=hold, parallel=3)
    engine.start()
    deadline = time.time() + 20
    while time.time() < deadline and not engine.state.snapshot()["held_for"]:
        time.sleep(0.05)
    engine.stop(join=True)

    states = Counter(r["state"] for r in conn.execute("SELECT state FROM cloud_uploads"))
    assert states["uploading"] == 0, "a file left marked as going up"
    assert states["done"] + states["pending"] == 12
    assert set(Counter(fake.uploaded_names()).values()) <= {1}
    assert states["done"] < 12, "the hold held nothing back"


def test_one_at_a_time_keeps_the_order(conn, db_path, fake, tmp_path):
    names = [f"2019/p{n:02}.jpg" for n in range(12)]
    library(conn, tmp_path / "lib", names)
    run(db_path, client_for(fake), parallel=1)
    assert fake.uploaded_names() == [n.rsplit("/", 1)[1] for n in names]


def test_the_console_reads_the_files_in_flight_as_one():
    state = engine_mod.SyncState()
    state.begin("a", "IMG_1.jpg", 100)
    state.begin("b", "IMG_2.jpg", 300)
    state.progress(50, 100, "a")
    state.progress(100, 300, "b")
    snap = state.snapshot()
    assert snap["current"] == "IMG_1.jpg and 1 more"
    assert (snap["current_sent"], snap["current_total"]) == (150, 400)
    state.finish("a")
    snap = state.snapshot()
    assert snap["current"] == "IMG_2.jpg" and snap["current_total"] == 300
    # Until the clock has visibly moved: before Python 3.13 Windows counts
    # it in 15 ms steps, and a fixed 10 ms nap can land inside one.
    started = time.monotonic()
    while time.monotonic() == started:
        time.sleep(0.005)
    state.progress(200, 300, "b")
    assert state.snapshot()["speed_bps"] > 0, "counted across files, never backwards"


def test_the_speed_cap_is_shared_by_every_upload():
    bucket = limits.RateLimiter(1000, burst_seconds=1.0)
    took = []

    def take():
        bucket.take(500)
        took.append(time.monotonic())

    started = time.monotonic()
    threads = [threading.Thread(target=take) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    # 2,000 bytes against 1,000 a second with a 1,000-byte bucket: about a second.
    assert max(took) - started >= 0.8


# -- full speed ---------------------------------------------------------------

def workload(**settings):
    cfg = SimpleNamespace(workload_mode="quiet", workload_night_start="21:00",
                          workload_night_end="09:00", cloud_full_speed=False, **settings)
    return workload_mod.Workload(cfg)


def test_quiet_makes_the_backup_wait_for_the_household():
    w = workload()
    w.noticed("/api/assets")
    assert w.hold("upload") == "someone is using Ninaivu"
    assert w.boost("upload") is False


def test_full_speed_keeps_the_backup_going_and_nothing_else():
    w = workload()
    w.cfg.cloud_full_speed = True
    w.noticed("/api/file/1", ranged=True)
    assert w.hold("upload") is None and w.boost("upload") is True
    assert w.hold("analysis") == "someone is watching a video"


def test_full_speed_goes_by_day_in_overnight_mode_too():
    w = workload()
    w.cfg.workload_mode = "overnight"
    w.cfg.cloud_full_speed = True
    assert w.hold("upload") is None


def test_the_service_starts_the_engine_with_the_setting(tmp_path):
    from ninaivu.cloud.service import CloudService
    from ninaivu.server.config import Config

    cfg = Config()
    cfg.state_dir = tmp_path
    cfg.cloud_parallel = 4
    service = CloudService(cfg, connect_db=lambda: db.connect(tmp_path / "i.db"))
    assert service.engine().parallel == 4
    cfg.cloud_parallel = 2
    service.apply_settings()
    assert service.engine().parallel == 2
    assert service.status()["parallel"] == 2 and service.status()["full_speed"] is False


def test_bytes_sent_within_one_clock_tick_still_count_toward_the_speed(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    state = engine_mod.SyncState()
    state.begin("a", "IMG_1.jpg", 1000)
    state.progress(100, 1000, "a")
    state.progress(300, 1000, "a")      # same tick as the one before
    clock[0] += 1.0
    state.progress(400, 1000, "a")
    assert state.snapshot()["speed_bps"] == 300.0
