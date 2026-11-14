"""The weekly health digest (server/health_week.py): the week's damaged files,
free space, failed jobs and drives, for administrators only.

Each case sets one part into a known state on a real set of services and
checks what the console and the family app would be told.
"""
import json
import sqlite3
import time
from collections import namedtuple

import pytest

from ninaivu.engine.engine import database_path
from ninaivu.engine.tasks import TaskStore
from ninaivu.server import health_week as hw
from ninaivu.storage import db


@pytest.fixture()
def services(app, people):
    return app.config["MV_SERVICES"]


def part(report, part_id):
    return next(p for p in report["current"]["parts"] if p["id"] == part_id)


def fresh(services):
    return services.health_week.report(fresh=True)


# -- who may ask ---------------------------------------------------------------

def test_only_an_administrator_is_told(as_admin, as_family, as_guest, anon):
    answer = as_admin.get("/api/health/week?fresh=1")
    assert answer.status_code == 200
    body = answer.get_json()
    assert [p["id"] for p in body["current"]["parts"]] == ["damaged", "space", "jobs", "drives"]
    assert body["weeks"] == []
    for other in (as_family, as_guest, anon):
        assert other.get("/api/health/week").status_code in (401, 403)


def test_a_quiet_library_has_a_quiet_week(services):
    report = fresh(services)
    current = report["current"]
    assert part(report, "damaged")["status"] == hw.OK
    assert part(report, "jobs")["status"] == hw.OK
    assert part(report, "drives")["status"] == hw.OK
    assert "Nothing new was found damaged this week." in part(report, "damaged")["summary"]
    assert "Nothing failed this week." in part(report, "jobs")["summary"]
    assert current["until"] - current["since"] == pytest.approx(hw.WEEK)
    # Every sentence goes as a template the console can translate.
    for p in current["parts"]:
        for line in p["lines"]:
            assert hw.filled(line["key"], line["params"]) == line["text"]


# -- damaged files ---------------------------------------------------------------

def test_a_file_the_storage_check_found_changed_this_week_is_a_problem(services):
    conn = db.connect(services.cfg.db_path)
    asset = conn.execute("SELECT id, root, rel_path FROM assets LIMIT 1").fetchone()
    conn.execute("INSERT INTO bitrot_records(asset_id, root, rel_path, status, checked_at) "
                 "VALUES(?,?,?,?,?)", (asset["id"], asset["root"], asset["rel_path"],
                                       "corrupt", time.time()))
    conn.commit()
    found = part(fresh(services), "damaged")
    assert found["status"] == hw.PROBLEM
    assert found["numbers"]["found_this_week"] == 1
    assert "found 1 file changed or unreadable this week" in found["summary"]


def test_one_found_before_the_week_is_still_waiting_but_not_new(services):
    conn = db.connect(services.cfg.db_path)
    asset = conn.execute("SELECT id, root, rel_path FROM assets LIMIT 1").fetchone()
    conn.execute("INSERT INTO bitrot_records(asset_id, root, rel_path, status, checked_at) "
                 "VALUES(?,?,?,?,?)", (asset["id"], asset["root"], asset["rel_path"],
                                       "unreadable", time.time() - 20 * hw.DAY))
    conn.commit()
    found = part(fresh(services), "damaged")
    assert found["status"] == hw.ATTENTION
    assert found["numbers"]["found_this_week"] == 0
    assert found["numbers"]["still_damaged"] == 1
    assert "still waiting to be put back" in found["summary"]


def test_a_file_added_this_week_that_could_not_be_opened_is_counted(services):
    conn = db.connect(services.cfg.db_path)
    conn.execute("UPDATE assets SET thumb=NULL, added_at=? WHERE id=(SELECT MIN(id) FROM assets "
                 "WHERE kind='picture')", (time.time(),))
    conn.execute("INSERT INTO scan_runs(root, started_at, errors, status) VALUES(?,?,?,?)",
                 (services.cfg.library_roots[0], time.time(), 3, "done"))
    conn.commit()
    found = part(fresh(services), "damaged")
    assert found["numbers"]["unopened_this_week"] == 1
    assert found["numbers"]["scan_errors"] == 3
    assert found["status"] == hw.ATTENTION
    assert "1 file added this week could not be opened." in found["summary"]


# -- free space ------------------------------------------------------------------

Usage = namedtuple("Usage", "total used free")


def test_free_space_is_compared_with_a_week_ago(services, monkeypatch):
    keeper = services.health_week
    now = time.time()
    disks = hw.disks_now(services, db.connect(services.cfg.db_path), now)
    assert disks, "the library's disk is measured"
    key = disks[0]["key"]
    then = {"readings": [{"at": now - hw.WEEK, "disks": {
        key: {"free_bytes": disks[0]["free_bytes"] + 40 * 1024 ** 3,
              "total_bytes": disks[0]["total_bytes"]}}}], "weeks": []}
    keeper.path.write_text(json.dumps(then), encoding="utf-8")
    found = part(fresh(services), "space")
    item = next(d for d in found["items"] if d["key"] == key)
    assert item["change_bytes"] < 0
    assert "less than a week ago" in found["summary"]


