"""The weekly test restore: proof the backup comes back, before it has to.

Uploads through the real engine into the Drive stand-in, then lets the tester
restore a sample into its temporary folder and check it — against what was
sent, and against the originals still in the library.
"""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fake_drive import FakeDrive
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import keyring, restore_test, store
from ninaivu.cloud.restore_test import FAILED, PASSED, SKIPPED, RestoreTester
from ninaivu.cloud.upload_cache import UploadCache
from ninaivu.storage import db

PASSPHRASE = "correct horse battery staple"


@pytest.fixture()
def world(tmp_path):
    db_path = tmp_path / "index.db"
    store.init_schema(db.init_db(db_path))
    fake = FakeDrive()
    client = drive_mod.DriveClient(
        creds=drive_mod.Credentials(client_id="c", client_secret="s", refresh_token="r",
                                    access_token="a", expires_at=9e9),
        transport=fake)
    library = tmp_path / "lib"
    (library / "2019").mkdir(parents=True)
    files = {"2019/a.jpg": os.urandom(5000), "2019/b.jpg": os.urandom(7000),
             "2019/c.mp4": os.urandom(9000)}
    for rel, data in files.items():
        (library / rel).write_bytes(data)
    state = tmp_path / "state"
    state.mkdir()
    cfg = SimpleNamespace(state_dir=state, restore_test_days=7, restore_test_files=5,
                          restore_test_max_mb=1024)
    service = SimpleNamespace(creds=SimpleNamespace(connected=True),
                              client=lambda: client, _restore=None)
    told = []
    tester = RestoreTester(cfg, service, connect_db=lambda: db.connect(db_path),
                           notify=lambda *a: told.append(a))
    return SimpleNamespace(db_path=db_path, fake=fake, client=client, library=library,
                           files=files, cfg=cfg, service=service, tester=tester,
                           told=told, state=state)


def upload(w, *, encrypted=False):
    conn = db.connect(w.db_path)
    for rel in w.files:
        path = w.library / rel
        store.remember(conn, str(w.library), rel, size=path.stat().st_size,
                       filename=path.name)
    kwargs = {}
    if encrypted:
        record = keyring.create(w.state, PASSPHRASE, PASSPHRASE)
        kwargs = dict(encryption=lambda: keyring.key_material(record),
                      cache=UploadCache(w.state / "cache"))
    engine = engine_mod.SyncEngine(connect=lambda: w.client,
                                   open_db=lambda: db.connect(w.db_path), **kwargs)
    engine.start()
    engine._thread.join(30)
    assert store.summary(conn)["done"] == len(w.files)


def test_the_backup_restores_and_matches_the_library(world):
    upload(world)
    result = world.tester.run()
    assert result["status"] == PASSED, result
    assert result["restored"] == 3 and result["matched"] == 3
    assert "byte for byte" in result["summary"]
    assert not list((world.state / "restore-test").glob("*")), "the copies were left behind"
    assert world.told == []


def test_it_touches_nothing_in_the_library(world):
    upload(world)
    before = {p: p.stat().st_mtime for p in world.library.rglob("*") if p.is_file()}
    world.tester.run()
    after = {p: p.stat().st_mtime for p in world.library.rglob("*") if p.is_file()}
    assert before == after


def test_an_encrypted_backup_restores_with_the_key_here(world):
    upload(world, encrypted=True)
    assert world.tester.run()["status"] == PASSED


def test_an_encrypted_backup_whose_key_is_gone_fails_loudly(world):
    """The case that looks fine until the day it is needed."""
    upload(world, encrypted=True)
    keyring.path(world.state).unlink()
    result = world.tester.run()
    assert result["status"] == FAILED
    assert "no longer has the key" in result["summary"]
    assert world.told and world.told[0][0] == "restore_test"
    assert world.fake.downloads == [], "nothing is worth downloading without a key"


def test_a_backup_damaged_in_drive_fails_and_says_so(world):
    upload(world)
    world.fake.corrupt_download = True
    result = world.tester.run()
    assert result["status"] == FAILED and result["failed"] == 3
    assert "SHA-256" in result["detail"][0]["why"]
    assert world.told[0][1] == "A test restore from Google Drive failed"


def test_a_library_copy_changed_since_backup_is_noticed_not_failed(world):
    upload(world)
    (world.library / "2019/a.jpg").write_bytes(os.urandom(5000))    # same size
    result = world.tester.run()
    assert result["status"] == PASSED and result["matched"] == 2
    assert any(d.get("warning") and "no longer matches" in d["why"]
               for d in result["detail"])


