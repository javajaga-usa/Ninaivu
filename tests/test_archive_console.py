"""The Archive tab as part of the console.

The engine's own guarantees are covered by ``test_archive_engine.py``. What is
tested here is everything that only exists because the tool now lives inside
Ninaivu: who may reach it, that the family port cannot, where its state lives,
that it stands the library indexer down while it works, and that a finished
archive becomes a library folder only when somebody asks for it.
"""

import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu import archive, build_services, create_admin_app, create_home_app
from ninaivu.server import auth
from ninaivu.api import archive_api
from ninaivu.archive import database as adb


ARCHIVE_ROUTES = [
    ("get", "/api/archive/status"),
    ("get", "/api/archive/version"),
    ("get", "/api/archive/settings"),
    ("get", "/api/archive/recent"),
    ("get", "/api/archive/years"),
    ("get", "/api/archive/logs"),
    ("get", "/api/archive/recovery.json"),
    ("post", "/api/archive/validate"),
    ("post", "/api/archive/capacity"),
    ("post", "/api/archive/course-folders/decision"),
    ("post", "/api/archive/start"),
    ("post", "/api/archive/stop"),
    ("post", "/api/archive/retry-errors"),
    ("post", "/api/archive/reset"),
    ("post", "/api/archive/guardian/run"),
    ("post", "/api/archive/adopt"),
    ("get", "/api/archive/takeout-albums"),
    ("post", "/api/archive/takeout-albums"),
]


@pytest.fixture()
def console(scanned):
    """The admin console, signed in as an administrator."""
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    return login(app.test_client(), *ADMIN), cfg, services


@pytest.fixture()
def drives(tmp_path):
    """Two "old drives" with dated photos, one of them a byte-identical twin."""
    import shutil
    from datetime import datetime, timedelta

    def shot(path, when, seed):
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (120, 90), ((seed * 37) % 256, 90, (seed * 13) % 256))
        exif = img.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        img.save(path, "JPEG", exif=exif)

    card = tmp_path / "cardA"
    for i in range(4):
        shot(card / "DCIM" / f"IMG_{i}.jpg",
             datetime(2016, 2, 3) + timedelta(days=i * 400), i + 1)
    shutil.copy(card / "DCIM" / "IMG_0.jpg", card / "again.jpg")   # exact twin
    (card / "notes.txt").write_text("not media")
    return card


# ---------------------------------------------------------------------------
# Who can reach it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", ARCHIVE_ROUTES)
def test_the_family_app_does_not_route_the_archive_at_all(scanned, method, path):
    """Port 5000 gives a 404 — the routes are absent, not merely forbidden.

    Consolidating drives writes gigabytes and can enumerate every disk on the
    machine. That is not something the family app should be able to refuse; it
    is something it should not know about.
    """
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    home = login(create_home_app(services).test_client(), *ADMIN)

    response = getattr(home, method)(path, json={})
    assert response.status_code == 404, f"{method.upper()} {path}"


@pytest.mark.parametrize("who", ["family", "guest"])
@pytest.mark.parametrize("method,path", ARCHIVE_ROUTES)
def test_only_an_admin_may_use_the_archive(app, people, who, method, path):
    """On the console's own blueprint, a non-admin is refused."""
    client = login(app.test_client(), *(FAMILY if who == "family" else GUEST))
    response = getattr(client, method)(path, json={})
    assert response.status_code == 403, f"{who} reached {method.upper()} {path}"


def test_an_anonymous_visitor_is_refused(anon):
    assert anon.get("/api/archive/status").status_code in (401, 403)


def test_an_admin_can_reach_every_read_endpoint(console):
    client, _, _ = console
    for method, path in ARCHIVE_ROUTES:
        if method != "get":
            continue
        assert client.get(path).status_code == 200, path


def test_archive_page_exposes_health_and_recovery_controls(console):
    client, _, _ = console
    html = client.get('/').get_data(as_text=True)
    for element_id in ('ar-health', 'ar-health-run', 'ar-drive-banner',
                       'ar-resource', 'ar-recovery'):
        assert f'id="{element_id}"' in html


