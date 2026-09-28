"""Is everything safe? One answer from every protection, and honest about each.

Each case sets one protection into a known state on a real set of services and
checks the answer the Overview would show: what it says, how serious it calls
it, and that one check failing to answer does not take the rest with it.
"""

import time
from types import SimpleNamespace

import pytest

from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import store
from ninaivu.server import safety
from ninaivu.storage import db


@pytest.fixture()
def services(app, people):
    return app.config["MV_SERVICES"]


def check(services, check_id):
    report = services.safety.report(fresh=True)
    return next(c for c in report["checks"] if c["id"] == check_id), report


def connect(services, done_share=1.0):
    services.cfg.cloud_enabled = True
    services.cloud.creds = drive_mod.Credentials(
        client_id="c", client_secret="s", refresh_token="r", access_token="a",
        expires_at=9e9)
    conn = db.connect(services.cfg.db_path)
    store.init_schema(conn)
    rows = conn.execute("SELECT root, rel_path, size FROM assets WHERE trashed=0 "
                        "AND visibility < 2").fetchall()
    for n, row in enumerate(rows):
        conn.execute("INSERT OR IGNORE INTO cloud_uploads(root, rel_path, size, state, "
                     "remote_id) VALUES(?,?,?,?,?)",
                     (row["root"], row["rel_path"], row["size"],
                      store.DONE if n < len(rows) * done_share else store.PENDING,
                      f"id{n}"))
    conn.commit()


# -- the copy outside the house ----------------------------------------------------

def test_no_cloud_copy_is_a_problem_and_says_why(services):
    found, report = check(services, "cloud")
    assert found["status"] == safety.PROBLEM
    assert "no copy of the library outside this house" in found["summary"]
    assert report["verdict"] == safety.PROBLEM


def test_a_complete_cloud_copy_is_ok(services):
    connect(services)
    found, _ = check(services, "cloud")
    assert found["status"] == safety.OK and "(100%)" in found["summary"]


def test_a_cloud_copy_still_going_up_is_worth_a_look(services):
    connect(services, done_share=0.5)
    found, _ = check(services, "cloud")
    assert found["status"] == safety.ATTENTION


def test_a_refused_google_permission_is_a_problem(services):
    connect(services)
    services.cloud._engine = SimpleNamespace(
        running=False, state=SimpleNamespace(snapshot=lambda: {"needs_reconnect": True}))
    assert check(services, "cloud")[0]["status"] == safety.PROBLEM


# -- the restore tests ------------------------------------------------------------------

def record_test(services, status, age_days=0):
    services.restore_tests._record({
        "status": status, "summary": "All 5 files came back intact.", "files": 5,
        "bytes": 1, "restored": 5, "failed": 0, "matched": 5, "detail": [],
        "started_at": time.time() - age_days * 86400, "ended_at": time.time()}, True)


def test_restore_tests_are_off_without_a_cloud_copy(services):
    assert check(services, "restore_test")[0]["status"] == safety.OFF


def test_never_tested_passed_failed_and_overdue(services):
    connect(services)
    assert check(services, "restore_test")[0]["status"] == safety.ATTENTION
    record_test(services, "passed")
    assert check(services, "restore_test")[0]["status"] == safety.OK
    record_test(services, "passed", age_days=30)
    assert check(services, "restore_test")[0]["status"] == safety.ATTENTION
    record_test(services, "failed")
    assert check(services, "restore_test")[0]["status"] == safety.PROBLEM


# -- the index in Drive, and here ---------------------------------------------------------

def test_the_index_copy_never_sent_then_sent(services, tmp_path):
    import json
    from pathlib import Path

    connect(services)
    assert check(services, "index_copy")[0]["status"] == safety.ATTENTION
    Path(services.cfg.state_dir, "cloud-index-copy.json").write_text(
        json.dumps({"at": time.time(), "encrypted": True, "name": "ninaivu-index-a"}))
    found, _ = check(services, "index_copy")
    assert found["status"] == safety.OK and "encrypted" in found["summary"]


