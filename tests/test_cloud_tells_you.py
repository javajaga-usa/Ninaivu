"""A backup that stops has to say so.

`notify.py` has defined `cloud_failed` and `cloud_stalled` since it was
written, and nothing ever raised them. So when Google stopped accepting the
saved permission on a real installation, the upload stopped and stayed
stopped for seven hours — 125,732 files still waiting — and the only reason
anybody found out is that they opened the Cloud page and looked.

A backup that silently stops is worse than no backup, because the household
believes they have one. These pin the two moments worth interrupting somebody
for, and the several that are not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_drive import FakeDrive
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import store
from ninaivu.storage import db

ROOT = "/library"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "index.db"
    store.init_schema(db.init_db(path))
    return path


@pytest.fixture()
def conn(db_path):
    return db.connect(db_path)


@pytest.fixture()
def client():
    creds = drive_mod.Credentials(
        client_id="cid", client_secret="secret", refresh_token="r",
        access_token="a", expires_at=9e9, folder_name="Ninaivu")
    return drive_mod.DriveClient(creds=creds, transport=FakeDrive())


@pytest.fixture()
def library(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    (root / "beach.jpg").write_bytes(b"P" * 2048)
    return root


def told(db_path, connect, **kwargs):
    """Run an engine with its reports captured. Returns what it said.

    `connect` is what the engine calls to get a Drive client — a lambda
    returning one, or something that raises, which is how the account being
    refused is spelled here.
    """
    said: list[tuple[str, str, str]] = []
    engine = engine_mod.SyncEngine(
        connect=connect, open_db=lambda: db.connect(db_path),
        on_trouble=lambda *args: said.append(args), **kwargs)
    engine.start()
    engine._thread.join(30)
    assert not engine.running
    return said, engine


def queue(conn, root: Path):
    for path in sorted(root.rglob("*.jpg")):
        store.remember(conn, str(root), path.relative_to(root).as_posix(),
                       size=path.stat().st_size, filename=path.name)


# -- the two worth interrupting somebody for --------------------------------

def test_a_permission_google_will_not_accept_is_reported(db_path, conn,
                                                         library, client):
    """The one that happened. Seven hours of silence before anybody looked."""
    queue(conn, library)

    def refuses():
        raise drive_mod.NeedsReconnect("the saved permission was rejected")

    said, engine = told(db_path, refuses)
    assert [event for event, _, _ in said] == ["cloud_failed"]
    _, summary, detail = said[0]
    assert "stopped" in summary.lower()
    # What to do about it, not only what went wrong.
    assert "Connect the account again" in detail
    # And what did not happen: nothing already uploaded is at risk.
    assert "still there" in detail
    assert engine.state.snapshot()["needs_reconnect"] is True


def test_a_drive_that_is_not_plugged_in_is_reported_differently(db_path, conn,
                                                                library, client):
    """Not a failure and not the account: a drive somebody can plug in."""
    queue(conn, library)
    gone = library.parent / "lib-unplugged"
    library.rename(gone)

    said, _ = told(db_path, lambda: client)
    assert [event for event, _, _ in said] == ["cloud_stalled"]
    _, summary, detail = said[0]
    assert "waiting for a drive" in summary.lower()
    assert "still in the queue" in detail


# -- and the many that are not ----------------------------------------------

def test_an_ordinary_finished_run_says_nothing(db_path, conn, library, client):
    """This happens every night. One that arrives every night is not read."""
    queue(conn, library)
    said, _ = told(db_path, lambda: client)
    assert said == []


def test_an_empty_queue_says_nothing(db_path, client):
    said, _ = told(db_path, lambda: client)
    assert said == []


def test_a_notifier_that_throws_does_not_crash_the_backup(db_path, conn,
                                                          library, client):
    """"The backup stopped" must not become "the backup crashed"."""
    queue(conn, library)

    def refuses():
        raise drive_mod.NeedsReconnect("rejected")

    engine = engine_mod.SyncEngine(
        connect=refuses, open_db=lambda: db.connect(db_path),
        on_trouble=lambda *_: (_ for _ in ()).throw(RuntimeError("no")))
    engine.start()
    engine._thread.join(30)
    assert not engine.running
    assert engine.state.snapshot()["needs_reconnect"] is True


def test_an_engine_with_nobody_to_tell_still_runs(db_path, conn, library, client):
    """`on_trouble` is optional; the upload is not."""
    queue(conn, library)
    engine = engine_mod.SyncEngine(
        connect=lambda: client, open_db=lambda: db.connect(db_path))
    engine.start()
    engine._thread.join(30)
    assert store.summary(db.connect(db_path))["done"] == 1


# -- and the strip says so too ----------------------------------------------

def test_a_stopped_backup_is_on_the_activity_strip(app):
    """It showed only what was moving, so a stopped backup was invisible."""
    from ninaivu.server import activity

    services = app.config["MV_SERVICES"]

    class Halted:
        running = False

        def __init__(self):
            self.state = engine_mod.SyncState()

    stopped = Halted()
    stopped.state.update(needs_reconnect=True, last_error="rejected")
    services.cloud._engine = stopped
    try:
        jobs = {job["id"]: job for job in activity.running(services)}
        assert "cloud" in jobs, "a stopped backup is not nothing"
        assert jobs["cloud"]["paused"] is True
        assert "connect the account again" in jobs["cloud"]["detail"].lower()
    finally:
        services.cloud._engine = None


def test_a_backup_that_is_simply_off_is_not_on_the_strip(app):
    from ninaivu.server import activity

    services = app.config["MV_SERVICES"]

    class Idle:
        running = False

        def __init__(self):
            self.state = engine_mod.SyncState()

    services.cloud._engine = Idle()
    try:
        assert "cloud" not in {job["id"] for job in activity.running(services)}
    finally:
        services.cloud._engine = None
