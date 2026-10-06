"""Regression tests for the gap review of 4 October 2026.

Each test names the defect it pins down. Findings with an obvious home of
their own are tested there (tests/test_reroot.py, tests/test_db_maintenance.py);
what is here has no older file to sit in.
"""

from pathlib import Path

import pytest

from conftest import ADMIN, ids_of, login
from ninaivu.storage import recycle


@pytest.fixture()
def admin(app, people):
    client = login(app.test_client(), *ADMIN)
    client.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    return client


def _delete_one(admin, conn):
    target = ids_of(admin)[0]
    row = dict(conn.execute("SELECT * FROM assets WHERE id=?", (target,)).fetchone())
    response = admin.post("/api/delete", json={"ids": [target], "password": ADMIN[1]})
    assert response.status_code == 200, response.get_json()
    entry = conn.execute("SELECT id FROM recycled WHERE asset_id=? AND restored_at IS NULL",
                         (target,)).fetchone()[0]
    return row, entry


def _new_photo_at(conn, row):
    """A different photograph saved where the deleted one was, and indexed:
    the same path, so the same thumbnail name."""
    original = Path(row["root"]) / row["rel_path"]
    original.write_bytes(b"a different photograph")
    record = {k: row[k] for k in ("root", "rel_path", "filename", "kind", "thumb")}
    cols = ",".join(record)
    conn.execute(f"INSERT INTO assets({cols}) VALUES ({','.join('?' * len(record))})",
                 list(record.values()))
    conn.commit()
    return original


# -- emptying the bin erased the thumbnails of a photo now at the same path ----

def test_purging_keeps_thumbnails_a_live_photo_now_uses(admin, people):
    conn = people["conn"]
    row, entry = _delete_one(admin, conn)
    assert row["thumb"]
    _new_photo_at(conn, row)

    result = recycle.purge(conn, [entry])

    assert result["purged"] == 1
    assert row["thumb"] not in result["thumbs"], (
        "the new photo's thumbnails would be erased with the old one")


def test_purging_still_hands_back_thumbnails_nothing_uses(admin, people):
    conn = people["conn"]
    row, entry = _delete_one(admin, conn)
    assert recycle.purge(conn, [entry])["thumbs"] == [row["thumb"]]


# -- restored under a new name, it showed the other photo's thumbnails ---------

def test_a_photo_restored_under_a_new_name_gets_its_own_thumbnails(admin, people):
    conn = people["conn"]
    row, entry = _delete_one(admin, conn)
    _new_photo_at(conn, row)

    assert recycle.restore(conn, [entry])["restored"] == 1

    back = conn.execute("SELECT rel_path, thumb, mtime FROM assets "
                        "WHERE root=? AND rel_path != ? AND filename LIKE ?",
                        (row["root"], row["rel_path"],
                         Path(row["filename"]).stem + "%")).fetchone()
    assert back is not None, "it was not restored beside the new photo"
    assert back["thumb"] is None, "it points at the other photo's thumbnails"
    assert not back["mtime"], "the next scan would take it as already indexed"


# -- the storage check's alerts never left the building -----------------------

class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, event, summary, detail=""):
        self.sent.append(event)


def _wait_for_the_check(admin_api):
    import time
    deadline = time.time() + 30
    while admin_api._SCRUBBER_RUNNING and time.time() < deadline:
        time.sleep(0.05)
    assert not admin_api._SCRUBBER_RUNNING


def _start_up_finished(app):
    """Start-up scans the library from a thread of its own, after the app
    fixture has stopped the scanner. On a slow runner that scan ran during
    the test and removed the lost file's record before the check read the
    library, so there was nothing missing to report."""
    services = app.config["MV_SERVICES"]
    services.boot_thread.join(timeout=60)
    scanning = services.scanner._thread
    if scanning is not None:
        scanning.join(timeout=60)


