"""Getting the photographs back out of Google Drive.

Everything here uploads through the real engine into a stand-in for Drive
(tests/fake_drive.py), then restores through the real client, so what is
tested is the round trip a household would depend on: the file that comes
back is the file that went, byte for byte, or it is not put anywhere.
"""

import json
import os
import time
from pathlib import Path

import pytest

from fake_drive import FakeDrive
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import keyring, restore, store
from ninaivu.cloud.upload_cache import UploadCache
from ninaivu.storage import db

PASSPHRASE = "correct horse battery staple"
TAKEN = 1_262_304_000.0                      # 2010-01-01, well before the test


@pytest.fixture()
def world(tmp_path):
    db_path = tmp_path / "index.db"
    store.init_schema(db.init_db(db_path))
    fake = FakeDrive()
    creds = drive_mod.Credentials(client_id="cid", client_secret="secret",
                                  refresh_token="r", access_token="a",
                                  expires_at=9e9, folder_name="Ninaivu")
    client = drive_mod.DriveClient(creds=creds, transport=fake)
    library = tmp_path / "lib"
    (library / "2019" / "07").mkdir(parents=True)
    (library / "2021").mkdir(parents=True)
    files = {
        "2019/07/beach.jpg": os.urandom(restore.PIECE + 12345),   # more than one piece
        "2019/07/pier.jpg": b"P" * 2048,
        "2021/garden.jpg": os.urandom(4096),
    }
    for rel, data in files.items():
        path = library / rel
        path.write_bytes(data)
        os.utime(path, (TAKEN, TAKEN))
    return db_path, fake, client, library, files, tmp_path


def upload(world, *, encrypted=False):
    db_path, fake, client, library, files, tmp_path = world
    conn = db.connect(db_path)
    for rel in files:
        path = library / rel
        store.remember(conn, str(library), rel, size=path.stat().st_size,
                       filename=path.name)
        conn.execute("UPDATE cloud_uploads SET source_mtime=? WHERE rel_path=?",
                     (TAKEN, rel))
    conn.commit()
    key = None
    kwargs = {}
    if encrypted:
        record = keyring.create(tmp_path / "state", PASSPHRASE, PASSPHRASE)
        key = keyring.key_material(record)[0]
        params = tmp_path / "state" / restore.PARAMS_NAME
        params.write_text(json.dumps(keyring.public_params(record)))
        published = []

        def publish(drive_client):                 # as the service does, once
            if not published:
                published.append(drive_client.upload_file(
                    params, params.name, drive_client.ninaivu_root()))

        kwargs = dict(encryption=lambda: keyring.key_material(record),
                      cache=UploadCache(tmp_path / "state" / "cache"),
                      publish_params=publish)
    engine = engine_mod.SyncEngine(connect=lambda: client,
                                   open_db=lambda: db.connect(db_path), **kwargs)
    engine.start()
    engine._thread.join(60)
    assert store.summary(conn)["done"] == len(files)
    return key


def run(job):
    job.start()
    job.join(60)
    assert not job.running
    return job.state.snapshot()


def lose_the_library(library):
    for path in list(library.rglob("*")):
        if path.is_file():
            path.unlink()


# -- the record: what went up, put back where it was --------------------------

def test_a_lost_library_comes_back_byte_for_byte(world):
    db_path, fake, client, library, files, _ = world
    upload(world)
    lose_the_library(library)

    items = restore.from_record(db.connect(db_path))
    state = run(restore.RestoreJob(connect=lambda: client, items=items,
                                   destination=None))

    assert state["finished"] and state["restored"] == 3 and state["failed"] == 0
    for rel, data in files.items():
        assert (library / rel).read_bytes() == data
    assert not (library / restore.STAGING).exists(), "staging was left behind"


def test_a_restored_file_keeps_its_date(world):
    """The date chain falls back to the file's timestamp; a restore dated
    today would move an undated photograph to the day of the disaster."""
    db_path, _, client, library, _, _ = world
    upload(world)
    lose_the_library(library)
    run(restore.RestoreJob(connect=lambda: client,
                           items=restore.from_record(db.connect(db_path)),
                           destination=None))
    assert abs((library / "2021/garden.jpg").stat().st_mtime - TAKEN) < 2


def test_encrypted_backups_come_back_decrypted(world):
    db_path, fake, client, library, files, _ = world
    key = upload(world, encrypted=True)
    assert all(n.endswith(".ninaivu") for n in fake.uploaded_names()
               if n != restore.PARAMS_NAME)
    lose_the_library(library)

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None, key=key))

    assert state["restored"] == 3, state["problems"]
    for rel, data in files.items():
        assert (library / rel).read_bytes() == data


def test_the_wrong_key_writes_nothing(world):
    db_path, _, client, library, _, tmp_path = world
    upload(world, encrypted=True)
    lose_the_library(library)
    wrong = keyring.derive("another passphrase entirely", b"0" * 16)

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None, key=wrong))

    assert state["failed"] == 3 and state["restored"] == 0
    assert "different key" in state["problems"][0]["why"]
    assert not [p for p in library.rglob("*.jpg")]


