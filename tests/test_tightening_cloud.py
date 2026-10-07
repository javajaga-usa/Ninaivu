"""The tightening pass over the cloud backup, the off-site copy and the archive.

Each test names the loose end it closes: a network answer cut short that was
blamed on the disk, a stop recorded as a finished run, a restore that called
every file failed when Google was only out of reach, and so on.
"""

from __future__ import annotations

import http.client
import io
import logging
import os
import sqlite3
import time
import types
import urllib.error
from pathlib import Path

import pytest

from ninaivu.archive import guardian as guardian_mod
from ninaivu.archive import status_kit
from ninaivu.cloud import drive, engine as engine_mod, keyring, offsite as offsite_mod
from ninaivu.cloud import index_copy, restore, restore_test, s3, store
from ninaivu.cloud.drive import Credentials, DriveError
from ninaivu.cloud.offsite import Offsite
from ninaivu.storage import db


# -- CL-1: an error body cut short is still a DriveError / S3Error ------------

class _BodyThatFails(io.BytesIO):
    def __init__(self, error):
        super().__init__(b"")
        self.error = error

    def read(self, *args):
        raise self.error


@pytest.mark.parametrize("error", [TimeoutError("timed out"), http.client.IncompleteRead(b"")])
def test_an_error_body_that_cannot_be_read_still_answers_with_its_status(monkeypatch, error):
    class Opener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, _BodyThatFails(error))

    monkeypatch.setattr(drive, "_OPENER", Opener())
    status, _, body = drive._request("PUT", "https://example.invalid/upload")
    assert (status, body) == (503, b"")


@pytest.mark.parametrize("error", [TimeoutError("timed out"), http.client.IncompleteRead(b"")])
def test_s3_turns_a_broken_answer_into_an_s3_error(error):
    def error_body(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, _BodyThatFails(error))

    client = s3.S3(endpoint="https://s3.example.invalid", bucket="b", access_key="k",
                   secret_key="s", opener=error_body)
    with pytest.raises(s3.S3Error) as caught:
        client.get("x")
    assert caught.value.status == 503

    class Response:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            raise error

    client.opener = lambda request, timeout=None: Response()
    with pytest.raises(s3.S3Error) as caught:
        client.get("x")
    assert caught.value.status == 0


# -- CL-2: a folder renamed during a run is used for the rest of it ----------

def test_renaming_the_drive_folder_forgets_the_running_uploads_folders(tmp_path):
    from ninaivu.cloud.service import CloudService

    cfg = types.SimpleNamespace(state_dir=str(tmp_path), cloud_folder_name="Ninaivu")
    service = CloudService(cfg, lambda: None)
    engine = engine_mod.SyncEngine(connect=service.client, open_db=lambda: None)
    engine._folders["2019/07"] = "id-under-the-old-folder"
    service._engine = engine
    assert service.set_folder("Family photos")
    assert engine._folders == {}


# -- CL-3 and CL-4: the off-site copy ----------------------------------------

@pytest.fixture()
def lib(scanned, tmp_path):
    cfg, conn, _ = scanned
    keyring.create(cfg.state_dir, "correct horse battery", "correct horse battery")
    cfg.offsite_kind = "folder"
    cfg.offsite_folder = str(tmp_path / "friends-disk")
    Path(cfg.offsite_folder).mkdir()
    return {"cfg": cfg, "conn": conn, "offsite": Offsite(cfg, lambda: db.connect(cfg.db_path))}


def _last_run(cfg) -> str:
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT value FROM offsite_meta WHERE key='last_run'").fetchone()
    return row[0] if row else ""


def test_a_stop_part_way_through_a_file_is_not_a_finished_run(lib, monkeypatch):
    offsite = lib["offsite"]

    def stopped_mid_file(self, name, source, on_bytes=None, stop=None):
        offsite._stop.set()
        raise OSError("stopped")

    monkeypatch.setattr(offsite_mod.FolderTarget, "put_file", stopped_mid_file)
    offsite.start()
    offsite.join(60)
    status = offsite.status()
    assert status["message"].startswith("Stopped"), status["message"]
    assert not _last_run(lib["cfg"]), "the schedule must not think the copy is up to date"


