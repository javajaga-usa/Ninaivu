"""Regressions for the cloud review: each test is one of the verified bugs.

They drive the real client, engine, restore and index copy through
``tests/fake_drive.py``, as the other cloud tests do, and wrap its transport
where Google's answer needs to be one the stand-in does not give on its own
(a 403 with a reason, a 401, a 308 without a Range header).
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fake_drive import FakeDrive
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import index_copy, keyring, restore, store
from ninaivu.storage import db

PASSPHRASE = "correct horse battery staple"


# --- fixtures --------------------------------------------------------------

@pytest.fixture(autouse=True)
def quick_backoff():
    original = engine_mod.BACKOFF_START
    engine_mod.BACKOFF_START = 0.05           # the wait itself is not the test
    yield
    engine_mod.BACKOFF_START = original


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "index.db"
    store.init_schema(db.init_db(path))
    return path


@pytest.fixture()
def conn(db_path):
    return db.connect(db_path)


@pytest.fixture()
def fake():
    return FakeDrive()


def make_client(transport, **creds):
    fields = dict(client_id="cid", client_secret="secret", refresh_token="r",
                  access_token="a", expires_at=9e9, folder_name="Ninaivu")
    fields.update(creds)
    return drive_mod.DriveClient(creds=drive_mod.Credentials(**fields), transport=transport)


@pytest.fixture()
def gdrive(fake):
    return make_client(fake)


@pytest.fixture()
def photos(tmp_path):
    root = tmp_path / "photos"
    (root / "2019").mkdir(parents=True)
    (root / "2019" / "beach.jpg").write_bytes(os.urandom(600 * 1024))
    (root / "2019" / "pier.jpg").write_bytes(os.urandom(4096))
    return root


def queue(conn, root: Path):
    for path in sorted(root.rglob("*.jpg")):
        store.remember(conn, str(root), path.relative_to(root).as_posix(),
                       size=path.stat().st_size, filename=path.name)


def run_engine(engine, timeout=60):
    engine.start()
    engine._thread.join(timeout)
    assert not engine.running, "the engine did not finish"


class Scripted:
    """The fake Drive, with some answers replaced by what a test needs."""

    def __init__(self, fake, answer):
        self.fake = fake
        self.answer = answer                  # (method, url, headers) -> reply or None
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=60.0):
        self.calls.append((method, url.split("?")[0], dict(headers or {})))
        reply = self.answer(method, url, headers or {})
        if reply is not None:
            return reply
        return self.fake(method, url, headers=headers, body=body, timeout=timeout)


def rate_limited(reason="userRateLimitExceeded"):
    return 403, {}, json.dumps({"error": {
        "code": 403, "message": "User Rate Limit Exceeded",
        "errors": [{"domain": "usageLimits", "reason": reason}]}}).encode()


# --- 1. the copy of the index in Drive ---------------------------------------

def test_with_no_record_the_older_slot_is_written_not_always_a():
    found = {"ninaivu-index-a": {"id": "A", "modifiedTime": "2026-09-20T00:00:00Z"},
             "ninaivu-index-b": {"id": "B", "modifiedTime": "2026-09-10T00:00:00Z"}}
    assert index_copy._choose_slot(found, {}) == "ninaivu-index-b"
    found["ninaivu-index-b"]["modifiedTime"] = "2026-09-25T00:00:00Z"
    assert index_copy._choose_slot(found, {}) == "ninaivu-index-a"


def test_a_record_drive_does_not_bear_out_is_not_trusted_for_the_slot():
    found = {"ninaivu-index-a": {"id": "A", "modifiedTime": "2026-09-20T00:00:00Z"},
             "ninaivu-index-b": {"id": "B", "modifiedTime": "2026-09-10T00:00:00Z"}}
    # This machine's own last write, borne out: the other slot.
    assert index_copy._choose_slot(found, {"slot": "ninaivu-index-b",
                                           "remote_id": "B"}) == "ninaivu-index-a"
    # A record naming a file that is not there: the older one, never the newest.
    assert index_copy._choose_slot(found, {"slot": "ninaivu-index-b",
                                           "remote_id": "gone"}) == "ninaivu-index-b"


def new_machine_copy(tmp_path, fake, gdrive):
    state = tmp_path / "state"
    state.mkdir()
    index = state / "index.db"
    store.init_schema(db.init_db(index))
    cfg = SimpleNamespace(state_dir=state, cloud_rate_kbps=0, cloud_index_every_hours=24,
                          cloud_enabled=True)
    service = SimpleNamespace(creds=gdrive.creds, client=lambda: gdrive,
                              _encryption=lambda: None)
    copy = index_copy.IndexCopy(cfg, service, connect_db=lambda: db.connect(index))
    return copy, db.connect(index)


def test_a_new_machine_does_not_replace_the_copy_already_in_drive(tmp_path, fake, gdrive):
    folder = gdrive.ensure_folder(index_copy.FOLDER, gdrive.ninaivu_root())
    good = tmp_path / "good.tar.gz"
    good.write_bytes(b"the household's real index")
    gdrive.upload_file(good, "ninaivu-index-a.tar.gz", folder)

    copy, conn = new_machine_copy(tmp_path, fake, gdrive)
    result = copy.run()
    assert not result["ok"] and "another computer" in result["error"]
    assert fake.content("ninaivu-index-a.tar.gz") == b"the household's real index"
    assert fake.updates == [] and len(fake.uploads) == 1

    # Once this machine has backed something up itself, it may send — and
    # into the empty slot, leaving the other copy as it was.
    store.remember(conn, "/lib", "a.jpg", size=1)
    store.record_done(conn, "/lib", "a.jpg", remote_id="x")
    result = copy.run()
    assert result["ok"], result
    assert result["slot"] == "ninaivu-index-b"
    assert fake.content("ninaivu-index-a.tar.gz") == b"the household's real index"


def test_a_machine_with_nothing_in_drive_still_sends_its_first_copy(tmp_path, fake, gdrive):
    copy, _ = new_machine_copy(tmp_path, fake, gdrive)
    result = copy.run()
    assert result["ok"], result
    assert result["slot"] == "ninaivu-index-a"


# --- 2. a restore from Drive writes only into this machine's libraries --------

def service_for(tmp_path, roots):
    from ninaivu.cloud.service import CloudService

    state = tmp_path / "svc-state"
    state.mkdir(exist_ok=True)
    cfg = SimpleNamespace(state_dir=state, cloud_folder_name="Ninaivu", roots=roots,
                          libraries=roots, cloud_encrypt=False, cloud_rate_kbps=0,
                          cloud_window_start="", cloud_window_end="")
    service = CloudService(cfg, lambda: db.connect(tmp_path / "index.db"))
    service.creds = drive_mod.Credentials(refresh_token="r")
    return service


def test_roots_from_the_index_copy_must_be_libraries_here(tmp_path, db_path):
    library = tmp_path / "Photos"
    library.mkdir()
    service = service_for(tmp_path, [str(library)])
    items = [restore.RestoreItem(remote_id="1", rel_path="a.jpg", root=str(library)),
             restore.RestoreItem(remote_id="2", rel_path="b.jpg", root="/etc")]
    assert service.roots_outside_libraries(items) == ["/etc"]
    assert service.roots_outside_libraries(items[:1]) == []

    service.index_found = {"bundle": "x", "roots": [str(library), "/etc"]}
    with pytest.raises(ValueError, match="Choose a folder"):
        service.restore_start(items, destination=None, key=None)
    assert service._restore is None, "a restore started anyway"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows compares paths without case")
def test_a_library_root_matches_whatever_its_case_on_windows(tmp_path, db_path):
    service = service_for(tmp_path, [r"D:\Photos"])
    items = [restore.RestoreItem(remote_id="1", rel_path="a.jpg", root=r"d:\photos\\")]
    assert service.roots_outside_libraries(items) == []


# --- 3 and 10. checksums for a resumed upload, and a restore that uses them ---

def resume_half_way(conn, gdrive, root: Path, rel: str) -> None:
    """Start an upload, send its first piece, and leave it as a crash would."""
    source = root / rel
    size = source.stat().st_size
    session = gdrive.begin_upload(source.name, gdrive.ninaivu_root(), size)
    gdrive.send_chunk(session, source.read_bytes()[:256 * 1024], 0, size)
    store.save_resume(conn, str(root), rel, session, encrypted=False, size=size)


def test_a_resumed_upload_records_drives_md5(conn, gdrive, fake, photos):
    queue(conn, photos)
    resume_half_way(conn, gdrive, photos, "2019/beach.jpg")
    engine = engine_mod.SyncEngine(connect=lambda: gdrive, open_db=lambda: conn)
    row = next(r for r in store.pending_batch(conn) if r["rel_path"] == "2019/beach.jpg")
    assert row["resume_url"]
    assert engine._one(conn, gdrive, row) == "sent"

    done = conn.execute("SELECT digest, md5 FROM cloud_uploads WHERE rel_path=?",
                        ("2019/beach.jpg",)).fetchone()
    expected = hashlib.md5((photos / "2019" / "beach.jpg").read_bytes()).hexdigest()
    assert done["digest"] == "", "a resumed upload cannot hash the whole file itself"
    assert done["md5"] == expected

    items = [i for i in restore.from_record(conn) if i.rel_path == "2019/beach.jpg"]
    assert items and items[0].md5 == expected


def test_a_damaged_download_of_a_resumed_upload_is_caught(conn, gdrive, fake, photos, tmp_path):
    queue(conn, photos)
    resume_half_way(conn, gdrive, photos, "2019/beach.jpg")
    engine = engine_mod.SyncEngine(connect=lambda: gdrive, open_db=lambda: conn)
    row = next(r for r in store.pending_batch(conn) if r["rel_path"] == "2019/beach.jpg")
    engine._one(conn, gdrive, row)

    fake.corrupt_download = True
    items = [i for i in restore.from_record(conn) if i.rel_path == "2019/beach.jpg"]
    job = restore.RestoreJob(connect=lambda: gdrive, items=items,
                             destination=tmp_path / "back")
    job.start()
    job.join(60)
    state = job.state.snapshot()
    assert state["failed"] == 1 and state["restored"] == 0, state


def test_drives_checksum_is_used_when_the_record_has_none(conn, gdrive, fake, photos, tmp_path):
    """An older record with no checksum at all: Drive's MD5 is asked for."""
    rel = "2019/pier.jpg"
    source = photos / rel
    remote = gdrive.upload_file(source, source.name, gdrive.ninaivu_root())
    store.remember(conn, str(photos), rel, size=source.stat().st_size, filename=source.name)
    store.record_done(conn, str(photos), rel, remote_id=remote)       # no digest, no md5
    fake.corrupt_download = True
    job = restore.RestoreJob(connect=lambda: gdrive, items=restore.from_record(conn),
                             destination=tmp_path / "back")
    job.start()
    job.join(60)
    assert job.state.snapshot()["failed"] == 1