def _lose_a_file(conn):
    row = conn.execute("SELECT root, rel_path FROM assets ORDER BY id LIMIT 1").fetchone()
    (Path(row["root"]) / row["rel_path"]).unlink()


def test_a_storage_check_started_from_the_console_sends_its_alert(app, scanned):
    """The check runs on a thread with no app context, where the notifier
    could not read the settings: the alert was logged and never sent."""
    from ninaivu.api import admin_api

    cfg, conn, _ = scanned
    _start_up_finished(app)
    _lose_a_file(conn)
    recorder = _Recorder()
    app.config["MV_NOTIFY"] = recorder
    with app.app_context():
        assert admin_api.start_scrubber_job(cfg.db_path)
    _wait_for_the_check(admin_api)
    assert "missing" in recorder.sent


def test_a_storage_check_resumed_at_start_up_uses_the_notifier_it_is_given(scanned):
    from ninaivu.api import admin_api

    cfg, conn, _ = scanned
    _lose_a_file(conn)
    sent = []
    assert admin_api.start_scrubber_job(
        cfg.db_path, notify=lambda event, summary, detail="": sent.append(event))
    _wait_for_the_check(admin_api)
    assert "missing" in sent


# -- the Drive folder name setting was never read -----------------------------

def test_a_new_cloud_connection_uses_the_configured_folder_name(cfg):
    from ninaivu.cloud import service as cloud_service
    from ninaivu.storage import db

    db.init_db(cfg.db_path).close()
    cfg.cloud_folder_name = "Family photos"
    service = cloud_service.CloudService(cfg, lambda: db.connect(cfg.db_path))
    assert service.creds.folder_name == "Family photos"


def test_the_advanced_page_sends_a_folder_rename_to_mugil(cfg):
    """Saved there, the name changed in config.json while the uploads went on
    into the folder whose id was remembered."""
    from ninaivu.server import settings_groups

    with pytest.raises(settings_groups.BadValue, match="Mugil"):
        settings_groups.apply(cfg, {"cloud_folder_name": "Elsewhere"})


# -- malformed requests answered 500 ------------------------------------------

@pytest.mark.parametrize("url, body", [
    ("/api/faces/confirm", {"face_id": "abc", "person_id": 1}),
    ("/api/faces/reject", {"face_id": 1, "person_id": "x"}),
    ("/api/faces/merge", {"source_id": "one", "target_id": 2}),
    ("/api/straighten/undo", {"batch": "latest"}),
    ("/api/admin/notifications", {"events": 5}),
    ("/api/admin/notifications", {"webhook": "file:///etc/passwd"}),
    ("/api/admin/notifications", {"webhook_format": "carrier-pigeon"}),
])
def test_a_malformed_body_is_a_400(admin, url, body):
    response = admin.post(url, json=body)
    assert response.status_code == 400, (url, body, response.status_code)


def test_an_infinite_size_floor_is_not_a_500(admin):
    assert admin.get("/api/admin/large-files?min_mb=inf").status_code < 500


# -- a long Tamil file name could not be written at all -----------------------

def test_a_long_tamil_name_fits_in_255_bytes():
    from ninaivu.utils.filenames import MAX_LENGTH, safe_filename

    name = safe_filename("நினைவு" * 80 + ".jpg")
    assert name.endswith(".jpg")
    assert len(name.encode("utf-8")) <= MAX_LENGTH <= 255
    assert name.startswith("நினைவு")


def test_a_long_english_name_is_cut_as_before():
    from ninaivu.utils.filenames import MAX_LENGTH, safe_filename

    name = safe_filename("a" * 300 + ".jpeg")
    assert name == "a" * (MAX_LENGTH - 5) + ".jpeg"


# -- dates read in UTC rather than the camera's local time --------------------

def _living_in(monkeypatch, zone):
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset")
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture()
def in_india(monkeypatch):
    yield from _living_in(monkeypatch, "Asia/Kolkata")


@pytest.fixture()
def in_california(monkeypatch):
    yield from _living_in(monkeypatch, "America/Los_Angeles")