def test_a_damaged_download_is_not_put_anywhere(world):
    db_path, fake, client, library, _, _ = world
    upload(world)
    lose_the_library(library)
    fake.corrupt_download = True

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None))

    assert state["failed"] == 3
    assert "SHA-256" in state["problems"][0]["why"]
    assert not [p for p in library.rglob("*.jpg")]


def test_nothing_is_overwritten(world):
    """A different photograph at the same path stays; the backup goes beside."""
    db_path, _, client, library, files, _ = world
    upload(world)
    (library / "2021/garden.jpg").write_bytes(b"a newer photograph")

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None))

    assert (library / "2021/garden.jpg").read_bytes() == b"a newer photograph"
    assert (library / "2021/garden (restored).jpg").read_bytes() == files["2021/garden.jpg"]
    assert state["already_there"] == 2 and state["beside"] == 1


def test_a_damaged_photograph_the_same_size_is_brought_back(world):
    """Bit rot and a failing disk leave a photograph the size it was. Judged by
    size alone it was "already there", and the restore that exists for
    exactly this case put nothing back."""
    db_path, _, client, library, files, _ = world
    upload(world)
    damaged = bytearray(files["2021/garden.jpg"])
    damaged[100:108] = b"\x00" * 8
    (library / "2021/garden.jpg").write_bytes(bytes(damaged))

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None))

    assert state["beside"] == 1 and state["already_there"] == 2, state
    assert (library / "2021/garden (restored).jpg").read_bytes() == files["2021/garden.jpg"]
    assert (library / "2021/garden.jpg").read_bytes() == bytes(damaged), "overwrote a file"


def test_running_it_again_downloads_nothing(world):
    db_path, fake, client, library, _, _ = world
    upload(world)
    (library / "2021/garden.jpg").write_bytes(b"a newer photograph")
    items = restore.from_record(db.connect(db_path))
    run(restore.RestoreJob(connect=lambda: client, items=items, destination=None))
    fake.downloads.clear()

    state = run(restore.RestoreJob(connect=lambda: client, items=items,
                                   destination=None))

    assert fake.downloads == []
    assert state["already_there"] == 3
    assert not (library / "2021/garden (restored 2).jpg").exists()


def test_a_stopped_restore_carries_on_from_the_bytes_it_has(world, monkeypatch):
    db_path, fake, client, library, files, _ = world
    upload(world)
    lose_the_library(library)
    items = [i for i in restore.from_record(db.connect(db_path))
             if i.rel_path.endswith("beach.jpg")]
    job = restore.RestoreJob(connect=lambda: client, items=items, destination=None)
    real = client.download_range

    def stop_after_one_piece(file_id, start, end):
        piece = real(file_id, start, end)
        job.stop()
        return piece

    monkeypatch.setattr(client, "download_range", stop_after_one_piece)
    state = run(job)
    assert not state["finished"] and state["failed"] == 0
    staged = list((library / restore.STAGING).glob("*.part"))
    assert staged and staged[0].stat().st_size == restore.PIECE

    monkeypatch.setattr(client, "download_range", real)
    fake.downloads.clear()
    state = run(restore.RestoreJob(connect=lambda: client, items=items,
                                   destination=None))
    assert state["restored"] == 1
    assert len(fake.downloads) == 1, "it started the file again"
    assert (library / "2019/07/beach.jpg").read_bytes() == files["2019/07/beach.jpg"]


def test_part_of_the_library_can_be_chosen(world):
    db_path, *_ = world
    upload(world)
    conn = db.connect(db_path)
    assert [i.rel_path for i in restore.from_record(conn, folder="2019")] == [
        "2019/07/beach.jpg", "2019/07/pier.jpg"]
    assert [i.rel_path for i in restore.from_record(conn, folder="2019/07/pier.jpg")] == [
        "2019/07/pier.jpg"]
    assert restore.from_record(conn, roots=["/somewhere/else"]) == []


def test_it_can_restore_into_another_folder(world):
    db_path, _, client, library, files, tmp_path = world
    upload(world)
    elsewhere = tmp_path / "new-disk"

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=elsewhere))

    assert state["restored"] == 3
    for rel, data in files.items():
        assert (elsewhere / rel).read_bytes() == data
        assert (library / rel).read_bytes() == data     # untouched


def test_a_path_that_climbs_out_is_refused(world):
    _, _, client, _, _, tmp_path = world
    item = restore.RestoreItem(remote_id="x", rel_path="../../outside.jpg")
    assert restore._clean_rel(item.rel_path) is None
    job = restore.RestoreJob(connect=lambda: client, items=[item],
                             destination=tmp_path / "dest")
    state = run(job)
    assert state["failed"] == 1
    assert not (tmp_path / "outside.jpg").exists()


# -- no record: a new machine, walking the Drive folder -------------------------

