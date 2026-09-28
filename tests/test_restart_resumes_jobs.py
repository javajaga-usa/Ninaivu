"""Long jobs somebody started carry on after Ninaivu restarts.

Each job writes itself down when it starts and crosses itself off when it
finishes or somebody stops it. Shutting down does neither, so start-up carries
on exactly what a restart interrupted: a cloud upload, a straightening survey
or apply, a storage check, a model download.
"""

import pytest

from ninaivu.api import admin_api, ai_models_api
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import service as cloud_service
from ninaivu.cloud import store
from ninaivu.media import model_catalog, straighten
from ninaivu.storage import db, resume


@pytest.fixture()
def conn(cfg):
    connection = db.init_db(cfg.db_path)
    return connection


# -- the record ------------------------------------------------------------------

def test_a_job_is_remembered_until_it_is_crossed_off(conn):
    resume.want(conn, "scrubber", {"after_id": 12})
    resume.want(conn, "model:siglip2", {"model_id": "siglip2"})
    assert resume.wanted(conn) == {"scrubber": {"after_id": 12},
                                   "model:siglip2": {"model_id": "siglip2"}}
    resume.done(conn, "scrubber")
    assert list(resume.wanted(conn)) == ["model:siglip2"]


# -- cloud backup ------------------------------------------------------------------

def test_an_upload_with_nothing_left_crosses_itself_off(cfg, conn):
    store.init_schema(conn)
    resume.want(conn, cloud_service.RESUME_NAME)
    engine = engine_mod.SyncEngine(
        connect=lambda: object(), open_db=lambda: db.connect(cfg.db_path),
        on_finished=lambda: resume.done(db.connect(cfg.db_path),
                                        cloud_service.RESUME_NAME))
    engine.start()
    engine._thread.join(30)
    assert cloud_service.RESUME_NAME not in resume.wanted(conn)


def test_pausing_in_the_console_is_not_carried_on(as_admin, cfg):
    connection = db.connect(cfg.db_path)
    resume.want(connection, cloud_service.RESUME_NAME)
    assert as_admin.post("/api/cloud/pause").status_code == 200
    assert cloud_service.RESUME_NAME not in resume.wanted(connection)


# -- straightening -----------------------------------------------------------------

def test_a_survey_that_could_not_run_is_not_carried_on(cfg, conn):
    """No orientation model: it ends at once, and must not try every start."""
    straightener = straighten.Straightener(cfg)
    straighten.init_schema(conn)
    straightener._survey([str(cfg.active_root)], None, False)
    assert straighten.RESUME_NAME not in resume.wanted(conn)


def test_a_survey_stopped_by_a_shutdown_is_carried_on(cfg, conn, monkeypatch):
    straightener = straighten.Straightener(cfg)
    straighten.init_schema(conn)

    def stopped_part_way(self, conn_, roots, limit, rescan):
        self._stop.set()

    monkeypatch.setattr(straighten.Straightener, "_survey_all", stopped_part_way)
    straightener._survey([str(cfg.active_root)], 50, True)
    assert resume.wanted(conn)[straighten.RESUME_NAME] == {"job": "survey", "limit": 50}


def test_an_apply_carried_on_joins_the_batch_it_started(cfg, scanned, monkeypatch):
    """So one Undo still puts back the whole run."""
    _, conn, _ = scanned
    straighten.init_schema(conn)
    ids = [r["id"] for r in conn.execute("SELECT id FROM assets LIMIT 2")]
    for asset_id in ids:
        conn.execute("INSERT INTO orientation_proposals(asset_id, rotation, confidence, "
                     "status) VALUES(?, 90, 0.99, 'pending')", (asset_id,))
    conn.commit()
    monkeypatch.setattr(straighten.Straightener, "_turn_one",
                        lambda self, c, row, batch, now: c.execute(
                            "UPDATE orientation_proposals SET status='applied', batch=? "
                            "WHERE asset_id=?", (batch, row["asset_id"])))
    straightener = straighten.Straightener(cfg)

    straightener._apply(None, 0.5, batch=7)

    batches = {r[0] for r in conn.execute("SELECT batch FROM orientation_proposals")}
    assert batches == {7}
    assert straighten.RESUME_NAME not in resume.wanted(conn)


def test_stopping_straightening_in_the_console_is_not_carried_on(as_admin, cfg):
    connection = db.connect(cfg.db_path)
    resume.want(connection, straighten.RESUME_NAME, {"job": "survey"})
    assert as_admin.post("/api/straighten/stop").status_code == 200
    assert straighten.RESUME_NAME not in resume.wanted(connection)


# -- the storage check -------------------------------------------------------------

