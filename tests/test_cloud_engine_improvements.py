"""Tests for improvements made to the Google Cloud upload engine.

Covers:
- Extended MIME type resolution for modern media and camera RAW files
- Intra-chunk transient retry resilience
- Exception wrapping in _request for network/socket errors
- Batch queueing performance and correctness in store.queue_missing
- Live speed and ETA calculations in SyncState and CloudService
- Proactive expired upload session recovery
"""

import time
import pytest

from fake_drive import FakeDrive
from ninaivu.storage import db
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import store


# --- fixtures --------------------------------------------------------------

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


@pytest.fixture()
def client(fake):
    creds = drive_mod.Credentials(
        client_id="cid", client_secret="secret", refresh_token="r",
        access_token="a", expires_at=9e9, folder_name="Ninaivu")
    return drive_mod.DriveClient(creds=creds, transport=fake)


# --- MIME type tests -------------------------------------------------------

@pytest.mark.parametrize("filename,expected_mime", [
    ("photo.jpg", "image/jpeg"),
    ("photo.HEIC", "image/heic"),
    ("photo.heif", "image/heif"),
    ("photo.avif", "image/avif"),
    ("raw_shot.cr2", "image/x-canon-cr2"),
    ("raw_shot.CR3", "image/x-canon-cr3"),
    ("raw_shot.nef", "image/x-nikon-nef"),
    ("raw_shot.dng", "image/x-adobe-dng"),
    ("clip.mp4", "video/mp4"),
    ("clip.mkv", "video/x-matroska"),
    ("clip.mov", "video/quicktime"),
    ("clip.flv", "video/x-flv"),
    ("song.flac", "audio/flac"),
    ("song.opus", "audio/opus"),
    ("song.ogg", "audio/ogg"),
    ("song.m4a", "audio/mp4"),
    ("unknown.customxyz", "application/octet-stream"),
])
def test_expanded_mime_types(filename, expected_mime):
    assert drive_mod._mime(filename) == expected_mime


# --- Intra-chunk retry tests -----------------------------------------------

def test_intra_chunk_retry_recovers_on_transient_error(tmp_path, client, fake):
    """When a single chunk encounters a transient 503 or 429, intra-chunk retry
    recovers without bubbling up to abort the whole batch loop."""
    fpath = tmp_path / "video.mp4"
    fpath.write_bytes(b"V" * (1024 * 1024))

    root_id = client.ninaivu_root()
    session_url = client.begin_upload("video.mp4", root_id, len(fpath.read_bytes()))
    # Queue a transient 503 error on the first send_chunk attempt
    fake.fail_next = [503]

    remote_id = client.upload_file(fpath, "video.mp4", root_id, resume_url=session_url)
    assert remote_id
    assert fake.content("video.mp4") == fpath.read_bytes()


def test_request_exception_wrapping(monkeypatch):
    """Network, socket, and SSL exceptions are converted to retryable DriveError."""
    def mock_broken_opener():
        class MockOpener:
            def open(self, req, timeout=60):
                raise ConnectionResetError("Connection reset by peer")
        return MockOpener()

    monkeypatch.setattr(drive_mod, "_get_opener", mock_broken_opener)

    with pytest.raises(drive_mod.DriveError) as exc_info:
        drive_mod._request("GET", "https://www.googleapis.com/test")

    assert exc_info.value.retryable is True
    assert "network error communicating with Google" in str(exc_info.value)


# --- Bulk queueing performance & correctness -------------------------------

def test_batch_queue_missing_large_library(conn):
    """Bulk queueing thousands of assets is fast and populates correctly."""
    assets = [
        {"root": "/media", "rel_path": f"2023/{i:05d}.jpg", "size": 1000 + i, "filename": f"{i:05d}.jpg"}
        for i in range(2000)
    ]

    start = time.monotonic()
    added = store.queue_missing(conn, assets)
    elapsed = time.monotonic() - start

    assert added == 2000
    assert elapsed < 2.0  # Should be very fast (under 2 seconds for 2000 items)
    summary = store.summary(conn)
    assert summary["pending"] == 2000
    assert summary["done"] == 0

    # Re-queueing unchanged library adds 0 items
    re_added = store.queue_missing(conn, assets)
    assert re_added == 0


