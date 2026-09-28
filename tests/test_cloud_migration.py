"""After a move to another computer, the cloud backup carries on — it does not
start again.

The upload record decides whether a file has changed by its size and time. A
library copied to a new machine without its timestamps looked edited, file by
file, and the whole of it went to Drive a second time: 1.6 TB, days of
uploading, and a duplicate of everything. These check that the contents are
asked before anything is sent again.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from fake_drive import FakeDrive
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import store
from ninaivu.storage import db
from ninaivu.utils import source_version as sv

LONG_AGO = 1_500_000_000.0


@pytest.fixture()
def world(tmp_path):
    db_path = tmp_path / "index.db"
    store.init_schema(db.init_db(db_path))
    fake = FakeDrive()
    client = drive_mod.DriveClient(
        creds=drive_mod.Credentials(client_id="c", client_secret="s", refresh_token="r",
                                    access_token="a", expires_at=9e9),
        transport=fake)
    library = tmp_path / "old-disk" / "MasterArchive"
    (library / "2019" / "07").mkdir(parents=True)
    files = {"2019/07/a.jpg": os.urandom(200_000), "2019/07/b.jpg": os.urandom(3_000),
             "2019/c.mp4": os.urandom(150_000)}
    for rel, data in files.items():
        (library / rel).write_bytes(data)
        os.utime(library / rel, (LONG_AGO, LONG_AGO))
    return db_path, fake, client, library, files, tmp_path


def assets(root: Path, files):
    """What the index would say about each file now."""
    for rel in files:
        st = (root / rel).stat()
        yield {"root": str(root), "rel_path": rel, "size": st.st_size,
               "mtime": st.st_mtime, "filename": Path(rel).name, "kind": ""}


def upload_everything(world):
    db_path, fake, client, library, files, _ = world
    conn = db.connect(db_path)
    store.queue_missing(conn, assets(library, files))
    engine = engine_mod.SyncEngine(connect=lambda: client,
                                   open_db=lambda: db.connect(db_path))
    engine.start()
    engine._thread.join(30)
    assert store.summary(conn)["done"] == len(files)
    return conn


def move_without_timestamps(world):
    """A copy to a new disk that keeps the bytes and loses the times, then the
    record moved to the new folder as tools/reroot_library.py does."""
    db_path, _, _, library, files, tmp_path = world
    new = tmp_path / "new-disk" / "MasterArchive"
    for rel in files:
        (new / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(library / rel, new / rel)        # no copystat: new times
    conn = db.connect(db_path)
    conn.execute("UPDATE cloud_uploads SET root=?", (str(new),))
    conn.commit()
    return new


# -- the move ------------------------------------------------------------------

def test_a_library_moved_without_its_timestamps_is_not_sent_again(world):
    conn = upload_everything(world)
    sent = len(world[1].uploads)
    new = move_without_timestamps(world)

    queued = store.queue_missing(conn, assets(new, world[4]))

    assert queued == 0, "the whole library would have gone up a second time"
    assert store.summary(conn)["done"] == len(world[4])
    assert len(world[1].uploads) == sent


def test_the_new_timestamps_are_remembered_so_the_files_are_read_once(world, monkeypatch):
    conn = upload_everything(world)
    new = move_without_timestamps(world)
    store.queue_missing(conn, assets(new, world[4]))

    reads = []
    real = sv.same_content
    monkeypatch.setattr(sv, "same_content", lambda *a: reads.append(a) or real(*a))
    store.queue_missing(conn, assets(new, world[4]))
    assert reads == [], "every file was read again on the next pass"


def test_an_engine_run_after_the_move_sends_nothing(world):
    db_path, fake, client, _, files, _ = world
    conn = upload_everything(world)
    new = move_without_timestamps(world)
    store.queue_missing(conn, assets(new, files))
    sent = len(fake.uploads)
    engine = engine_mod.SyncEngine(connect=lambda: client,
                                   open_db=lambda: db.connect(db_path))
    engine.start()
    engine._thread.join(30)
    assert len(fake.uploads) == sent


# -- what still goes up ------------------------------------------------------------

def test_a_real_edit_at_the_same_size_is_still_sent(world):
    conn = upload_everything(world)
    _, _, _, library, files, _ = world
    target = library / "2019/07/b.jpg"
    target.write_bytes(os.urandom(len(files["2019/07/b.jpg"])))     # same size, new bytes
    assert store.queue_missing(conn, assets(library, files)) == 1
    assert store.state_of(conn, str(library), "2019/07/b.jpg") == store.PENDING


def test_a_file_that_grew_is_sent_without_being_read(world, monkeypatch):
    conn = upload_everything(world)
    _, _, _, library, files, _ = world
    with open(library / "2019/c.mp4", "ab") as handle:
        handle.write(b"more")
    monkeypatch.setattr(sv, "same_content", lambda *a: pytest.fail("read needlessly"))
    assert store.queue_missing(conn, assets(library, files)) == 1


def test_a_file_that_cannot_be_read_now_is_left_as_it_was(world, monkeypatch):
    """Unplugged, or locked: no answer is not a reason to send it again."""
    conn = upload_everything(world)
    _, _, _, library, files, _ = world
    rows = list(assets(library, files))
    for row in rows:
        row["mtime"] += 3600
    monkeypatch.setattr(sv, "same_content", lambda *a: None)
    assert store.queue_missing(conn, rows) == 0
    assert store.summary(conn)["done"] == len(files)


def test_a_record_from_before_content_stamps_falls_back_to_the_timestamp(world):
    conn = upload_everything(world)
    _, _, _, library, files, _ = world
    conn.execute("UPDATE cloud_uploads SET source_version=''")
    conn.commit()
    rows = list(assets(library, files))
    for row in rows:
        row["mtime"] += 3600
    assert store.queue_missing(conn, rows) == len(files)


# -- the stamp itself -----------------------------------------------------------------

def test_the_content_stamp_ignores_where_and_when(tmp_path):
    one = tmp_path / "one.jpg"
    one.write_bytes(os.urandom(300_000))
    version = sv.source_version(one)
    two = tmp_path / "elsewhere" / "two.jpg"
    two.parent.mkdir()
    shutil.copyfile(one, two)
    os.utime(two, (time.time() + 99, time.time() + 99))
    assert sv.source_version(two) != version          # the full stamp differs
    assert sv.same_content(version, two) is True      # the contents do not
    data = bytearray(two.read_bytes())
    data[-1] ^= 0xFF
    two.write_bytes(bytes(data))
    assert sv.same_content(version, two) is False
    assert sv.same_content("", two) is None
    assert sv.same_content(version, tmp_path / "gone.jpg") is None