def test_a_service_out_of_reach_ends_the_run_after_one_file(lib, monkeypatch):
    offsite = lib["offsite"]
    hashed = []
    real_hash = offsite_mod.sha256_file

    def counting(path):
        hashed.append(path)
        return real_hash(path)

    real_exists = offsite_mod.FolderTarget.exists

    def unreachable(self, name):
        if name.startswith("files/"):
            raise s3.S3Error(0, "could not reach the service")
        return real_exists(self, name)

    monkeypatch.setattr(offsite_mod, "sha256_file", counting)
    monkeypatch.setattr(offsite_mod.FolderTarget, "exists", unreachable)
    offsite.start()
    offsite.join(60)
    status = offsite.status()
    assert len(hashed) == 1, "the rest of the library is not read for nothing"
    assert "could not be reached" in status["error"]


def test_files_left_in_the_staging_folder_by_a_crash_are_removed(tmp_path):
    staging = tmp_path / "offsite-staging"
    staging.mkdir()
    stale, fresh = staging / "tmpabc.ninaivu", staging / "manifest.json"
    stale.write_bytes(b"x" * 10)
    fresh.write_bytes(b"{}")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    assert offsite_mod._clear_staging(staging) == 1
    assert not stale.exists() and fresh.exists()


# -- CL-5: Google out of reach stops a restore rather than failing every file --

def test_a_restore_stops_when_google_cannot_be_reached(tmp_path):
    class Unreachable:
        def download_range(self, *args):
            raise DriveError("could not reach Google: no route", retryable=False,
                             account_wide=True)

    items = [restore.RestoreItem(remote_id=f"id{n}", rel_path=f"p{n}.jpg", root="",
                                 size=3, stored_size=3, sha256="0" * 64) for n in range(5)]
    job = restore.RestoreJob(connect=Unreachable, items=items, destination=tmp_path / "out")
    job.start()
    job.join(30)
    state = job.state.snapshot()
    assert state["failed"] == 0 and not state["finished"]
    assert "could not be used" in state["message"]


def test_a_file_refused_on_its_own_is_still_counted_and_the_rest_carry_on(tmp_path):
    class Refuses:
        def download_range(self, *args):
            raise DriveError("the file is no longer in Google Drive", 404)

    items = [restore.RestoreItem(remote_id=f"id{n}", rel_path=f"p{n}.jpg", root="",
                                 size=3, stored_size=3, sha256="0" * 64) for n in range(3)]
    job = restore.RestoreJob(connect=Refuses, items=items, destination=tmp_path / "out")
    job.start()
    job.join(30)
    assert job.state.snapshot()["failed"] == 3


# -- CL-6: the guardian's timing ---------------------------------------------

def _loop_once(guardian):
    """Run one pass of the guardian's loop, recording whether it checked."""
    ran = []
    guardian.run_once = lambda: ran.append(True)
    calls = {"n": 0}

    class OneWait:
        def wait(self, seconds):
            calls["n"] += 1
            return calls["n"] == 1 and False

        def is_set(self):
            return calls["n"] >= 2

    guardian._stop = OneWait()
    guardian._loop()
    return bool(ran)


def test_a_nearly_full_archive_disk_is_not_rehashed_every_five_minutes(monkeypatch):
    monkeypatch.setattr(guardian_mod.db, "load_guardian_state", lambda: {})
    g = guardian_mod.ArchiveGuardian(lambda: False)
    g._state = {"status": "warning", "checked_at": time.time() - 600}
    assert not _loop_once(g)