def test_a_damaged_file_the_same_size_is_not_taken_as_already_there(conn, gdrive, fake,
                                                                   photos):
    rel = "2019/pier.jpg"
    source = photos / rel
    original = source.read_bytes()
    remote = gdrive.upload_file(source, source.name, gdrive.ninaivu_root())
    store.remember(conn, str(photos), rel, size=len(original), filename=source.name)
    store.record_done(conn, str(photos), rel, remote_id=remote)       # no checksum recorded
    source.write_bytes(bytes(b ^ 0x55 for b in original))            # same size, damaged
    job = restore.RestoreJob(connect=lambda: gdrive, items=restore.from_record(conn),
                             destination=None)
    job.start()
    job.join(60)
    state = job.state.snapshot()
    assert state["already_there"] == 0 and state["beside"] == 1, state
    assert (photos / "2019" / "pier (restored).jpg").read_bytes() == original


# --- 4. rate limits and expired tokens -----------------------------------------

def test_a_403_rate_limit_is_retryable_and_a_plain_403_is_not():
    limited = drive_mod._failure(*rate_limited()[::2], "refused")
    assert limited.retryable and limited.account_wide and limited.status == 403
    newer = drive_mod._failure(403, {"error": {"details": [
        {"reason": "RATE_LIMIT_EXCEEDED"}]}}, "refused")
    assert newer.retryable and newer.account_wide
    full = drive_mod._failure(*rate_limited("storageQuotaExceeded")[::2], "refused")
    assert not full.retryable and full.account_wide
    plain = drive_mod._failure(403, {"error": {"message": "forbidden"}}, "refused")
    assert not plain.retryable and not plain.account_wide