# ---------------------------------------------------------------------------
# Where its state lives
# ---------------------------------------------------------------------------

def test_the_archive_database_lives_in_ninaivus_state_directory(console):
    _, cfg, _ = console
    assert Path(archive.state_paths()["db"]) == Path(cfg.state_dir) / "archive.db"
    assert (Path(cfg.state_dir) / "archive.db").is_file()


def test_the_version_endpoint_names_the_engine_and_its_host(console):
    client, _, _ = console
    payload = client.get("/api/archive/version").get_json()
    assert payload["version"] == archive.VERSION
    assert payload["build"] == archive.BUILD
    assert payload["hosted_by"] == "Ninaivu"


def test_the_two_indexes_are_separate_files(console):
    """The library index and the archive record must not share a database.

    They answer different questions and have different lifetimes: clearing the
    archive report must never disturb the library, and re-indexing the library
    must never lose the record of what has already been copied.
    """
    _, cfg, _ = console
    assert Path(archive.state_paths()["db"]) != Path(cfg.db_path)


# ---------------------------------------------------------------------------
# Planning a job
# ---------------------------------------------------------------------------

def test_an_archive_inside_a_source_is_allowed_but_flagged(console, drives):
    """Source ``C:\\`` with destination ``C:\\Master`` is the common case.

    It is allowed — the walk steps around the archive rather than reading its
    own output back in — but it is unusual enough to say so out loud.
    """
    client, _, _ = console
    result = client.post("/api/archive/validate", json={
        "source_dirs": [str(drives)],
        "destination_dir": str(drives / "Master"),
    }).get_json()
    assert result["ok"] is True, result["problems"]
    assert result["notices"], "the page must explain what will happen"


def test_a_source_entirely_inside_the_archive_is_refused(console, drives, tmp_path):
    """Nothing would be left to scan, so this one is a real refusal."""
    client, _, _ = console
    dest = tmp_path / "Master"
    (dest / "2016").mkdir(parents=True)
    result = client.post("/api/archive/validate", json={
        "source_dirs": [str(dest / "2016")],
        "destination_dir": str(dest),
    }).get_json()
    assert result["ok"] is False
    assert result["problems"]


def test_validate_accepts_an_ordinary_job(console, drives, tmp_path):
    client, _, _ = console
    result = client.post("/api/archive/validate", json={
        "source_dirs": [str(drives)],
        "destination_dir": str(tmp_path / "Master"),
    }).get_json()
    assert result["ok"] is True, result["problems"]


def test_capacity_estimates_before_anything_is_written(console, drives, tmp_path):
    client, _, _ = console
    dest = tmp_path / "Master"
    result = client.post("/api/archive/capacity", json={
        "source_dirs": [str(drives)],
        "destination_dir": str(dest),
    }).get_json()
    assert result["ok"] is True
    assert result["files"] >= 5
    assert result["bytes"] > 0
    assert "free" in result and "fits" in result
    assert not dest.exists(), "an estimate must not create the destination"


def test_starting_with_no_media_types_is_refused(console, drives, tmp_path):
    client, _, _ = console
    response = client.post("/api/archive/start", json={
        "source_dirs": [str(drives)],
        "destination_dir": str(tmp_path / "Master"),
        "media_types": [],
    })
    assert response.status_code == 409
    assert "at least one" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------

def run_and_wait(client, sources, dest, mode="copy", timeout=60):
    response = client.post("/api/archive/start", json={
        "source_dirs": [str(s) for s in sources],
        "destination_dir": str(dest),
        "mode": mode,
    })
    assert response.status_code == 200, response.get_json()

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get("/api/archive/status").get_json()
        if not status["is_scanning"] and status["phase"] in ("done", "error", "idle"):
            if status["verified"] or status["planned"] or status["phase"] == "done":
                return status
        time.sleep(0.1)
    raise AssertionError("the run never finished")