def test_a_check_that_could_not_finish_is_tried_again_soon(monkeypatch):
    monkeypatch.setattr(guardian_mod.db, "load_guardian_state", lambda: {})
    g = guardian_mod.ArchiveGuardian(lambda: False)
    g._state = {"status": "warning", "retry_soon": True, "checked_at": time.time() - 600}
    assert _loop_once(g)


def test_an_unplugged_archive_drive_is_looked_for_again_soon(monkeypatch, tmp_path):
    monkeypatch.setattr(guardian_mod.db, "load_guardian_state", lambda: {})
    monkeypatch.setattr(guardian_mod.db, "load_settings",
                        lambda: {"destination_dir": str(tmp_path / "unplugged")})
    monkeypatch.setattr(guardian_mod.db, "save_guardian_state", lambda state: None)
    g = guardian_mod.ArchiveGuardian(lambda: False)
    state = g.run_once()
    assert state["status"] == "critical" and state["retry_soon"] is True


# -- CL-7: reading the index copy back tries again --------------------------

def test_reading_the_index_copy_back_survives_one_slow_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(restore.time, "sleep", lambda s: None)
    calls = []

    class Flaky:
        def download_range(self, file_id, start, end):
            calls.append(start)
            if len(calls) == 1:
                raise DriveError("Google did not answer in time", retryable=True)
            return b"0123456789"[start:end + 1]

    with pytest.raises(Exception) as caught:
        index_copy.fetch(Flaky(), {"id": "x", "name": "ninaivu-index-a.tar.gz", "size": 10},
                         None, tmp_path / "work")
    assert "Google did not answer" not in str(caught.value)
    assert len(calls) == 2
    assert (tmp_path / "work" / "download.tar.gz").read_bytes() == b"0123456789"


# -- CL-8: the test restore checks against the checksum recorded at upload ---

def test_the_test_restore_keeps_the_recorded_md5():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_schema(conn)
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, size, state, remote_id, digest, md5) "
                 "VALUES('/lib', 'a.jpg', 10, ?, 'rid', '', 'abc')", (store.DONE,))
    items = restore_test.pick(conn, 1, 1 << 20)
    assert items and items[0].md5 == "abc"


# -- CL-9: disconnecting stays disconnected ----------------------------------

def test_a_client_made_before_disconnect_cannot_save_the_old_token_back(tmp_path):
    from ninaivu.cloud.service import CloudService

    cfg = types.SimpleNamespace(state_dir=str(tmp_path), cloud_folder_name="Ninaivu")
    service = CloudService(cfg, lambda: None)
    service.creds = Credentials(client_id="c", client_secret="s", refresh_token="old-token")
    service._save()
    client = service.client()
    client.transport = lambda *a, **k: (200, {}, b"")
    service.disconnect()
    client.on_change(client.creds)
    assert "old-token" not in (tmp_path / "google.json").read_text()


def test_google_refusing_to_revoke_is_said_out_loud(caplog):
    client = drive.DriveClient(creds=Credentials(refresh_token="t"),
                               transport=lambda *a, **k: (400, {}, b""))
    with caplog.at_level(logging.WARNING, logger="ninaivu.cloud.drive"):
        client.revoke()
    assert "HTTP 400" in caplog.text


# -- CL-10: one wait, not two -------------------------------------------------