def test_a_rate_limited_account_does_not_fail_the_files(db_path, conn, fake, photos):
    refusals = {"left": 3}

    def answer(method, url, headers):
        if method == "POST" and url.startswith(drive_mod.UPLOAD_URL) and refusals["left"]:
            refusals["left"] -= 1
            return rate_limited()
        return None

    gdrive = make_client(Scripted(fake, answer))
    queue(conn, photos)
    run_engine(engine_mod.SyncEngine(connect=lambda: gdrive,
                                     open_db=lambda: db.connect(db_path)))
    rows = store.recent(conn)
    assert all(r["state"] == store.DONE for r in rows), rows
    assert all(r["attempts"] == 0 for r in rows), "a rate limit was charged to the files"


def test_a_401_renews_the_token_once_and_carries_on(fake):
    first = {"refused": False}

    def answer(method, url, headers):
        if url.startswith(drive_mod.API_URL) and not first["refused"]:
            first["refused"] = True
            return 401, {}, b'{"error": {"message": "Invalid Credentials"}}'
        return None

    scripted = Scripted(fake, answer)
    gdrive = make_client(scripted)
    assert gdrive.ninaivu_root()
    assert any(url == drive_mod.TOKEN_URL for _, url, _ in scripted.calls), "no renewal"
    assert gdrive.creds.access_token != "a"


