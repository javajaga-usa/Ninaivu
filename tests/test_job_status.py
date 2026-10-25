"""The Activity page's "Background jobs": how far each long job over the
library has got, running or not (ninaivu/server/job_status.py).

The activity strip shows a job only while it runs, so 180,000 photographs
described overnight left nothing on screen to say they were done.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ninaivu.runtime import florence
from ninaivu.runtime.describer import Describer
from ninaivu.server import job_status
from ninaivu.storage import db, descriptions

from tests.conftest import FAMILY, login

STATES = {"running", "waiting", "complete", "off", "unavailable"}


@pytest.fixture(autouse=True)
def _count_afresh():
    job_status._cache.update(at=0.0, rows=None)
    yield
    job_status._cache.update(at=0.0, rows=None)


def test_every_job_over_the_library_has_a_row_saying_where_it_stands(as_admin):
    response = as_admin.get("/api/admin/jobs")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.get_json()["jobs"]}
    assert {"descriptions", "story-words", "tags",
            "places", "text", "faces", "videos", "fingerprints"} <= set(rows)
    # Moments inside videos are off by default, and then not listed at all.
    assert "video-moments" not in rows
    assert all(row["state"] in STATES for row in rows.values())
    # The test library runs with the image model off: off, and no counts.
    assert rows["tags"]["state"] == "off" and rows["tags"]["left"] is None
    assert response.headers["Cache-Control"] == "no-store"


def test_only_an_administrator_sees_the_jobs(app, people):
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/admin/jobs").status_code == 403


def test_a_finished_job_says_complete_with_how_many_were_done(as_admin, scanned):
    cfg, conn, _ = scanned
    cfg.ai_enabled = True
    owed = conn.execute(
        "SELECT COUNT(*) FROM assets WHERE trashed=0 AND thumb IS NOT NULL "
        f"AND kind != 'audio' AND {db.AI_MAY_READ}").fetchone()[0]
    assert owed
    conn.execute("UPDATE assets SET ai_version = 0")
    conn.commit()
    tags = next(r for r in as_admin.get("/api/admin/jobs?fresh=1").get_json()["jobs"]
                if r["id"] == "tags")
    assert tags["state"] == "waiting" and tags["left"] == owed and tags["done"] == 0

    from ninaivu.media.scanner import AI_VERSION
    conn.execute("UPDATE assets SET ai_version = ?", (AI_VERSION,))
    conn.commit()
    tags = next(r for r in as_admin.get("/api/admin/jobs?fresh=1").get_json()["jobs"]
                if r["id"] == "tags")
    assert tags["state"] == "complete"
    assert tags["left"] == 0 and tags["done"] == tags["total"] == owed


def test_an_answer_is_kept_a_while_and_counted_again_when_asked(as_admin, scanned):
    cfg, conn, _ = scanned
    first = as_admin.get("/api/admin/jobs").get_json()["counted_at"]
    assert as_admin.get("/api/admin/jobs").get_json()["counted_at"] == first
    assert as_admin.get("/api/admin/jobs?fresh=1").get_json()["counted_at"] > first


# -- describing photographs ---------------------------------------------------

@pytest.fixture()
def pictures(tmp_path, monkeypatch):
    conn = db.init_db(tmp_path / "index.db")
    for n in range(3):
        conn.execute(
            "INSERT INTO assets(root, rel_path, folder, filename, ext, kind, size, mtime, "
            "thumb, date_key, visibility) VALUES('/lib', ?, '', ?, 'jpg', 'picture', 1, 0, ?, "
            "'2026-10-01', 1)", (f"{n}.jpg", f"{n}.jpg", f"t{n}"))
    conn.commit()
    monkeypatch.setattr(florence, "chosen_model", lambda: "captions-large")
    monkeypatch.setattr(florence, "ready", lambda: None)
    cfg = SimpleNamespace(ai_runtime=True, describe_photos=True, db_path=tmp_path / "index.db")
    describer = Describer(cfg, lambda: conn, host=lambda: None, hold=lambda: None)
    return conn, cfg, SimpleNamespace(describer=describer)


def _row(services, cfg, conn):
    return job_status._descriptions(services, cfg, conn)


def test_describing_says_how_many_are_left_then_complete(pictures):
    conn, cfg, services = pictures
    row = _row(services, cfg, conn)
    assert (row["state"], row["left"], row["done"], row["total"]) == ("waiting", 3, 0, 3)

    ids = [r[0] for r in conn.execute("SELECT id FROM assets")]
    for asset_id in ids:
        descriptions.store(conn, asset_id, "a lamp", model="captions-large",
                           version=florence.version())
    row = _row(services, cfg, conn)
    assert (row["state"], row["left"], row["done"], row["total"]) == ("complete", 0, 3, 3)


def test_a_photograph_set_aside_is_counted_as_one_that_could_not_be_done(pictures):
    conn, cfg, services = pictures
    first = conn.execute("SELECT id FROM assets ORDER BY id").fetchone()[0]
    for _ in range(descriptions.MAX_FAILURES):
        descriptions.fail(conn, first, florence.version(), "would not open")
    row = _row(services, cfg, conn)
    assert row["failed"] == 1 and row["left"] == 2


def test_describing_turned_off_or_without_its_model_says_so(pictures, monkeypatch):
    conn, cfg, services = pictures
    cfg.describe_photos = False
    assert _row(services, cfg, conn)["state"] == "off"
    cfg.describe_photos = True
    monkeypatch.setattr(florence, "ready", lambda: "no description model is downloaded")
    row = _row(services, cfg, conn)
    assert row["state"] == "unavailable" and row["why"] == "no description model is downloaded"
    assert row["left"] is None


def test_a_running_pass_says_running(pictures):
    conn, cfg, services = pictures
    services.describer._say(running=True)
    assert _row(services, cfg, conn)["state"] == "running"


def test_one_job_that_cannot_be_counted_leaves_the_others(pictures, monkeypatch):
    conn, cfg, services = pictures

    def broken(*_args):
        raise RuntimeError("no")

    monkeypatch.setattr(job_status, "WORKERS", [broken, job_status._descriptions])
    monkeypatch.setattr(job_status, "_scan_jobs", lambda *_: [])
    cfg.library_roots = ["/lib"]
    rows = job_status.report(services, cfg, fresh=True)["jobs"]
    assert [r["id"] for r in rows] == ["descriptions"]