def test_a_new_machine_rebuilds_from_the_drive_folder(world):
    _, fake, client, _, files, tmp_path = world
    upload(world)
    items = restore.from_drive(client)
    assert sorted(i.rel_path for i in items) == sorted(files)
    assert all(i.md5 for i in items)

    rebuilt = tmp_path / "rebuilt"
    state = run(restore.RestoreJob(connect=lambda: client, items=items,
                                   destination=rebuilt))
    assert state["restored"] == 3
    for rel, data in files.items():
        assert (rebuilt / rel).read_bytes() == data


def test_a_new_machine_rebuilds_an_encrypted_backup_with_the_passphrase(world):
    _, fake, client, _, files, tmp_path = world
    upload(world, encrypted=True)
    params = restore.find_params(client)
    key = keyring.key_from(passphrase=PASSPHRASE, params=params)

    items = restore.from_drive(client)
    assert all(i.encrypted for i in items)
    assert restore.PARAMS_NAME not in [i.rel_path for i in items]
    rebuilt = tmp_path / "rebuilt"
    state = run(restore.RestoreJob(connect=lambda: client, items=items,
                                   destination=rebuilt, key=key))
    assert state["restored"] == 3, state["problems"]
    for rel, data in files.items():
        assert (rebuilt / rel).read_bytes() == data


def test_drives_own_checksum_catches_damage_without_a_record(world):
    _, fake, client, _, _, tmp_path = world
    upload(world)
    fake.corrupt_download = True
    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_drive(client),
                                   destination=tmp_path / "rebuilt"))
    assert state["failed"] == 3 and state["restored"] == 0


def test_the_summary_says_what_a_restore_amounts_to(world):
    db_path, *_ = world
    upload(world)
    summary = restore.summarise(restore.from_record(db.connect(db_path)))
    assert summary["files"] == 3
    assert summary["bytes"] == restore.PIECE + 12345 + 2048 + 4096
    assert {f["folder"] for f in summary["folders"]} == {"2019", "2021"}


# -- the console ----------------------------------------------------------------

@pytest.fixture()
def backed_up(app, people):
    """The test library, uploaded through the real service to a fake Drive."""
    from conftest import ADMIN, login

    services = app.config["MV_SERVICES"]
    service = services.cloud
    fake = FakeDrive()
    service.creds = drive_mod.Credentials(
        client_id="cid", client_secret="secret", refresh_token="r",
        access_token="a", expires_at=9e9, folder_name="Ninaivu")
    service.client = lambda: drive_mod.DriveClient(creds=service.creds,
                                                   transport=fake)
    engine = service.engine()
    engine.start()
    engine._thread.join(60)
    done = store.summary(db.connect(app.config["MV_CONFIG"].db_path))["done"]
    assert done >= 10
    return login(app.test_client(), *ADMIN), services, fake


def wait_for_restore(admin):
    deadline = time.time() + 60
    while time.time() < deadline:
        status = admin.get("/api/cloud/restore/status").get_json()
        if not status.get("running"):
            return status
        time.sleep(0.05)
    raise AssertionError("the restore did not finish")


def test_the_console_restores_a_folder_back_into_the_library(backed_up):
    admin, services, _ = backed_up
    root = Path(services.cfg.active_root)
    lost = sorted((root / "shared" / "holiday").glob("*.jpg"))
    kept = {p.name: p.read_bytes() for p in lost}
    for path in lost:
        path.unlink()

    preview = admin.post("/api/cloud/restore/preview",
                         json={"scope": {"folder": "shared/holiday"}}).get_json()
    assert preview["files"] == len(lost) and not preview["needs_key"]

    started = admin.post("/api/cloud/restore/start",
                         json={"scope": {"folder": "shared/holiday"}})
    assert started.status_code == 200, started.get_json()
    status = wait_for_restore(admin)

    assert status["finished"] and status["restored"] == len(lost), status
    for name, data in kept.items():
        assert (root / "shared" / "holiday" / name).read_bytes() == data
    assert services.scanner.deferred is None, "the indexer was left standing down"


def test_the_console_refuses_a_restore_into_a_system_folder(backed_up):
    admin, _, _ = backed_up
    system = r"C:\Windows" if os.name == "nt" else "/etc"
    refused = admin.post("/api/cloud/restore/start",
                         json={"scope": {}, "destination": system})
    assert refused.status_code == 403


def test_an_encrypted_restore_without_a_key_is_refused_before_it_starts(backed_up):
    admin, services, fake = backed_up
    conn = db.connect(services.cfg.db_path)
    conn.execute("UPDATE cloud_uploads SET encrypted=1")
    conn.commit()
    refused = admin.post("/api/cloud/restore/start",
                         json={"scope": {"folder": "misc"}})
    assert refused.status_code == 409
    assert "recovery file" in refused.get_json()["error"]
    assert fake.downloads == []


def test_family_members_cannot_reach_the_restore(app, people):
    from conftest import FAMILY, login
    family = login(app.test_client(), *FAMILY)
    assert family.post("/api/cloud/restore/start",
                       json={"scope": {}}).status_code in (401, 403, 404)