def test_an_occasion_taken_after_midnight_is_titled_its_own_day(in_india):
    from datetime import datetime

    from ninaivu.archive.dates import to_timestamp
    from ninaivu.media import occasions

    early = to_timestamp(datetime(2019, 1, 5, 2, 0))
    assert occasions.date_range(early, early) == "5 January 2019"


def _dated(conn, rel_path, *, date_key, captured_at, mtime=0):
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, date_key, captured_at, mtime, "
        "thumb, visibility, indexed_at) VALUES('/lib', ?, ?, 'picture', ?, ?, ?, ?, 1, 0)",
        (rel_path, rel_path, date_key, captured_at, mtime, "ab/" + rel_path))


def test_on_this_day_follows_the_local_date_and_leaves_undated_files_out(in_india, tmp_path):
    """A photo taken at 2 a.m. in India on 5 January was a memory of the 4th
    as well (its UTC date), and an undated file was matched by its mtime,
    which the scanner deliberately does not take as a capture date."""
    from datetime import datetime

    from ninaivu.archive.dates import to_timestamp
    from ninaivu.server import auth
    from ninaivu.storage import db

    conn = db.init_db(tmp_path / "index.db")
    auth.init_auth_schema(conn)
    _dated(conn, "early.jpg", date_key="2019-01-05",
           captured_at=to_timestamp(datetime(2019, 1, 5, 2, 0)))
    _dated(conn, "undated.jpg", date_key="", captured_at=None,
           mtime=to_timestamp(datetime(2019, 1, 4, 12, 0)))
    conn.commit()

    def names(month, day):
        found = db.query_on_this_day(conn, ["/lib"], month=month, day=day, max_visibility=2)
        picked = db.candidates_for_the_day(conn, ["/lib"], month=month, day=day,
                                           max_visibility=2)
        return ({i["rel_path"] for i in found["items"]}, {i["rel_path"] for i in picked})

    assert names(1, 5) == ({"early.jpg"}, {"early.jpg"})
    assert names(1, 4) == (set(), set())


def test_a_date_set_by_hand_is_that_day_west_of_greenwich(in_california):
    """It was stored as midnight UTC: the evening before in California."""
    from ninaivu.archive.dates import from_timestamp, to_timestamp
    from ninaivu.media import date_edit

    when = date_edit.parse_date("2019-01-05")
    assert from_timestamp(to_timestamp(when)).date().isoformat() == "2019-01-05"


# -- cloud restore: names with a colon or glob characters ----------------------

@pytest.mark.skipif(__import__("os").name == "nt", reason="a colon is replaced on Windows")
def test_a_name_with_a_colon_is_restored_not_skipped():
    from ninaivu.cloud import restore

    rel = "2019/Screenshot 2019-07-01 at 10:30:00.png"
    assert restore._clean_rel(rel) == rel


def test_a_colon_cannot_name_a_drive_on_windows(monkeypatch):
    from ninaivu.cloud import restore

    monkeypatch.setattr(restore.os, "name", "nt")
    assert restore._clean_rel("C:/Windows/x.jpg") == "C_/Windows/x.jpg"
    assert restore._clean_rel("a/b:stream") == "a/b_stream"
    assert restore._clean_rel("../x") is None


def test_an_earlier_restore_beside_a_bracketed_name_is_found(tmp_path):
    from ninaivu.cloud import restore

    target = tmp_path / "IMG [1].jpg"
    earlier = tmp_path / "IMG [1] (restored).jpg"
    earlier.write_bytes(b"x")
    assert restore._earlier_beside(target) == [earlier]


# -- the archive: two workers, one name on a case-insensitive disk -------------