def test_a_slow_down_in_the_one_at_a_time_path_is_waited_out_once(monkeypatch):
    monkeypatch.setattr(engine_mod.store, "pending_batch",
                        lambda conn, **kw: [{"root": "/r", "rel_path": "a.jpg"}])
    engine = engine_mod.SyncEngine(connect=lambda: object(), open_db=lambda: None)
    sleeps = []

    def one(conn, client, row):
        if len(sleeps) >= 1:
            engine._stop.set()
        return "wait"

    monkeypatch.setattr(engine, "_one", one)
    monkeypatch.setattr(engine, "_sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(engine, "_await_window", lambda: True)
    engine._run()
    waits = [s for s in sleeps if s != engine_mod.BREATH]
    assert waits == [engine_mod.BACKOFF_START, engine_mod.BACKOFF_START * 2]


# -- CL-11: temporary files are removed when writing fails -------------------

def test_a_status_kit_that_cannot_be_written_leaves_nothing_half_written(tmp_path, monkeypatch):
    def full_disk(destination):
        raise OSError(28, "No space left on device")
        yield  # pragma: no cover

    monkeypatch.setattr(status_kit, "archived_files", full_disk)
    with pytest.raises(OSError):
        status_kit.write_status_kit(str(tmp_path))
    assert not list(tmp_path.glob("*.writing"))


def test_older_unpacked_index_copies_are_removed(tmp_path, monkeypatch):
    from ninaivu.cloud.service import CloudService

    cfg = types.SimpleNamespace(state_dir=str(tmp_path), cloud_folder_name="Ninaivu")
    service = CloudService(cfg, lambda: None)
    old = tmp_path / "from-drive" / "old-tag"
    (old / "unpacked" / "state").mkdir(parents=True)
    (old / "ninaivu_backup_old.tar.gz").write_bytes(b"kept")

    def fetched(client, entry, key, work):
        (work / "unpacked" / "state").mkdir(parents=True)
        bundle = work / "index-copy.tar.gz"
        bundle.write_bytes(b"new")
        return work / "unpacked" / "state", bundle

    monkeypatch.setattr(index_copy, "fetch", fetched)
    service._index_copy_state(None, {"id": "new", "modifiedTime": "t"}, None)
    assert not (old / "unpacked").exists()
    assert (old / "ninaivu_backup_old.tar.gz").exists(), "the bundle itself is kept"


def test_a_leftover_test_restore_folder_is_cleared_by_the_next_test(tmp_path):
    leftover = tmp_path / "restore-test" / "20260101-000000" / ".ninaivu-restore"
    leftover.mkdir(parents=True)
    (leftover / "x.part").write_bytes(b"x")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_schema(conn)
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, size, state, remote_id) "
                 "VALUES('/lib', 'a.jpg', 1, ?, 'rid')", (store.DONE,))
    cfg = types.SimpleNamespace(state_dir=str(tmp_path))

    class NoDrive:
        def download_range(self, *args):
            raise DriveError("the file is no longer in Google Drive", 404)

        def file_info(self, *args):
            raise DriveError("the file is no longer in Google Drive", 404)

    service = types.SimpleNamespace(client=NoDrive,
                                    creds=types.SimpleNamespace(connected=True))
    tester = restore_test.RestoreTester(cfg, service, connect_db=lambda: conn)
    tester._run()
    assert not list((tmp_path / "restore-test").iterdir())


# -- CL-12: a malformed recovery file is a ValueError -------------------------

@pytest.mark.parametrize("document", [{}, {"key": 5}, {"key": "not base64!"}])
def test_a_malformed_recovery_file_is_refused_in_words(document):
    with pytest.raises(ValueError):
        keyring.key_from(recovery=document)


# -- CL-13: the Google connection file ----------------------------------------

def test_a_damaged_connection_file_is_reported(tmp_path, caplog):
    path = tmp_path / "google.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="ninaivu.cloud.drive"):
        creds = Credentials.load(path)
    assert not creds.connected and "could not be read" in caplog.text


def test_no_connection_file_is_quietly_not_connected(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="ninaivu.cloud.drive"):
        assert not Credentials.load(tmp_path / "google.json").connected
    assert not caplog.text


# -- CL-14: a commit lost in the archive database is said --------------------

def test_a_failed_archive_commit_is_logged(monkeypatch, caplog):
    from ninaivu.archive import database

    class Broken:
        def commit(self):
            raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(database._local, "conn", Broken(), raising=False)
    with caplog.at_level(logging.WARNING, logger="ninaivu.archive.database"):
        database.flush()
    assert "disk is full" in caplog.text
    monkeypatch.setattr(database._local, "conn", None, raising=False)