def test_a_401_after_renewing_needs_a_person(fake):
    def answer(method, url, headers):
        if url.startswith(drive_mod.API_URL):
            return 401, {}, b'{"error": {"message": "Invalid Credentials"}}'
        return None

    gdrive = make_client(Scripted(fake, answer))
    with pytest.raises(drive_mod.NeedsReconnect):
        gdrive.ninaivu_root()


def test_a_lost_permission_is_not_charged_to_the_file(db_path, conn, fake, photos):
    fake.invalid_grant = True
    gdrive = make_client(fake, expires_at=0)
    queue(conn, photos)
    engine = engine_mod.SyncEngine(connect=lambda: gdrive, open_db=lambda: db.connect(db_path))
    run_engine(engine)
    assert engine.state.snapshot()["needs_reconnect"]
    assert all(r["attempts"] == 0 for r in store.recent(conn))


# --- 5. a session opened for other bytes is not resumed ---------------------------

def test_a_session_for_ciphertext_is_not_resumed_with_plain_bytes(conn, gdrive, photos,
                                                                  monkeypatch):
    queue(conn, photos)
    rel = "2019/pier.jpg"
    store.save_resume(conn, str(photos), rel, "https://upload.example/session/old",
                      encrypted=True, size=4096 + 36)
    row = next(r for r in store.pending_batch(conn) if r["rel_path"] == rel)
    engine = engine_mod.SyncEngine(connect=lambda: gdrive, open_db=lambda: conn)
    sent = {}

    def upload_file(path, name, parent, **kwargs):
        sent.update(resume=kwargs.get("resume_url"), name=name)
        return "file-new"

    monkeypatch.setattr(gdrive, "upload_file", upload_file)
    monkeypatch.setattr(engine, "_folder", lambda c, r: "folder")
    assert engine._one(conn, gdrive, row) == "sent"
    assert sent["resume"] == "", "plain bytes were offered to a session made for ciphertext"
    assert sent["name"] == "pier.jpg"


def test_a_session_for_the_same_bytes_is_resumed(conn, gdrive, photos, monkeypatch):
    queue(conn, photos)
    rel = "2019/pier.jpg"
    store.save_resume(conn, str(photos), rel, "https://upload.example/session/same",
                      encrypted=False, size=4096)
    row = next(r for r in store.pending_batch(conn) if r["rel_path"] == rel)
    engine = engine_mod.SyncEngine(connect=lambda: gdrive, open_db=lambda: conn)
    sent = {}
    monkeypatch.setattr(gdrive, "upload_file",
                        lambda path, name, parent, **kw: sent.update(resume=kw.get("resume_url"))
                        or "file-same")
    monkeypatch.setattr(engine, "_folder", lambda c, r: "folder")
    assert engine._one(conn, gdrive, row) == "sent"
    assert sent["resume"] == "https://upload.example/session/same"


# --- 6. the key settings follow the Drive folder ------------------------------------

def test_a_new_drive_folder_gets_the_key_settings_too(tmp_path, db_path, fake):
    from ninaivu.cloud.service import CloudService

    state = tmp_path / "key-state"
    state.mkdir()
    keyring.create(state, PASSPHRASE, PASSPHRASE)
    cfg = SimpleNamespace(state_dir=state, cloud_folder_name="Ninaivu", cloud_encrypt=True,
                          cloud_rate_kbps=0, cloud_window_start="", cloud_window_end="")
    service = CloudService(cfg, lambda: db.connect(db_path))
    gdrive = make_client(fake)

    service._publish_params(gdrive)
    service._publish_params(gdrive)
    assert fake.uploaded_names().count(restore.PARAMS_NAME) == 1

    gdrive.creds.folder_name, gdrive.creds.folder_id = "Family photos", ""
    service._publish_params(gdrive)
    service._publish_params(gdrive)
    assert fake.uploaded_names().count(restore.PARAMS_NAME) == 2
    assert restore.find_params(gdrive) is not None, "the new folder has no key settings"