def test_a_real_run_archives_verifies_and_dedupes(console, drives, tmp_path):
    client, _, _ = console
    dest = tmp_path / "Master"
    status = run_and_wait(client, [drives], dest)

    assert status["verified"] == 4, status
    assert status["duplicates"] == 1, "the byte-identical twin is caught"
    assert status["errors"] == 0

    # Filed by capture date, not by the date of the run.
    years = {p.name for p in dest.iterdir() if p.is_dir()}
    assert years == {"2016", "2017", "2018", "2019"}, years


def test_the_sources_are_left_exactly_as_they_were(console, drives, tmp_path):
    before = {p: p.stat().st_size for p in sorted(drives.rglob("*")) if p.is_file()}
    client, _, _ = console
    run_and_wait(client, [drives], tmp_path / "Master")
    after = {p: p.stat().st_size for p in sorted(drives.rglob("*")) if p.is_file()}
    assert after == before, "nothing is moved, changed or deleted at the source"


def test_a_dry_run_writes_nothing(console, drives, tmp_path):
    client, _, _ = console
    dest = tmp_path / "Master"
    status = run_and_wait(client, [drives], dest, mode="dry-run")

    assert status["planned"] > 0
    assert status["verified"] == 0
    archived = [p for p in dest.rglob("*") if p.is_file() and p.name != ".photo-archive-root"] \
        if dest.exists() else []
    assert not archived, archived


def test_the_manifest_exports_both_hashes(console, drives, tmp_path):
    client, _, _ = console
    run_and_wait(client, [drives], tmp_path / "Master")

    response = client.get("/api/archive/manifest.csv")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    header, *rows = [line for line in body.splitlines() if line]
    assert "source_sha256" in header and "archived_sha256" in header
    assert len(rows) >= 5


def test_the_run_is_written_to_the_activity_log(console, drives, tmp_path):
    client, _, _ = console
    run_and_wait(client, [drives], tmp_path / "Master")
    actions = [e["action"] for e in client.get("/api/audit").get_json()["entries"]]
    assert "archive_copy" in actions


def test_reset_clears_the_report_but_not_the_archive(console, drives, tmp_path):
    client, _, _ = console
    dest = tmp_path / "Master"
    run_and_wait(client, [drives], dest)
    on_disk = sorted(p.name for p in dest.rglob("*.jpg"))

    assert client.post("/api/archive/reset").status_code == 200
    assert client.get("/api/archive/status").get_json()["verified"] == 0
    assert sorted(p.name for p in dest.rglob("*.jpg")) == on_disk


# ---------------------------------------------------------------------------
# Sharing the disk with the library indexer
# ---------------------------------------------------------------------------

def test_the_indexer_stands_down_for_a_run_and_comes_back(console, drives, tmp_path):
    client, _, services = console
    scanner = services.scanner
    assert scanner.deferred is None

    run_and_wait(client, [drives], tmp_path / "Master")

    # The watcher restores it just after the job thread ends.
    deadline = time.time() + 10
    while scanner.deferred is not None and time.time() < deadline:
        time.sleep(0.1)
    assert scanner.deferred is None, "the indexer must be released"


def test_archive_temporarily_requests_performance_mode(console, drives, tmp_path):
    client, _, _ = console

    class RecordingPower:
        def __init__(self):
            self.events = []

        def archive_started(self):
            self.events.append("performance")

        def archive_finished(self):
            self.events.append("efficient")

        def snapshot(self):
            active = self.events[-1:] == ["performance"]
            return {"mode": "performance" if active else "efficient",
                    "managed": True, "archive_active": active}

    power = RecordingPower()
    client.application.config["MV_POWER"] = power
    run_and_wait(client, [drives], tmp_path / "Master")

    deadline = time.time() + 2
    while power.events[-1:] != ["efficient"] and time.time() < deadline:
        time.sleep(0.02)
    assert power.events == ["performance", "efficient"]


def test_a_deferred_indexer_queues_a_scan_instead_of_losing_it(console):
    """Asking for a rescan mid-consolidation must not silently do nothing."""
    _, cfg, services = console
    scanner = services.scanner

    scanner.defer("a consolidation is running")
    scanner.start()                            # the admin presses Rescan
    assert not scanner.running, "it must not fight the archive for the disk"
    assert scanner.progress.snapshot()["status"] == "paused"

    started = scanner.resume()
    assert started, "the queued scan runs once the disk is free"
    scanner.stop(join=True)