def test_a_storage_check_carries_on_from_where_it_got(cfg, scanned, monkeypatch):
    _, conn, _ = scanned
    ids = [r["id"] for r in conn.execute("SELECT id FROM assets WHERE trashed=0 ORDER BY id")]
    assert len(ids) > 6
    monkeypatch.setattr(admin_api, "SCRUBBER_CHECKPOINT", 2)
    monkeypatch.setattr(admin_api, "_report_scrubber_findings", lambda: None)
    real_hash = admin_api._hash_file
    checked = []

    class Shutdown(BaseException):
        pass

    def hash_until_shutdown(path):
        if len(checked) == 5:
            raise Shutdown()
        checked.append(path)
        return real_hash(path)

    monkeypatch.setattr(admin_api, "_hash_file", hash_until_shutdown)
    with pytest.raises(Shutdown):
        admin_api._run_scrubber(cfg.db_path)
    reached = resume.wanted(conn)[admin_api.SCRUBBER_RESUME]["after_id"]
    assert reached == ids[3], "the check did not note how far it got"

    checked.clear()
    monkeypatch.setattr(admin_api, "_hash_file", lambda path: checked.append(path) or real_hash(path))
    admin_api._run_scrubber(cfg.db_path, reached)
    assert len(checked) == len(ids) - 4, "the carried-on check started again from the top"
    assert admin_api._SCRUBBER_PROGRESS["processed"] == len(ids)
    assert admin_api.SCRUBBER_RESUME not in resume.wanted(conn)


# -- model downloads ---------------------------------------------------------------

def test_a_model_download_is_remembered_until_it_ends(cfg, conn, monkeypatch):
    ended = {}

    def fake_start(model_id, opener=None, on_done=None, on_end=None,
                   force=False):
        ended["callback"] = on_end
        return True

    monkeypatch.setattr(model_catalog.downloads, "start", fake_start)
    monkeypatch.setattr(model_catalog, "installed", lambda model_id: False)
    assert ai_models_api.start_model_download(cfg.db_path, "siglip2") is True
    assert "model:siglip2" in resume.wanted(conn)
    ended["callback"]("siglip2")                    # installed, or failed
    assert "model:siglip2" not in resume.wanted(conn)


# -- start-up ------------------------------------------------------------------------

@pytest.fixture()
def services(cfg):
    from ninaivu import Services
    return Services(cfg)


def test_start_up_carries_on_what_is_written_down(services, cfg, monkeypatch):
    connection = db.connect(cfg.db_path)
    cfg.cloud_enabled = True
    cfg.cloud_autostart = False
    resume.want(connection, cloud_service.RESUME_NAME)
    resume.want(connection, straighten.RESUME_NAME,
                {"job": "apply", "ids": [3, 4], "min_confidence": 0.7, "batch": 9})
    resume.want(connection, admin_api.SCRUBBER_RESUME, {"after_id": 40})
    resume.want(connection, "model:siglip2", {"model_id": "siglip2"})

    calls = []
    monkeypatch.setattr(services.cloud, "start", lambda: calls.append("cloud"))
    monkeypatch.setattr(services.straightener, "apply",
                        lambda ids, floor, batch=None: calls.append(("apply", ids, floor, batch)))
    monkeypatch.setattr(admin_api, "start_scrubber_job",
                        lambda path, after, scanner=None: calls.append(("scrubber", after)))
    monkeypatch.setattr(model_catalog, "installed", lambda model_id: False)
    monkeypatch.setattr(ai_models_api, "start_model_download",
                        lambda path, model_id, force=False:
                            calls.append(("model", model_id, force)))

    services._resume_jobs()

    assert "cloud" in calls
    assert ("apply", [3, 4], 0.7, 9) in calls
    assert ("scrubber", 40) in calls
    # With whether it was a forced re-download: a forced fetch interrupted
    # half way has to carry on being forced, or it steps over the very file
    # it was replacing.
    assert ("model", "siglip2", False) in calls


def test_nothing_written_down_starts_nothing(services, cfg, monkeypatch):
    cfg.cloud_enabled = True
    cfg.cloud_autostart = False
    calls = []
    monkeypatch.setattr(services.cloud, "start", lambda: calls.append("cloud"))
    monkeypatch.setattr(services.straightener, "survey", lambda *a, **k: calls.append("survey"))
    monkeypatch.setattr(admin_api, "start_scrubber_job", lambda *a, **k: calls.append("scrubber"))
    services._resume_jobs()
    assert calls == []


def test_a_survey_carried_on_keeps_what_it_proposed(services, cfg, monkeypatch):
    """A survey started with rescan must not rescan again, throwing away its work."""
    resume.want(db.connect(cfg.db_path), straighten.RESUME_NAME, {"job": "survey", "limit": None})
    calls = []
    monkeypatch.setattr(services.straightener, "survey",
                        lambda roots, limit=None, rescan=False: calls.append(rescan))
    services._resume_jobs()
    assert calls == [False]


def test_things_that_cannot_carry_on_are_crossed_off(services, cfg, monkeypatch):
    connection = db.connect(cfg.db_path)
    cfg.cloud_enabled = False
    resume.want(connection, cloud_service.RESUME_NAME)
    resume.want(connection, "model:no-such-model", {"model_id": "no-such-model"})
    monkeypatch.setattr(services.cloud, "start",
                        lambda: pytest.fail("cloud backup is switched off"))
    services._resume_jobs()
    assert resume.wanted(connection) == {}