# --- 7. the sign-in file is never readable by others ----------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_credentials_file_is_created_owner_only(tmp_path):
    target = tmp_path / "google.json"
    stale = target.with_suffix(".json.tmp")
    stale.write_text("left by an interrupted save")
    os.chmod(stale, 0o644)
    old = os.umask(0)
    try:
        drive_mod.Credentials(refresh_token="SECRET").save(target)
    finally:
        os.umask(old)
    assert target.stat().st_mode & 0o777 == 0o600
    assert drive_mod.Credentials.load(target).refresh_token == "SECRET"
    assert not stale.exists()


# --- 8. restoring the index copy keeps what it does not carry ---------------------------

def test_restoring_the_index_copy_keeps_this_machines_keys_and_secrets(tmp_path):
    from ninaivu.cli import backup_restore as tool

    existing, extracted, candidate = tmp_path / "live", tmp_path / "bundle", tmp_path / "new"
    (existing / "pending-uploads" / "k").mkdir(parents=True)
    (existing / "pending-uploads" / "k" / "photo.jpg").write_bytes(b"not yet in the library")
    for name in ("ninaivu-ca.key", "ninaivu.key", "ninaivu.crt", "google.json",
                 "cloud-encryption.json"):
        (existing / name).write_text(f"this machine's {name}")
    (existing / "config.json").write_text(json.dumps(
        {"notify_smtp_password": "hunter2", "notify_webhook": "https://ntfy.sh/x",
         "port": 8000}))
    (existing / "index.db").write_bytes(b"old index")

    extracted.mkdir()
    (extracted / "index.db").write_bytes(b"index from drive")
    (extracted / "config.json").write_text(json.dumps(
        {"notify_smtp_password": "", "notify_webhook": "", "port": 9000}))
    (extracted / "backup_manifest.json").write_text(json.dumps(
        {"kind": index_copy.KIND, "files": {}}))

    tool._prepare_replacement(existing, extracted, candidate)
    assert (candidate / "index.db").read_bytes() == b"index from drive"
    for name in ("ninaivu-ca.key", "ninaivu.key", "ninaivu.crt", "google.json",
                 "cloud-encryption.json"):
        assert (candidate / name).read_text() == f"this machine's {name}", name
    assert (candidate / "pending-uploads" / "k" / "photo.jpg").is_file()
    settings = json.loads((candidate / "config.json").read_text())
    assert settings == {"notify_smtp_password": "hunter2",
                        "notify_webhook": "https://ntfy.sh/x", "port": 9000}


def test_a_full_backup_still_replaces_what_it_carries(tmp_path):
    from ninaivu.cli import backup_restore as tool

    existing, extracted, candidate = tmp_path / "live", tmp_path / "bundle", tmp_path / "new"
    existing.mkdir()
    (existing / "ninaivu-ca.key").write_text("this machine's")
    (existing / "config.json").write_text(json.dumps({"notify_smtp_password": "hunter2"}))
    extracted.mkdir()
    (extracted / "index.db").write_bytes(b"index")
    (extracted / "config.json").write_text(json.dumps({"notify_smtp_password": ""}))
    tool._prepare_replacement(existing, extracted, candidate)
    assert not (candidate / "ninaivu-ca.key").exists()
    assert json.loads((candidate / "config.json").read_text()) == {"notify_smtp_password": ""}


# --- 9. a 308 without a Range header ------------------------------------------------------

def test_a_308_without_range_is_no_progress():
    def transport(method, url, *, headers=None, body=None, timeout=60.0):
        return 308, {}, b""

    gdrive = make_client(transport)
    done, offset, remote = gdrive.send_chunk("https://upload.example/session/s",
                                             b"x" * 1024, 4096, 10_000)
    assert (done, offset, remote) == (False, 4096, "")


def test_a_session_that_never_keeps_anything_gives_up(tmp_path, fake):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"v" * 2048)

    def answer(method, url, headers):
        if method == "PUT" and url.startswith("https://upload.example/session/"):
            return 308, {}, b""
        return None

    gdrive = make_client(Scripted(fake, answer))
    with pytest.raises(drive_mod.DriveError) as caught:
        gdrive.upload_file(source, source.name, gdrive.ninaivu_root(), chunk_size=1024)
    assert caught.value.retryable