def test_batch_queue_missing_detects_changed_files(conn):
    """If a file size changes, bulk queueing detects it and re-queues."""
    assets = [{"root": "/media", "rel_path": "photo.jpg", "size": 1000, "filename": "photo.jpg"}]
    store.queue_missing(conn, assets)
    store.record_done(conn, "/media", "photo.jpg", remote_id="rem-1")

    # Offer same file, same size -> 0 added
    assert store.queue_missing(conn, assets) == 0

    # Offer same file, different size -> 1 added (re-queued)
    changed = [{"root": "/media", "rel_path": "photo.jpg", "size": 2500, "filename": "photo.jpg"}]
    assert store.queue_missing(conn, changed) == 1
    assert store.state_of(conn, "/media", "photo.jpg") == store.PENDING


# --- Live speed & ETA calculations -----------------------------------------

def test_sync_state_speed_and_eta():
    state = engine_mod.SyncState()
    assert state.snapshot()["speed_formatted"] == ""
    assert state.snapshot()["eta_seconds"] == 0.0

    # Simulate chunk progress
    state.current_total = 10 * 1024 * 1024  # 10 MB
    state.progress(1 * 1024 * 1024, state.current_total)
    time.sleep(0.05)
    state.progress(3 * 1024 * 1024, state.current_total)

    snap = state.snapshot()
    assert snap["speed_bps"] > 0
    assert "B/s" in snap["speed_formatted"]
    assert snap["eta_seconds"] >= 0.0

    state.reset_progress()
    snap_after = state.snapshot()
    assert snap_after["current_sent"] == 0
    assert snap_after["speed_bps"] == 0.0
    assert snap_after["speed_formatted"] == ""


# --- Expired session fast recovery -----------------------------------------

def test_resume_offset_expired_session_raises(fake):
    """When a session is expired, resume_offset raises a retryable DriveError."""
    fake.sessions["expired-sid"] = {"dead": True}
    creds = drive_mod.Credentials(client_id="cid", client_secret="sec")
    client = drive_mod.DriveClient(creds=creds, transport=fake)

    with pytest.raises(drive_mod.DriveError) as exc_info:
        client.resume_offset("https://upload.example/session/expired-sid", 1000)

    assert exc_info.value.status == 404
    assert exc_info.value.retryable is True


def test_idle_boost_adapts_mid_upload(tmp_path, client, fake, monkeypatch):
    from ninaivu.cloud import limits
    step = limits.STEP
    monkeypatch.setattr(engine_mod, 'CHUNK', step)
    monkeypatch.setattr(engine_mod, 'IDLE_CHUNK', 2 * step)
    idle = [True]
    engine = engine_mod.SyncEngine(connect=lambda: client, open_db=lambda: None,
                                   idle=lambda: idle[0])
    path = tmp_path / 'adaptive.mp4'
    payload = b'v' * (5 * step)
    path.write_bytes(payload)
    chunks = []
    def progress(sent, total):
        chunks.append(sent)
        idle[0] = False
    client.upload_file(path, path.name, client.ninaivu_root(),
                       chunk_size=engine._chunk_size, on_progress=progress)
    assert chunks == [2 * step, 3 * step, 4 * step, 5 * step]
    assert fake.content(path.name) == payload


def test_idle_boost_respects_live_cap():
    engine = engine_mod.SyncEngine(connect=lambda: None, open_db=lambda: None,
                                   idle=lambda: True)
    assert engine._chunk_size() == engine_mod.IDLE_CHUNK
    engine.rate_kbps = 512
    assert engine._chunk_size() == 512 * 1024
    engine.rate_kbps = 0
    assert engine._chunk_size() == engine_mod.IDLE_CHUNK
    def unavailable():
        raise RuntimeError('unavailable')
    engine._idle = unavailable
    assert engine._chunk_size() == engine_mod.CHUNK


def test_idle_probe_ignores_cloud_and_sleeping_jobs(monkeypatch):
    from ninaivu.server import activity
    job = {'paused': True}
    monkeypatch.setattr(activity, 'SOURCES', [activity._cloud, lambda s, c: job])
    assert not activity.other_work_active(None)
    job['paused'] = False
    assert activity.other_work_active(None)
    def broken(s, c):
        raise RuntimeError('unavailable')
    monkeypatch.setattr(activity, 'SOURCES', [broken])
    assert activity.other_work_active(None)