def test_a_nearly_full_disk_is_a_problem(services, monkeypatch):
    import shutil
    tb = 1024 ** 4
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(tb, tb - 5 * 1024 ** 3, 5 * 1024 ** 3))
    found = part(fresh(services), "space")
    assert found["status"] == hw.PROBLEM
    assert "5.0 GB free of 1.0 TB" in found["summary"]


# -- failed jobs ---------------------------------------------------------------------

def test_photographs_the_engine_set_aside_this_week_are_failed_jobs(services):
    store = TaskStore(database_path(services.cfg))
    root = services.cfg.library_roots[0]
    store.add("faces", root, 1, [(1, 0.0), (2, 0.0)])
    store._conn.execute("UPDATE tasks SET state='failed', updated_at=?", (time.time(),))
    store.close()
    found = part(fresh(services), "jobs")
    assert found["status"] == hw.ATTENTION
    assert found["numbers"]["engine"] == {"faces": 2}
    assert "Finding faces: 2." in found["summary"]


def test_an_old_failure_is_not_this_weeks(services):
    store = TaskStore(database_path(services.cfg))
    store.add("faces", services.cfg.library_roots[0], 1, [(1, 0.0)])
    store._conn.execute("UPDATE tasks SET state='failed', updated_at=?",
                        (time.time() - 30 * hw.DAY,))
    store.close()
    assert part(fresh(services), "jobs")["status"] == hw.OK


def test_a_scan_that_stopped_with_an_error_is_counted(services):
    conn = db.connect(services.cfg.db_path)
    conn.execute("INSERT INTO scan_runs(root, started_at, status) VALUES(?,?,?)",
                 (services.cfg.library_roots[0], time.time(), "error"))
    conn.commit()
    found = part(fresh(services), "jobs")
    assert found["numbers"]["scans_failed"] == 1
    assert found["status"] == hw.ATTENTION


# -- drives ------------------------------------------------------------------------------

def test_a_library_folder_not_connected_is_named_by_its_folder_only(services, tmp_path):
    gone = tmp_path / "unplugged" / "Old Photos"
    services.cfg.roots = [*services.cfg.roots, str(gone)]
    found = part(fresh(services), "drives")
    assert found["status"] == hw.ATTENTION
    assert found["numbers"]["away"] == 1
    assert "Old Photos" in found["summary"]
    assert str(tmp_path) not in found["summary"]


def test_one_part_that_cannot_answer_leaves_the_rest(services, monkeypatch):
    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such table")
    monkeypatch.setattr(hw, "PARTS", [hw.damaged, broken, hw.drives])
    report = fresh(services)
    assert [p["id"] for p in report["current"]["parts"]] == ["damaged", "broken", "drives"]
    assert report["current"]["parts"][1]["summary"] == "Could not tell."


# -- the keeper ----------------------------------------------------------------------------

def test_a_finished_week_is_kept_once_and_sent_in_counts(services):
    sent = []
    keeper = hw.HealthWeek(services, notify=lambda *a: sent.append(a))
    kept = keeper.tick()
    assert kept is not None and kept["until"] == hw.week_start(time.time())
    assert keeper.tick() is None, "the same week is not kept twice"
    data = json.loads(keeper.path.read_text(encoding="utf-8"))
    assert len(data["weeks"]) == 1 and len(data["readings"]) == 2
    assert len(sent) == 1
    event, summary, detail = sent[0]
    assert event == hw.EVENT
    assert summary.startswith("Ninaivu's week:")
    # Never a path or a folder's name in a notification.
    for root in services.cfg.library_roots:
        assert root not in detail and root not in summary
    assert keeper.report(fresh=True)["weeks"][0]["verdict"] == kept["verdict"]


def test_old_readings_are_dropped(services):
    keeper = services.health_week
    old = {"readings": [{"at": time.time() - hw.KEEP_READINGS - hw.DAY, "disks": {}}],
           "weeks": [{"until": hw.week_start(time.time())}]}
    keeper.path.write_text(json.dumps(old), encoding="utf-8")
    keeper.tick()
    data = json.loads(keeper.path.read_text(encoding="utf-8"))
    assert len(data["readings"]) == 1


def test_a_damaged_file_does_not_stop_the_answer(services):
    services.health_week.path.write_text("{not json", encoding="utf-8")
    assert fresh(services)["weeks"] == []


def test_the_week_starts_on_monday_at_midnight():
    from datetime import datetime
    start = datetime.fromtimestamp(hw.week_start(time.time()))
    assert start.weekday() == 0 and (start.hour, start.minute, start.second) == (0, 0, 0)