def test_two_names_differing_only_in_case_are_not_both_reserved(tmp_path):
    """On NTFS, exFAT or APFS IMG_1.JPG and img_1.jpg are one file: two workers
    each reserving "their" name meant the second rename replaced the first."""
    import threading
    from types import SimpleNamespace

    from ninaivu.archive.scanner import ArchiveJob

    job = object.__new__(ArchiveJob)
    job.gate = SimpleNamespace(check=lambda: None)
    job._decide_lock = threading.Lock()
    job._claimed = set()

    first, _ = job._unique_path(str(tmp_path), "IMG_1.JPG", "aa")
    second, _ = job._unique_path(str(tmp_path), "img_1.jpg", "bb")

    assert first.casefold() != second.casefold()
    job._unclaim(first)
    job._unclaim(second)
    assert job._claimed == set()


# -- guests could list and filter by the household's cameras -------------------

def _give_everything_a_camera(people):
    conn = people["conn"]
    conn.execute("UPDATE assets SET camera='Pixel 9', visibility=0")
    conn.commit()


@pytest.mark.parametrize("who", ["as_guest", "anon"])
def test_a_guest_sees_no_cameras(request, people, who):
    _give_everything_a_camera(people)
    client = request.getfixturevalue(who)
    facets = client.get("/api/facets")
    assert facets.status_code == 200
    assert facets.get_json()["cameras"] == []
    suggested = client.get("/api/suggest?q=pixel").get_json()["suggestions"]
    assert not [s for s in suggested if s["type"] == "camera"]


def test_the_family_still_sees_cameras(as_family, people):
    _give_everything_a_camera(people)
    names = [c["name"] for c in as_family.get("/api/facets").get_json()["cameras"]]
    assert "Pixel 9" in names


# -- the Advanced page skipped what the dedicated pages do --------------------

def test_the_advanced_page_keeps_uploads_at_six_at_once(cfg):
    from ninaivu.server import settings_groups

    with pytest.raises(settings_groups.BadValue):
        settings_groups.apply(cfg, {"cloud_parallel": 500})
    assert settings_groups.apply(cfg, {"cloud_parallel": 6}) == ["cloud_parallel"]


def test_opening_the_console_from_the_advanced_page_asks_for_a_restart(admin):
    response = admin.post("/api/admin/settings/all", json={"console_on_network": True})
    assert response.status_code == 200, response.get_json()
    assert "console_on_network" in response.get_json()["restart"]


# -- removing a library by another spelling left its rows in the index ---------

def test_removing_a_library_by_another_spelling_clears_its_rows(admin, people, cfg):
    conn, library = people["conn"], cfg.roots[0]
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE root=?", (str(library),)).fetchone()[0]
    response = admin.delete("/api/admin/libraries", query_string={
        "path": str(library) + "/", "force": "1"})
    assert response.status_code == 200, response.get_json()
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE root=?",
                        (str(library),)).fetchone()[0] == 0


def test_a_scan_starting_during_a_storage_check_is_not_locked_out(scanned):
    """The check wrote each file's row and committed about once a second, so
    between files it held an open transaction without the write lock. A scan
    that started then took the lock and waited on SQLite for that transaction,
    while the check waited for the lock: after 30 seconds the scan failed with
    "database is locked"."""
    import threading
    import time

    from ninaivu.api import admin_api
    from ninaivu.storage import db

    cfg, _, _ = scanned
    took = []

    class _ScanStartsNow:
        files = 0

        def wait_turn(self, job, on_hold=None):
            self.files += 1
            if self.files != 2:                # after the first file's row
                return True

            def scan():
                started = time.monotonic()
                db.start_scan_run(db.connect(cfg.db_path), "elsewhere")
                took.append(time.monotonic() - started)
                db.close_all()

            thread = threading.Thread(target=scan, daemon=True)
            thread.start()
            thread.join(timeout=5)
            return True

    admin_api._SCRUBBER_RUNNING = True        # what start_scrubber_job sets
    assert admin_api._run_scrubber(cfg.db_path, workload=_ScanStartsNow())
    assert took and took[0] < 5, "the scan waited on the check's open transaction"