def test_it_skips_what_it_cannot_test(world):
    world.service.creds.connected = False
    assert world.tester.run()["status"] == SKIPPED
    world.service.creds.connected = True
    assert "Nothing has been uploaded" in world.tester.run()["summary"]


def test_it_takes_a_handful_and_leaves_out_the_huge(world):
    upload(world)
    world.cfg.restore_test_files = 2
    items = restore_test.pick(db.connect(world.db_path), 2, 8000)
    assert len(items) == 2
    assert all(item.size <= 8000 for item in items)


def test_a_video_is_among_them_when_there_is_one(world):
    upload(world)
    for _ in range(5):
        items = restore_test.pick(db.connect(world.db_path), 2, 1 << 30)
        assert any(item.rel_path.endswith(".mp4") for item in items)


def test_every_test_is_written_down(world):
    upload(world)
    world.tester.run()
    world.fake.corrupt_download = True
    world.tester.run()
    history = world.tester.history()
    assert [h["status"] for h in history] == [FAILED, PASSED]
    assert world.tester.status()["last"]["status"] == FAILED


# -- when it runs by itself ---------------------------------------------------------

def test_one_is_due_a_week_after_the_last(world):
    clock = SimpleNamespace(now=time.time())
    world.tester._clock = lambda: clock.now
    assert not world.tester.due(), "nothing uploaded yet"
    upload(world)
    assert world.tester.due()
    world.tester.run(scheduled=True)
    assert not world.tester.due()
    clock.now += 8 * 86400
    assert world.tester.due()


def test_it_waits_for_the_household_and_can_be_turned_off(world):
    upload(world)
    world.tester._hold = lambda: "someone is watching a video"
    assert not world.tester.due()
    world.tester._hold = None
    world.cfg.restore_test_days = 0
    assert not world.tester.due()


def test_it_does_not_run_beside_a_real_restore(world):
    upload(world)
    world.service._restore = SimpleNamespace(running=True)
    assert not world.tester.due()


def test_the_activity_strip_says_a_test_is_running():
    from ninaivu.server import activity

    tester = SimpleNamespace(running=True, current=SimpleNamespace(
        state=SimpleNamespace(snapshot=lambda: {"processed": 2, "total": 5})))
    job = activity._restore_test(SimpleNamespace(restore_tests=tester), None)
    assert job["title"] == "Testing a restore from Google Drive"
    assert job["percent"] == 40


# -- the console -----------------------------------------------------------------------

def test_the_console_runs_one_and_shows_the_result(app, people):
    from conftest import ADMIN, FAMILY, login

    services = app.config["MV_SERVICES"]
    fake = FakeDrive()
    services.cloud.creds = drive_mod.Credentials(
        client_id="c", client_secret="s", refresh_token="r", access_token="a",
        expires_at=9e9)
    services.cloud.client = lambda: drive_mod.DriveClient(creds=services.cloud.creds,
                                                          transport=fake)
    engine = services.cloud.engine()
    engine.start()
    engine._thread.join(60)
    admin = login(app.test_client(), *ADMIN)

    assert admin.post("/api/cloud/restore-tests/run").get_json()["ok"]
    deadline = time.time() + 60
    status = {}
    while time.time() < deadline:
        status = admin.get("/api/cloud/restore-tests").get_json()
        if not status["running"] and status["last"]:
            break
        time.sleep(0.1)
    assert status["last"]["status"] == PASSED, status["last"]

    assert admin.post("/api/cloud/restore-tests/settings",
                      json={"every_days": 14}).get_json()["every_days"] == 14
    assert admin.post("/api/cloud/restore-tests/settings",
                      json={"every_days": 400}).status_code == 400
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/cloud/restore-tests").status_code in (401, 403, 404)
    assert Path(services.cfg.state_dir, "restore-test").exists() is False or \
        not list(Path(services.cfg.state_dir, "restore-test").glob("*"))


def test_a_failure_notification_names_no_files(world):
    """It goes to a webhook or a mail server outside the house; a path inside
    the library is what notify.py promises never to send."""
    upload(world)
    world.fake.corrupt_download = True
    world.tester.run()
    _event, _title, detail = world.told[0]
    for rel in world.files:
        assert str(rel) not in detail and Path(rel).name not in detail
    assert "Mugil page" in detail