def test_resume_with_nothing_queued_does_nothing(console):
    _, _, services = console
    scanner = services.scanner
    scanner.defer("a consolidation is running")
    assert scanner.resume() is False
    assert scanner.deferred is None


def test_the_status_payload_reports_the_indexer(console):
    client, _, services = console
    services.scanner.defer("a consolidation is running")
    try:
        payload = client.get("/api/archive/status").get_json()
        assert payload["indexer"]["deferred"] == "a consolidation is running"
    finally:
        services.scanner.resume()


# ---------------------------------------------------------------------------
# Handing the archive to the library
# ---------------------------------------------------------------------------

def test_nothing_is_added_to_the_library_on_its_own(console, drives, tmp_path):
    client, cfg, _ = console
    dest = tmp_path / "Master"
    before = list(cfg.libraries)
    run_and_wait(client, [drives], dest)

    assert list(cfg.libraries) == before, "a finished run is an offer, not an act"
    handoff = client.get("/api/archive/status").get_json()["handoff"]
    assert handoff["available"] is True
    assert handoff["in_library"] is False
    assert handoff["verified"] == 4


def test_one_call_adopts_the_finished_archive(console, drives, tmp_path):
    client, cfg, _ = console
    dest = tmp_path / "Master"
    run_and_wait(client, [drives], dest)

    response = client.post("/api/archive/adopt", json={})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["already"] is False
    assert str(dest) in [str(Path(p)) for p in cfg.libraries]

    handoff = client.get("/api/archive/status").get_json()["handoff"]
    assert handoff["in_library"] is True


def test_adopting_twice_is_harmless(console, drives, tmp_path):
    client, cfg, _ = console
    dest = tmp_path / "Master"
    run_and_wait(client, [drives], dest)

    client.post("/api/archive/adopt", json={})
    again = client.post("/api/archive/adopt", json={})
    assert again.status_code == 200
    assert again.get_json()["already"] is True
    assert len(cfg.libraries) == len({str(Path(p)) for p in cfg.libraries})


def test_adopting_is_recorded_in_the_activity_log(console, drives, tmp_path):
    client, _, _ = console
    run_and_wait(client, [drives], tmp_path / "Master")
    client.post("/api/archive/adopt", json={})
    actions = [e["action"] for e in client.get("/api/audit").get_json()["entries"]]
    assert "archive_adopt" in actions


def test_adopting_a_folder_that_is_gone_says_so(console, drives, tmp_path):
    client, _, _ = console
    response = client.post("/api/archive/adopt", json={"path": str(tmp_path / "nope")})
    assert response.status_code == 400
    assert "not there any more" in response.get_json()["error"]


def test_adopting_a_system_folder_is_refused(console):
    client, _, _ = console
    response = client.post("/api/archive/adopt", json={"path": "/etc"})
    assert response.status_code == 403
    assert "system folder" in response.get_json()["error"]


def test_adopting_before_any_run_is_refused(console):
    client, _, _ = console
    adb.clear_db()
    response = client.post("/api/archive/adopt", json={})
    assert response.status_code == 400
    assert "no finished archive" in response.get_json()["error"].lower()


def test_an_adopted_archive_becomes_browsable_media(console, drives, tmp_path):
    """The whole point of the hand-off: the photos show up in the gallery."""
    client, cfg, services = console
    dest = tmp_path / "Master"
    run_and_wait(client, [drives], dest)
    client.post("/api/archive/adopt", json={})

    # The adopt call queues or starts the scan; let it finish.
    deadline = time.time() + 30
    while time.time() < deadline:
        if not services.scanner.running and services.scanner.deferred is None:
            break
        time.sleep(0.2)
    services.scanner.stop(join=True)

    from ninaivu.storage import db as index
    conn = index.connect(cfg.db_path)
    indexed = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE root=? AND trashed=0", (str(dest),)
    ).fetchone()["n"]
    assert indexed == 4, "the archive's photos are now in the library"


