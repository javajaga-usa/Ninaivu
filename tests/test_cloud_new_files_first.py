"""A file found during a long backup is queued when it is found.

The engine offered the library to the queue once, when a run had nothing left
to send. Here that was 2.2 TB away, three weeks at the pace Drive allows: a
photograph added on the first day waited for every video owed before it, and
an edited copy made on 23 September was still not queued four days later.
Queued when a scan finds it, a photograph goes ahead of every video still owed.
"""
from types import SimpleNamespace

import pytest

from fake_drive import FakeDrive
from ninaivu import Services
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import store
from ninaivu.storage import db


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


def _add(conn, root, rel, data):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    store.remember(conn, str(root), rel, size=len(data), filename=path.name)


def _run(db_path, client, **kwargs):
    engine = engine_mod.SyncEngine(connect=lambda: client,
                                   open_db=lambda: db.connect(db_path), **kwargs)
    engine.start()
    engine._thread.join(30)
    assert not engine.running, "the engine did not finish"
    return engine


def test_a_photograph_a_scan_finds_mid_run_goes_before_the_videos_owed(
        conn, db_path, client, fake, tmp_path):
    root = tmp_path / "lib"
    for n in range(3):
        _add(conn, root, f"2019/old{n}.jpg", b"P" * 2048)
    for n in range(12):
        _add(conn, root, f"2019/film{n:02}.mp4", b"V" * 4096)
    new = root / "2026" / "today.jpg"
    engine = None

    def scan_finds_it(_root, _rel):
        # The first file is going up when a scan indexes today's photograph.
        if not new.exists():
            new.parent.mkdir(parents=True)
            new.write_bytes(b"T" * 2048)
            engine.library_changed()
        return 1

    def requeue():
        if new.exists() and store.state_of(conn, str(root), "2026/today.jpg") is None:
            store.remember(conn, str(root), "2026/today.jpg", size=2048,
                           filename="today.jpg")
            return 1
        return 0

    engine = engine_mod.SyncEngine(connect=lambda: client,
                                   open_db=lambda: db.connect(db_path),
                                   visibility_of=scan_finds_it, requeue=requeue)
    engine.start()
    engine._thread.join(30)
    assert not engine.running

    sent = [u["name"] for u in fake.uploads]
    assert len(sent) == 16
    # The batch that was already in hand goes first; the next one starts with it.
    assert sent.index("today.jpg") == 10, sent
    assert sent[11:] == [f"film{n:02}.mp4" for n in range(7, 12)]


def test_a_long_run_looks_for_new_files_every_so_often(conn, db_path, client, fake,
                                                        tmp_path, monkeypatch):
    """Files that arrive other than by a scan (an approved upload, an edit) are
    queued within the hour."""
    monkeypatch.setattr(engine_mod, "REQUEUE_EVERY", 0.0)
    root = tmp_path / "lib"
    for n in range(25):
        _add(conn, root, f"2019/p{n:02}.jpg", b"P" * 1024)
    calls = []
    _run(db_path, client, requeue=lambda: calls.append(1) or 0)
    # Before each of the three batches and the empty fourth look, and once
    # more because nothing was left.
    assert len(calls) == 5


def test_a_run_looks_when_it_starts_and_when_it_has_nothing_left(conn, db_path, client,
                                                                  fake, tmp_path):
    """A run carried on after a restart would otherwise wait an hour for what
    arrived while Ninaivu was stopped."""
    root = tmp_path / "lib"
    for n in range(25):
        _add(conn, root, f"2019/p{n:02}.jpg", b"P" * 1024)
    calls = []
    _run(db_path, client, requeue=lambda: calls.append(len(fake.uploads)) or 0)
    assert calls == [0, 25]


def test_a_failing_look_does_not_stop_the_uploads(conn, db_path, client, fake,
                                                  tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "REQUEUE_EVERY", 0.0)
    root = tmp_path / "lib"
    _add(conn, root, "2019/a.jpg", b"A" * 1024)

    def broken():
        raise RuntimeError("index unreadable")

    _run(db_path, client, requeue=broken)
    assert [u["name"] for u in fake.uploads] == ["a.jpg"]


@pytest.mark.parametrize("payload,told", [
    ({"phase": "indexed", "added": 1, "updated": 0}, True),
    ({"phase": "done", "added": 0, "updated": 3}, True),
    ({"phase": "done", "added": 0, "updated": 0}, False),
    ({"phase": "progress", "added": 5, "updated": 0}, False),
])
def test_a_scan_that_found_something_tells_the_backup(payload, told):
    heard = []
    services = SimpleNamespace(cloud=SimpleNamespace(library_changed=lambda: heard.append(1)))
    Services._scan_found(services, payload)
    assert bool(heard) is told
