"""A drive that is not plugged in is not a library that has been deleted.

The engine asked one question of every queued file — is it there? — and read
"no" the same way whether the file had been deleted or the whole drive was
unplugged. So a run started with an external disk away marked every pending
file "the file is no longer there", found nothing left to send, called that
*finished*, and cleared the record saying an upload was still owed. The
console then reported a complete backup that had sent nothing, which is the
worst thing a backup can say.

What should happen instead: the folder is set aside for the run, its files
stay pending, any other library carries on, and the run ends saying why.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fake_drive import FakeDrive
from ninaivu.storage import db
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import store


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
    (root / "2019").mkdir(parents=True)
    for name in ("beach.jpg", "pier.jpg", "garden.jpg"):
        (root / "2019" / name).write_bytes(b"P" * 2048)
    return root


def queue_everything(conn, root: Path):
    for path in sorted(root.rglob("*.jpg")):
        store.remember(conn, str(root), path.relative_to(root).as_posix(),
                       size=path.stat().st_size, filename=path.name)


def build(db_path, client, **kwargs):
    return engine_mod.SyncEngine(
        connect=lambda: client, open_db=lambda: db.connect(db_path), **kwargs)


def run(engine, timeout=30):
    engine.start()
    engine._thread.join(timeout)
    assert not engine.running, "the engine did not finish"


def unplug(root: Path) -> None:
    """What an external drive being pulled out looks like to every path."""
    root.rename(root.parent / (root.name + "-unplugged"))


# -- the bug ----------------------------------------------------------------

def test_an_unplugged_drive_does_not_empty_the_queue(db_path, conn, client,
                                                     library):
    queue_everything(conn, library)
    assert store.summary(conn)["pending"] == 3
    unplug(library)

    run(build(db_path, client))

    after = store.summary(conn)
    assert after["pending"] == 3, "the files are still owed"
    assert after["skipped"] == 0, "nothing was deleted; nothing was skipped"
    assert after["done"] == 0


def test_an_unplugged_drive_is_not_a_finished_backup(db_path, conn, client,
                                                     library):
    """The record of an upload still owed must survive the run.

    `_on_finished` is what clears it, and it used to be called here — so a
    restart afterwards had nothing to carry on.
    """
    finished = []
    queue_everything(conn, library)
    unplug(library)

    run(build(db_path, client, on_finished=lambda: finished.append(True)))

    assert finished == [], "the run reported itself finished"


def test_the_run_says_which_drive_it_was(db_path, conn, client, library):
    """Plain words, in the state the console and the strip already read."""
    queue_everything(conn, library)
    root = str(library)
    unplug(library)

    engine = build(db_path, client)
    run(engine)

    state = engine.state.snapshot()
    assert state["offline_roots"] == [root]
    assert "not connected" in state["last_error"]
    # Not the Google account: nothing about the connection is broken.
    assert state["needs_reconnect"] is False


def test_plugging_it_back_in_carries_on(db_path, conn, client, library):
    queue_everything(conn, library)
    gone = library.parent / (library.name + "-unplugged")
    unplug(library)
    run(build(db_path, client))
    assert store.summary(conn)["pending"] == 3

    gone.rename(library)
    run(build(db_path, client))

    after = store.summary(conn)
    assert after["done"] == 3
    assert after["pending"] == 0


# -- what must not regress --------------------------------------------------

def test_a_file_deleted_from_a_library_that_is_there_is_still_skipped(
        db_path, conn, client, library):
    """The original behaviour, which is right when the folder is readable."""
    queue_everything(conn, library)
    (library / "2019" / "pier.jpg").unlink()

    run(build(db_path, client))

    after = store.summary(conn)
    assert after["skipped"] == 1
    assert after["done"] == 2
    assert store.state_of(conn, str(library), "2019/pier.jpg") == store.SKIPPED


def test_one_drive_away_does_not_stop_another_library(db_path, conn, client,
                                                      library, tmp_path):
    """The reason the folder is set aside rather than the run being stopped."""
    second = tmp_path / "lib2"
    second.mkdir()
    (second / "kitchen.jpg").write_bytes(b"K" * 2048)
    queue_everything(conn, library)
    queue_everything(conn, second)
    unplug(library)

    run(build(db_path, client))

    assert store.state_of(conn, str(second), "kitchen.jpg") == store.DONE
    assert store.summary(conn)["pending"] == 3        # the away drive's three


def test_an_empty_queue_with_every_drive_there_is_still_finished(
        db_path, conn, client, library):
    """The ordinary end of a run must not be caught by any of this."""
    finished = []
    queue_everything(conn, library)

    run(build(db_path, client, on_finished=lambda: finished.append(True)))

    assert finished == [True]
    assert store.summary(conn)["done"] == 3


# -- the batch that steps over it -------------------------------------------

def test_the_batch_steps_over_an_excluded_folder(conn):
    store.remember(conn, "/away", "a.jpg", size=10)
    store.remember(conn, "/here", "b.jpg", size=10)

    assert len(store.pending_batch(conn)) == 2
    rest = store.pending_batch(conn, exclude_roots={"/away"})
    assert [row["root"] for row in rest] == ["/here"]
    # Excluding is not forgetting: the row is untouched and still pending.
    assert store.state_of(conn, "/away", "a.jpg") == store.PENDING


def test_the_drive_is_not_asked_about_once_per_file(db_path, conn, client,
                                                    library, monkeypatch):
    """A failing drive can take seconds to answer, and the queue is long."""
    queue_everything(conn, library)
    unplug(library)

    asked = []
    real = Path.is_dir

    def counted(self):
        if str(self) == str(library):
            asked.append(str(self))
        return real(self)

    monkeypatch.setattr(Path, "is_dir", counted)
    run(build(db_path, client))
    assert len(asked) == 1, f"asked {len(asked)} times"