@pytest.mark.parametrize("reading", [(True, 42), (False, 100), (None, None)])
def test_the_status_says_whether_the_computer_is_on_battery(console, monkeypatch, reading):
    """Said before a job starts, so a large archive is not begun unplugged and
    left to run many times slower without anyone knowing why."""
    client, _, _ = console
    monkeypatch.setattr("ninaivu.archive.pacing.read_battery", lambda: reading)
    # The reading is kept for a few seconds between status ticks; this test
    # wants the fresh one.
    monkeypatch.setattr(archive_api, "_battery_cache", {"at": 0.0, "value": None})
    battery = client.get("/api/archive/status").get_json()["battery"]
    assert battery == {"on_battery": reading[0], "percent": reading[1]}


def test_a_status_tick_counts_the_archive_once_and_asks_the_battery_rarely(
        console, monkeypatch):
    """Every open console tab asks for the status once a second. Each tick
    counted the whole archive twice and ran the battery query every time, which
    on Windows and macOS is a system call or a child process."""
    client, _, _ = console
    counted, asked = [], []
    real_stats = adb.get_stats
    monkeypatch.setattr(adb, "get_stats", lambda: counted.append(1) or real_stats())
    monkeypatch.setattr("ninaivu.archive.pacing.read_battery",
                        lambda: asked.append(1) or (True, 50))
    monkeypatch.setattr(archive_api, "_battery_cache", {"at": 0.0, "value": None})

    first = client.get("/api/archive/status").get_json()
    assert counted == [1], "the archive was counted more than once in a tick"
    assert first["battery"] == {"on_battery": True, "percent": 50}
    assert first["handoff"]["verified"] == first["verified"]
    second = client.get("/api/archive/status").get_json()
    assert second["battery"] == first["battery"]
    assert asked == [1], "the battery was read again within the same few seconds"


def test_the_run_panel_has_somewhere_to_say_so(console):
    client, _, _ = console
    body = client.get("/").get_data(as_text=True)
    assert 'id="ar-battery-banner"' in body and 'id="ar-battery-text"' in body


# ---------------------------------------------------------------------------
# From Ninaivu Lite: a suggested destination, and refusals in the family's
# language
# ---------------------------------------------------------------------------

def test_before_the_first_job_the_archive_is_suggested_inside_the_library(console):
    client, cfg, _ = console
    saved = client.get("/api/archive/settings").get_json()
    assert saved["destination_is_default"] is True
    assert Path(saved["destination_dir"]) == Path(cfg.libraries[0]) / "Ninaivu Archive"
    adb.save_settings([], "/somewhere/chosen", ["image"])
    saved = client.get("/api/archive/settings").get_json()
    assert saved["destination_is_default"] is False
    assert saved["destination_dir"] == "/somewhere/chosen"


def test_an_archive_inside_the_library_counts_as_in_the_library(console, drives):
    client, cfg, _ = console
    before = list(cfg.libraries)
    dest = Path(cfg.libraries[0]) / "Ninaivu Archive"
    run_and_wait(client, [drives], dest)

    handoff = client.get("/api/archive/status").get_json()["handoff"]
    assert handoff["in_library"] is True
    response = client.post("/api/archive/adopt", json={})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["already"] is True
    assert list(cfg.libraries) == before, "not added a second time, nested"


def test_refusals_carry_the_sentence_to_translate(console, drives, tmp_path):
    client, _, _ = console
    dest = tmp_path / "Master"
    (dest / "2016").mkdir(parents=True)
    result = client.post("/api/archive/validate", json={
        "source_dirs": [str(dest / "2016"), "relative/folder"],
        "destination_dir": str(dest),
    }).get_json()
    assert len(result["problem_keys"]) == len(result["problems"])
    keys = {k["key"] for k in result["problem_keys"] if k}
    assert "Give the full path of the source folder, not “{path}”." in keys
    inside = next(k for k in result["problem_keys"]
                  if k and k["key"].startswith("Source “{path}” is inside the destination"))
    assert inside["params"] == {"path": str(dest / "2016"), "destination": str(dest)}