def test_no_local_backup_is_a_problem(services):
    assert check(services, "backups")[0]["status"] == safety.PROBLEM


def test_a_fresh_local_backup_is_ok_once_the_index_is_also_in_drive(services):
    import json
    from pathlib import Path

    assert services.backups.run() is not None
    Path(services.cfg.state_dir, "cloud-index-copy.json").write_text(
        json.dumps({"at": time.time()}))
    assert check(services, "backups")[0]["status"] == safety.OK


# -- drives, archive and storage ------------------------------------------------------------

def test_a_failing_drive_ninaivu_uses_is_named(services, monkeypatch):
    report = {"supported": True, "checked_at": time.time(), "drives": [
        {"name": "Seagate BUP RD", "status": "critical", "used_for": ["library"],
         "problems": [{"advice": "Copy anything irreplaceable off it now."}]},
        {"name": "AirDisk", "status": "ok", "used_for": [], "problems": []}]}
    monkeypatch.setattr(services.disks, "report", lambda: report)
    found, _ = check(services, "drives")
    assert found["status"] == safety.PROBLEM and "Seagate BUP RD" in found["summary"]


def test_an_incomplete_archive_run_is_a_problem(services, monkeypatch):
    from ninaivu.archive import database as archive_db

    monkeypatch.setattr(archive_db, "latest_job", lambda: {
        "state": "completed", "mode": "copy", "ended_at": time.time() - 3600,
        "message": "INCOMPLETE - 61 folders could not be read"})
    found, _ = check(services, "archive")
    assert found["status"] == safety.PROBLEM and "Run it again" in found["detail"]
    monkeypatch.setattr(archive_db, "latest_job", lambda: None)
    assert check(services, "archive")[0]["status"] == safety.OFF


def test_a_file_that_changed_on_disk_is_a_problem(services):
    conn = db.connect(services.cfg.db_path)
    assert check(services, "storage")[0]["status"] == safety.ATTENTION   # never checked
    asset = conn.execute("SELECT id, root, rel_path FROM assets LIMIT 1").fetchone()
    db.record_bitrot_check(conn, asset["id"], asset["root"], asset["rel_path"],
                           "aaa", "bbb", "corrupt")
    assert check(services, "storage")[0]["status"] == safety.PROBLEM


# -- the answer as a whole ------------------------------------------------------------------

def test_a_check_that_cannot_answer_does_not_take_the_page(services, monkeypatch):
    def broken(services_, now):
        raise RuntimeError("the archive database is locked")

    monkeypatch.setattr(safety, "CHECKS", [broken, safety.archive])
    report = services.safety.report(fresh=True)
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses["broken"] == safety.UNKNOWN
    assert statuses["archive"] != safety.UNKNOWN, "one failure took another check"


def test_the_answer_is_kept_for_a_minute(services, monkeypatch):
    first = services.safety.report(fresh=True)
    calls = []
    monkeypatch.setattr(safety, "CHECKS", [lambda s, n: calls.append(1) or
                                           safety._check("x", "x", safety.OK, "", "")])
    assert services.safety.report() is first
    services.safety.report(fresh=True)
    assert calls == [1]


def test_worst_first_and_a_headline(services):
    report = services.safety.report(fresh=True)
    order = [safety._ORDER[c["status"]] for c in report["checks"]]
    assert order == sorted(order)
    assert report["headline"].endswith(".")


def test_the_console_shows_it_and_the_family_app_does_not(app, people):
    from conftest import ADMIN, FAMILY, login

    admin = login(app.test_client(), *ADMIN)
    report = admin.get("/api/admin/safety?fresh=1").get_json()
    assert {c["id"] for c in report["checks"]} >= {"cloud", "restore_test", "index_copy",
                                                   "backups", "drives", "archive",
                                                   "storage"}
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/admin/safety").status_code in (401, 403, 404)
