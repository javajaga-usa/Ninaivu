"""Background work sharing the machine with the people using it.

The rules live in ninaivu/server/workload.py; these check each mode against a
clock the test controls, then that the jobs actually ask — the scanner, the
cloud upload, the storage check — and that the family app is what tells it.
"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.server import workload as wl


class Clock:
    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def local(hour, minute=0):
    """A timestamp at this local time of day, today."""
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0,
                        0, 0, -1))


def make(mode="balanced", at=None, night=("23:00", "06:00")):
    cfg = SimpleNamespace(workload_mode=mode, workload_night_start=night[0],
                          workload_night_end=night[1])
    clock = Clock(at if at is not None else local(14))
    return wl.Workload(cfg, clock=clock), clock


def watch_a_video(work):
    work.noticed("/api/file/12", ranged=True)


def browse(work):
    work.noticed("/api/segments")


# -- what counts as somebody using the app -------------------------------------

def test_polling_is_not_somebody_browsing():
    """A tab left open polls for ever; it must not hold the machine for ever."""
    work, _ = make()
    for path in ("/api/status/activity", "/api/status", "/api/events",
                 "/healthz", "/static/js/app.js", "/sw.js"):
        work.noticed(path)
    assert not work.browsing()


def test_a_byte_range_of_an_original_is_somebody_watching():
    work, clock = make()
    watch_a_video(work)
    assert work.watching() and work.browsing()
    clock.now += wl.WATCHING_SECONDS + 1
    assert not work.watching()


def test_a_whole_photograph_is_browsing_not_watching():
    work, _ = make()
    work.noticed("/api/file/12")
    assert work.browsing() and not work.watching()


def test_walking_away_frees_the_machine():
    work, clock = make("quiet")
    browse(work)
    assert work.hold(wl.ANALYSIS)
    clock.now += wl.BROWSING_SECONDS + 1
    assert work.hold(wl.ANALYSIS) is None


# -- the three modes ------------------------------------------------------------

def test_balanced_keeps_going_while_people_browse():
    work, _ = make("balanced")
    browse(work)
    assert all(work.hold(job) is None for job in wl.JOBS)


def test_balanced_makes_way_for_a_video_but_not_with_the_upload():
    work, _ = make("balanced")
    watch_a_video(work)
    for job in (wl.INDEX, wl.ANALYSIS, wl.CHECK):
        assert work.hold(job) == "someone is watching a video"
    assert work.hold(wl.UPLOAD) is None
    assert not work.boost(wl.UPLOAD), "no idle-time chunks while a video plays"


def test_quiet_waits_for_anyone_but_still_indexes_new_files():
    work, _ = make("quiet")
    browse(work)
    assert work.hold(wl.INDEX) is None
    for job in (wl.ANALYSIS, wl.UPLOAD, wl.CHECK):
        assert work.hold(job) == "someone is using Ninaivu"
    watch_a_video(work)
    assert work.hold(wl.INDEX) == "someone is watching a video"


def test_quiet_never_takes_the_idle_boost():
    work, _ = make("quiet")
    assert not work.boost(wl.UPLOAD)


def test_overnight_saves_the_heavy_work_for_the_night():
    work, _ = make("overnight", at=local(14))
    assert work.hold(wl.INDEX) is None, "new photographs appear at once"
    for job in (wl.ANALYSIS, wl.UPLOAD, wl.CHECK):
        assert work.hold(job) == "saved for the night (23:00–06:00)"


def test_overnight_runs_flat_out_at_night_unless_a_video_is_on():
    work, _ = make("overnight", at=local(2))
    browse(work)
    assert all(work.hold(job) is None for job in wl.JOBS)
    assert work.boost(wl.UPLOAD), "browsing at night does not slow the night's work"
    watch_a_video(work)
    assert work.hold(wl.ANALYSIS) == "someone is watching a video"
    assert work.hold(wl.UPLOAD) is None


def test_an_unknown_mode_is_balanced():
    work, _ = make("turbo")
    assert work.mode == "balanced"


# -- waiting a turn ---------------------------------------------------------------

def test_waiting_ends_when_the_video_does(monkeypatch):
    monkeypatch.setattr(wl, "POLL_SECONDS", 0.01)
    work, clock = make("balanced")
    watch_a_video(work)
    said = []

    def later():
        time.sleep(0.05)
        clock.now += wl.WATCHING_SECONDS + 1

    threading.Thread(target=later).start()
    assert work.wait_turn(wl.ANALYSIS, threading.Event(), on_hold=said.append)
    assert said == ["someone is watching a video", None]


def test_a_stop_ends_the_wait_at_once(monkeypatch):
    monkeypatch.setattr(wl, "POLL_SECONDS", 30)
    work, _ = make("balanced")
    watch_a_video(work)
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    started = time.time()
    assert work.wait_turn(wl.ANALYSIS, stop) is False
    assert time.time() - started < 5


# -- the jobs ask -----------------------------------------------------------------

class Gate:
    """A workload the test opens and closes by hand."""

    def __init__(self):
        self.reason = None
        self.asked = []

    def hold(self, job):
        self.asked.append(job)
        return self.reason

    def wait_turn(self, job, stop=None, on_hold=None):
        return wl.Workload.wait_turn(self, job, stop, on_hold)


def test_the_scanner_makes_way_and_says_so(scanned, monkeypatch):
    from ninaivu.media.scanner import Scanner

    monkeypatch.setattr(wl, "POLL_SECONDS", 0.01)
    cfg, _, _ = scanned
    (Path(cfg.active_root) / "misc" / "new.png").write_bytes(
        (Path(cfg.active_root) / "misc" / "plain.png").read_bytes())
    gate = Gate()
    gate.reason = "someone is watching a video"
    scanner = Scanner(cfg)
    scanner.workload = gate
    seen = []

    def release():
        deadline = time.time() + 5
        while time.time() < deadline and not scanner.progress.held:
            time.sleep(0.01)
        seen.append(scanner.progress.held)
        gate.reason = None

    threading.Thread(target=release).start()
    scanner._run(Path(cfg.active_root), full=False)

    assert seen == ["someone is watching a video"]
    assert scanner.progress.status == "done" and scanner.progress.added == 1
    assert scanner.progress.held == ""
    assert "index" in gate.asked


def test_the_strip_shows_a_scan_making_way_as_waiting():
    from ninaivu.server import activity

    progress = SimpleNamespace(snapshot=lambda: {
        "running": True, "status": "tagging", "held": "someone is watching a video",
        "tagged": 3, "tag_total": 10})
    job = activity._indexing(SimpleNamespace(scanner=SimpleNamespace(
        progress=progress, deferred=None)), None)
    assert job["paused"] and job["detail"] == "Waiting — someone is watching a video"


def test_the_upload_waits_its_turn_between_files(tmp_path, monkeypatch):
    from fake_drive import FakeDrive
    from ninaivu.cloud import drive as drive_mod, engine as engine_mod, store
    from ninaivu.storage import db

    monkeypatch.setattr(engine_mod, "HOLD_POLL", 0.01)
    db_path = tmp_path / "index.db"
    conn = db.init_db(db_path)
    store.init_schema(conn)
    library = tmp_path / "lib"
    library.mkdir()
    for name in ("a.jpg", "b.jpg"):
        (library / name).write_bytes(name.encode() * 500)
        store.remember(conn, str(library), name, size=1000, filename=name)
    client = drive_mod.DriveClient(
        creds=drive_mod.Credentials(client_id="c", client_secret="s",
                                    refresh_token="r", access_token="a",
                                    expires_at=9e9),
        transport=FakeDrive())
    reason = ["someone is using Ninaivu"]
    engine = engine_mod.SyncEngine(connect=lambda: client,
                                   open_db=lambda: db.connect(db_path),
                                   hold=lambda: reason[0])
    engine.start()
    deadline = time.time() + 5
    while time.time() < deadline and not engine.state.snapshot()["held_for"]:
        time.sleep(0.01)
    assert engine.state.snapshot()["held_for"] == "someone is using Ninaivu"
    assert store.summary(db.connect(db_path))["done"] == 0

    reason[0] = None
    engine._thread.join(10)
    assert store.summary(db.connect(db_path))["done"] == 2


def test_the_storage_check_waits_its_turn(scanned, monkeypatch):
    from ninaivu.api import admin_api

    monkeypatch.setattr(wl, "POLL_SECONDS", 0.01)
    cfg, _, _ = scanned
    gate = Gate()
    gate.reason = "saved for the night (23:00–06:00)"
    seen = []

    def release():
        deadline = time.time() + 5
        while time.time() < deadline and not admin_api._SCRUBBER_PROGRESS.get("held"):
            time.sleep(0.01)
        seen.append(admin_api._SCRUBBER_PROGRESS.get("held"))
        gate.reason = None

    threading.Thread(target=release).start()
    admin_api._run_scrubber(cfg.db_path, workload=gate)
    assert seen == ["saved for the night (23:00–06:00)"]
    assert admin_api._SCRUBBER_PROGRESS["processed"] == 15


# -- the apps tell it, and the console sets it --------------------------------------

@pytest.fixture()
def apps(scanned):
    from ninaivu import build_services, create_admin_app, create_home_app

    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    yield {"home": create_home_app(services), "admin": create_admin_app(services),
           "services": services}
    services.stop(timeout=5.0)


def test_the_family_app_counts_and_the_console_does_not(apps):
    work = apps["services"].workload
    apps["admin"].test_client().get("/api/assets")
    assert not work.browsing(), "an administrator is not a household"
    apps["home"].test_client().get("/api/file/1", headers={"Range": "bytes=0-99"})
    assert work.watching()


def test_the_console_chooses_the_mode(apps, people):
    from conftest import ADMIN, FAMILY, login

    # `people` made the accounts in the fixture's database, which both apps share.
    admin = login(apps["admin"].test_client(), *ADMIN)
    saved = admin.post("/api/admin/workload",
                       json={"mode": "overnight", "night_start": "22:30",
                             "night_end": "7:00"})
    assert saved.status_code == 200, saved.get_json()
    data = saved.get_json()
    assert data["mode"] == "overnight" and data["night_label"] == "22:30–07:00"
    cfg = apps["services"].cfg
    assert cfg.workload_mode == "overnight" and cfg.workload_night_end == "07:00"

    assert admin.post("/api/admin/workload", json={"mode": "turbo"}).status_code == 400
    assert admin.post("/api/admin/workload",
                      json={"night_start": "25:00"}).status_code == 400
    assert admin.post("/api/admin/workload",
                      json={"night_start": "06:00", "night_end": "06:00"}).status_code == 400

    family = login(apps["home"].test_client(), *FAMILY)
    assert family.post("/api/admin/workload",
                       json={"mode": "quiet"}).status_code in (401, 403, 404)


def test_overnight_at_night_boosts_the_upload_even_beside_other_work(apps, monkeypatch):
    services = apps["services"]
    services.cfg.workload_mode = "overnight"
    monkeypatch.setattr(services.workload, "is_night", lambda: True)
    from ninaivu.server import activity
    monkeypatch.setattr(activity, "other_work_active", lambda *a: True)
    assert services._cloud_idle()
    services.cfg.workload_mode = "balanced"
    assert not services._cloud_idle(), "balanced still waits for the other work"


# -- somebody outside the house --------------------------------------------------
#
# Away from home, every photograph somebody opens comes up the house's internet
# connection, the one the cloud backup fills. At full speed it filled all of it,
# and a phone on the tailnet waited seconds for each picture.

@pytest.mark.parametrize("address,outside", [
    ("100.93.190.76", True),                 # a phone on the tailnet
    ("::ffff:100.93.190.76", True),
    ("fd7a:115c:a1e0::4732:f933", True),     # the tailnet's IPv6
    ("24.125.189.136", True),                # the internet
    ("192.168.0.57", False),                 # the house
    ("10.0.0.8", False),
    ("fe80::1", False),
    ("127.0.0.1", False),                    # this computer
    ("", False),
])
def test_who_is_outside_the_house(address, outside):
    assert wl.from_outside(address, {}, own=frozenset()) is outside


def test_this_computer_is_never_outside_the_house():
    """The console opened here at the Tailscale address came from this Mac's
    own tailnet address, was read as a device away from home, and held the
    backup for as long as the tab was open."""
    assert wl.from_outside("100.115.249.50", {}, own=frozenset({"100.115.249.50"})) is False
    assert wl.from_outside("100.93.190.76", {}, own=frozenset({"100.115.249.50"})) is True
    assert "127.0.0.1" in wl.own_addresses()


def test_a_proxy_is_outside_unless_ninaivu_trusts_it_to_say_who():
    forwarded = {"X-Forwarded-For": "192.168.0.57"}
    assert wl.from_outside("127.0.0.1", forwarded) is True
    # Trusted: ProxyFix has already put the real address in remote_addr.
    assert wl.from_outside("192.168.0.57", forwarded, trusted_proxies=1) is False


@pytest.mark.parametrize("mode", ["balanced", "quiet", "overnight"])
def test_the_backup_makes_way_for_somebody_outside_in_every_mode(mode):
    work, clock = make(mode, at=local(23, 30))
    work.noticed_outside("/api/thumb/12")
    assert work.hold("upload") == "someone is using Ninaivu from outside the house"
    assert work.boost("upload") is False
    clock.now += wl.BROWSING_SECONDS + 1
    assert work.hold("upload") != "someone is using Ninaivu from outside the house"


def test_full_speed_still_makes_way_for_somebody_outside():
    work, _ = make("quiet")
    work.cfg.cloud_full_speed = True
    work.noticed("/api/assets")                     # somebody at home
    assert work.hold("upload") is None and work.boost("upload") is True
    work.noticed_outside("/api/thumb/1")            # somebody away
    assert work.hold("upload") == "someone is using Ninaivu from outside the house"
    assert work.boost("upload") is False


def test_only_the_upload_waits_for_somebody_outside():
    work, _ = make("balanced")
    work.noticed_outside("/api/file/3")
    assert work.hold("index") is None and work.hold("analysis") is None


def test_a_tab_left_open_away_from_home_does_not_hold_the_backup():
    """What a page asks for by itself, every few seconds, is a few hundred
    bytes; photographs and video are what share the connection."""
    work, _ = make("balanced")
    for path in ("/api/status", "/api/events", "/api/assets", "/api/admin/uploads",
                 "/api/cloud/status", "/api/activity"):
        work.noticed_outside(path)
    assert work.hold("upload") is None
    work.noticed_outside("/api/proxy/7")
    assert work.hold("upload") == "someone is using Ninaivu from outside the house"


def test_photographs_sent_to_a_tailnet_address_count(apps):
    work = apps["services"].workload
    home = apps["home"].test_client()
    home.get("/api/thumb/1", environ_base={"REMOTE_ADDR": "192.168.0.57"})
    assert not work.outside(), "somebody at home"
    apps["admin"].test_client().get("/api/admin/uploads",
                                    environ_base={"REMOTE_ADDR": "100.93.190.76"})
    assert not work.outside(), "the console refreshing itself is not somebody looking"
    home.get("/api/thumb/1", environ_base={"REMOTE_ADDR": "100.93.190.76"})
    assert work.outside(), "photographs away from home use the same connection"
