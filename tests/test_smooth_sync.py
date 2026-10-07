"""Backups and copies that do no needless work and wait for nothing.

For a library of a few hundred thousand files on a Raspberry Pi with slow USB
disks and a home connection, the cost is in what is done again: a manifest of
a hundred megabytes sent every two hundred files, a file written out in full
only to find the same bytes were there, a day's wait after a run that never
got going, a whole cloud record held in a dict to find nothing new.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud import offsite as offsite_mod
from ninaivu.cloud import restore, store
from ninaivu.cloud.offsite import MANIFEST, MANIFEST_PREV, Offsite
from ninaivu.storage import db
from ninaivu.storage import mirror as mirror_mod
from ninaivu.utils.source_version import source_version


# -- the off-site copy --------------------------------------------------------

@pytest.fixture()
def off(scanned, tmp_path):
    pytest.importorskip("cryptography")
    from ninaivu.cloud import keyring
    cfg, conn, _ = scanned
    keyring.create(cfg.state_dir, "correct horse battery", "correct horse battery")
    cfg.offsite_kind = "folder"
    cfg.offsite_folder = str(tmp_path / "disk-1")
    Path(cfg.offsite_folder).mkdir()
    return SimpleNamespace(cfg=cfg, conn=conn, off=Offsite(cfg, lambda: db.connect(cfg.db_path)))


def _offsite_run(off):
    off.start()
    off.join(60)
    return off.status()


@pytest.fixture()
def puts(monkeypatch):
    sent: list[str] = []
    real = offsite_mod.FolderTarget.put_file

    def put_file(self, name, source, on_bytes=None, stop=None):
        sent.append(name)
        return real(self, name, source, on_bytes, stop)

    monkeypatch.setattr(offsite_mod.FolderTarget, "put_file", put_file)
    return sent


def test_the_manifest_is_written_once_a_run_and_not_at_all_when_nothing_changed(off, puts):
    first = _offsite_run(off.off)
    assert not first["error"] and first["sent"] > 1
    assert puts.count(MANIFEST) == 1, "written at the end, not every few files"

    puts.clear()
    second = _offsite_run(off.off)
    assert not second["error"] and second["sent"] == 0
    assert puts.count(MANIFEST) == 0, "a run that sent nothing rewrote the manifest"
    assert puts.count(MANIFEST_PREV) == 1, "kept once as the one before, there being none"

    puts.clear()
    _offsite_run(off.off)
    assert puts == [], "nothing at all is uploaded when nothing changed"


def test_the_manifest_is_written_part_way_on_a_timer(off, puts):
    off.off.MANIFEST_EVERY = 0          # every file, to see the timer at work
    status = _offsite_run(off.off)
    assert puts.count(MANIFEST) == status["sent"], "the end adds no extra copy"


def test_a_fresh_destination_is_not_asked_about_each_file_first(off, monkeypatch, puts):
    asked: Counter[str] = Counter()
    real = offsite_mod.FolderTarget.exists

    def exists(self, name):
        if name.startswith("files/"):
            asked[name] += 1
        return real(self, name)

    monkeypatch.setattr(offsite_mod.FolderTarget, "exists", exists)
    status = _offsite_run(off.off)
    assert status["sent"] == len(asked) > 0
    assert set(asked.values()) == {1}, "only the check after each upload"

    # Not fresh any more: a file the record forgot is looked for, found, and
    # not uploaded again.
    off.conn.execute("DELETE FROM offsite_copies WHERE rel_path LIKE '%shot1.jpg'")
    off.conn.commit()
    asked.clear()
    puts.clear()
    again = _offsite_run(off.off)
    assert not again["error"] and sum(asked.values()) >= 1
    assert not [name for name in puts if name.startswith("files/")]


# -- a restore from Drive -----------------------------------------------------

def _encrypted_item(path: Path, version: str) -> restore.RestoreItem:
    st = path.stat()
    return restore.RestoreItem(remote_id="r1", rel_path=path.name, size=st.st_size,
                               encrypted=True, mtime=st.st_mtime, source_version=version)


def test_an_encrypted_file_already_there_is_not_downloaded_again(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    photo = dest / "a.jpg"
    photo.write_bytes(b"x" * 200_000)
    item = _encrypted_item(photo, source_version(photo))
    job = restore.RestoreJob(connect=lambda: None, items=[item], destination=dest,
                             key=b"k" * 32)
    job._download = lambda *a: pytest.fail("downloaded a file that was already there")
    job._one(None, item)
    assert job.state.snapshot()["already_there"] == 1


def test_an_encrypted_file_that_differs_is_still_fetched(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    photo = dest / "a.jpg"
    photo.write_bytes(b"x" * 200_000)
    version = source_version(photo)
    st = photo.stat()
    photo.write_bytes(b"x" * 199_999 + b"y")          # damaged, same size
    os.utime(photo, ns=(st.st_atime_ns, st.st_mtime_ns))
    item = _encrypted_item(photo, version)

    class Fetched(Exception):
        pass

    def download(*_args):
        raise Fetched()

    job = restore.RestoreJob(connect=lambda: None, items=[item], destination=dest,
                             key=b"k" * 32)
    job._download = download
    with pytest.raises(Fetched):
        job._one(None, item)


# -- cloud uploads, several at once ------------------------------------------

def test_one_big_file_does_not_leave_the_other_workers_idle(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    store.init_schema(conn)
    for n in range(30):
        store.remember(conn, "R", f"p{n:02}.jpg", size=10, filename=f"p{n:02}.jpg")
    big = "p00.jpg"
    lock = threading.Lock()
    active: set[str] = set()
    starts: Counter[str] = Counter()
    seen = SimpleNamespace(most=0, small=0)
    plenty_done = threading.Event()

    def one(conn, client, row):
        rel = row["rel_path"]
        with lock:
            assert rel not in active, f"{rel} sent twice at once"
            active.add(rel)
            starts[rel] += 1
            seen.most = max(seen.most, len(active))
        try:
            if rel == big:
                # Done only once twenty others have gone past it. Waiting for
                # the batch to drain would leave this waiting for ever.
                assert plenty_done.wait(10), "the others waited for the big one"
            else:
                time.sleep(0.01)
                with lock:
                    seen.small += 1
                    if seen.small >= 20:
                        plenty_done.set()
            conn.execute("UPDATE cloud_uploads SET state='done' WHERE rel_path=?", (rel,))
            conn.commit()
            return "sent"
        finally:
            with lock:
                active.discard(rel)

    engine = engine_mod.SyncEngine(connect=lambda: None, open_db=lambda: db.connect(path),
                                   idle=lambda: True, parallel=3)
    engine._one = one
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = engine._send_together(pool, None, store.pending_batch(conn, limit=9),
                                        conn=db.connect(path), offline=set())
    assert len(results) == 30 and {o for _, o in results} == {"sent"}
    assert set(starts.values()) == {1}, "a file was sent twice"
    assert seen.most <= 3


def test_refilling_stops_when_it_is_time_to_look_for_new_files(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    store.init_schema(conn)
    for n in range(20):
        store.remember(conn, "R", f"p{n:02}.jpg", size=10, filename=f"p{n:02}.jpg")

    def one(conn, client, row):
        conn.execute("UPDATE cloud_uploads SET state='done' WHERE id=?", (row["id"],))
        conn.commit()
        return "sent"

    engine = engine_mod.SyncEngine(connect=lambda: None, open_db=lambda: db.connect(path),
                                   idle=lambda: True, parallel=2)
    engine._one = one
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = engine._send_together(pool, None, store.pending_batch(conn, limit=4),
                                        conn=db.connect(path), offline=set(),
                                        until=lambda: True)
    assert len(results) == 4, "the batch it was given, and no more"


# -- queueing the library for the cloud --------------------------------------

INDEX_SQL = "SELECT id, root, rel_path, filename, size, mtime, kind FROM assets WHERE trashed=0"


def _asset(conn, root, rel, size, mtime, trashed=0):
    conn.execute("INSERT INTO assets(root, rel_path, filename, kind, size, mtime, trashed) "
                 "VALUES(?,?,?,?,?,?,?)", (root, rel, rel.rsplit("/", 1)[-1], "picture",
                                            size, mtime, trashed))


def _record(conn, root, rel, size, mtime, state="done", version="", resume=""):
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, filename, size, state, "
                 "source_mtime, source_version, resume_url, kind) VALUES(?,?,?,?,?,?,?,?,?)",
                 (root, rel, rel, size, state, mtime, version, resume, "picture"))


def test_queueing_in_sqlite_matches_queueing_in_python(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    root = str(lib)

    def file(name, data):
        (lib / name).write_bytes(data)
        return len(data)

    conn = db.init_db(tmp_path / "a.db")
    store.init_schema(conn)
    # New, unchanged, bigger, merely touched, edited in place, no timestamp,
    # set aside, in flight, recorded without a timestamp, trashed.
    _asset(conn, root, "new.jpg", file("new.jpg", b"n" * 50), 100.0)
    _asset(conn, root, "same.jpg", 40, 100.0)
    _record(conn, root, "same.jpg", 40, 100.0)
    _asset(conn, root, "bigger.jpg", 80, 100.0)
    _record(conn, root, "bigger.jpg", 40, 100.0, resume="https://resume")
    size = file("touched.jpg", b"t" * 300)
    _asset(conn, root, "touched.jpg", size, 200.0)
    _record(conn, root, "touched.jpg", size, 100.0, version=source_version(lib / "touched.jpg"))
    file("edited.jpg", b"e" * 300)
    old = source_version(lib / "edited.jpg")
    file("edited.jpg", b"E" * 300)
    _asset(conn, root, "edited.jpg", 300, 200.0)
    _record(conn, root, "edited.jpg", 300, 100.0, version=old)
    size = file("nomtime.jpg", b"m" * 70)
    _asset(conn, root, "nomtime.jpg", size, 0)
    _record(conn, root, "nomtime.jpg", size, 100.0, version=source_version(lib / "nomtime.jpg"))
    _asset(conn, root, "aside.jpg", 40, 100.0)
    _record(conn, root, "aside.jpg", 40, 100.0, state="held")
    _asset(conn, root, "flying.jpg", 40, 100.0)
    _record(conn, root, "flying.jpg", 40, 100.0, state="uploading", resume="https://r")
    _asset(conn, root, "unstamped.jpg", 40, 100.0)
    _record(conn, root, "unstamped.jpg", 40, 0)
    _asset(conn, root, "binned.jpg", 40, 100.0, trashed=1)
    for n in range(30):
        _asset(conn, root, f"bulk/{n}.jpg", 10 + n, 50.0 + n)
        if n % 3:
            _record(conn, root, f"bulk/{n}.jpg", 10 + n, 50.0 + n)
    conn.commit()
    other = sqlite3.connect(tmp_path / "b.db")
    conn.backup(other)
    other.close()
    other = db.connect(tmp_path / "b.db")

    in_python = store.queue_missing(
        conn, [dict(r) for r in conn.execute(INDEX_SQL + " ORDER BY id")])
    in_sqlite = store.queue_changed(other, INDEX_SQL, [])
    assert in_python == in_sqlite > 0

    columns = ("root, rel_path, filename, size, state, source_mtime, source_version, "
               "resume_url, error, attempts, kind")

    def table(c):
        return [tuple(r) for r in c.execute(
            f"SELECT {columns} FROM cloud_uploads ORDER BY root, rel_path")]

    assert table(conn) == table(other)


def test_queueing_hands_python_only_what_changed(tmp_path, monkeypatch):
    conn = db.init_db(tmp_path / "a.db")
    store.init_schema(conn)
    for n in range(50):
        _asset(conn, "R", f"{n}.jpg", 10, 100.0)
        _record(conn, "R", f"{n}.jpg", 10, 100.0)
    _asset(conn, "R", "new.jpg", 10, 100.0)
    conn.commit()
    monkeypatch.setattr(store, "CHANGED_PAGE", 7)
    handed = []
    real = store._queue_pairs

    def spy(conn, pairs, on_queued, existing):
        pairs = list(pairs)
        handed.extend(a["rel_path"] for a, _ in pairs)
        return real(conn, pairs, on_queued, existing)

    monkeypatch.setattr(store, "_queue_pairs", spy)
    assert store.queue_changed(conn, INDEX_SQL, []) == 1
    assert handed == ["new.jpg"]


# -- the second copy ----------------------------------------------------------

@pytest.fixture()
def second(scanned, tmp_path):
    cfg, conn, _ = scanned
    disk = tmp_path / "other-disk"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    now = [2_000_000_000.0]
    mirror = mirror_mod.Mirror(cfg, lambda: db.connect(cfg.db_path), clock=lambda: now[0])
    return SimpleNamespace(cfg=cfg, conn=conn, mirror=mirror, disk=disk, now=now,
                           library=Path(cfg.active_root))


def _go(mirror, work):
    assert work() == {"started": True}
    mirror.join(30)
    return mirror.status()


def test_an_identical_file_already_on_the_copy_is_not_written_again(second, monkeypatch):
    _go(second.mirror, second.mirror.start)
    rel = "misc/plain.png"
    source = second.library / rel
    os.utime(source, (source.stat().st_atime, source.stat().st_mtime + 3600))
    second.conn.execute("UPDATE assets SET mtime=? WHERE rel_path=?",
                        (source.stat().st_mtime, rel))
    second.conn.commit()
    copy = second.disk / second.library.name / rel
    inode = copy.stat().st_ino
    writes = []
    real = mirror_mod.create_new
    monkeypatch.setattr(mirror_mod, "create_new", lambda p: writes.append(p) or real(p))

    status = _go(second.mirror, second.mirror.start)
    assert status["copied_this_run"] == 1 and not status["error"]
    assert writes == [], "the same bytes were written out again"
    assert copy.stat().st_ino == inode
    assert abs(copy.stat().st_mtime - source.stat().st_mtime) < 1
    assert not list(copy.parent.glob("*(before*"))


def test_a_folder_on_the_copy_is_listed_once_a_run(second, monkeypatch):
    listed = Counter()
    real = os.listdir

    def listdir(path="."):
        listed[str(path)] += 1
        return real(path)

    monkeypatch.setattr(mirror_mod.os, "listdir", listdir)
    second.mirror._listings = {}
    folder = second.disk / "lib" / "2023"
    folder.mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (folder / name).write_bytes(b"x")
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        assert not second.mirror._taken_by_another(
            second.conn, second.disk, "R", f"2023/{name}", folder / name)
    assert listed and max(listed.values()) == 1, listed


def test_a_run_that_fails_is_tried_again_soon_not_tomorrow(second, monkeypatch):
    mirror = second.mirror

    def full(*_args, **_kwargs):
        raise OSError(28, "no room left on the second copy's disk")

    monkeypatch.setattr(mirror, "_copy_one", full)
    status = _go(mirror, mirror.start)
    assert status["error"] and status["last_run"] == 0
    assert status["last_attempt"] == second.now[0]
    assert not mirror.due(), "straight away again"
    second.now[0] += mirror_mod.RETRY_AFTER
    assert mirror.due(), "an hour on, not a day on"

    monkeypatch.undo()
    status = _go(mirror, mirror.start)
    assert not status["error"] and status["last_run"] == second.now[0]
    second.now[0] += mirror_mod.RETRY_AFTER
    assert not mirror.due(), "a run that finished waits its day"


def test_asking_whether_a_copy_is_due_reads_the_record_first(second, monkeypatch):
    mirror = second.mirror
    mirror._set_meta(mirror._db(), "last_run", second.now[0])
    monkeypatch.setattr(mirror, "problem", lambda: pytest.fail("looked at the disk"))
    assert not mirror.due()


def test_a_check_of_the_copy_carries_on_where_it_stopped(second, monkeypatch):
    mirror = second.mirror
    _go(mirror, mirror.start)
    rows = second.conn.execute("SELECT root, rel_path FROM mirror_copies "
                               "ORDER BY root, rel_path").fetchall()
    assert len(rows) > 5
    read = []
    real = mirror_mod.sha256_file
    monkeypatch.setattr(mirror_mod, "sha256_file", lambda p: read.append(p) or real(p))

    # The nightly copy comes due part-way: the check makes way, keeping its place.
    monkeypatch.setattr(mirror_mod, "VERIFY_SAVE_EVERY", 2)
    second.now[0] += mirror.every
    status = _go(mirror, mirror.verify)
    assert len(read) == 2 and "set aside" in status["message"]
    cursor = json.loads(mirror._meta(mirror._db(), "verify_cursor"))
    assert (cursor["root"], cursor["rel"]) == tuple(rows[1])
    assert mirror.due() and mirror.verify_due()

    _go(mirror, mirror.start)
    assert not mirror.due()
    read.clear()
    status = _go(mirror, mirror.verify)
    assert len(read) == len(rows) - 2, "started again from the top"
    assert "Every one read back intact" in status["message"]
    assert status["last_verified"] and not mirror._meta(mirror._db(), "verify_cursor")
    assert not mirror.verify_due()


# -- importing an export ------------------------------------------------------

def _jpeg(colour) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (60, 40), colour).save(out, "JPEG")
    return out.getvalue()


def test_a_tgz_export_is_read_by_entry_and_duplicates_are_never_synced(
        scanned, tmp_path, monkeypatch):
    from conftest import ADMIN
    from ninaivu.server import auth
    from ninaivu.storage import importer as importer_mod

    cfg, conn, _ = scanned
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    exports = tmp_path / "exports"
    exports.mkdir()
    library = Path(cfg.active_root)
    photo = _jpeg((200, 10, 10))
    with tarfile.open(exports / "takeout-1.tgz", "w:gz") as tar:
        for name, data in (("Photos from 2019/new.jpg", photo),
                           ("Album/new.jpg", photo),
                           ("Photos from 2023/shot1.jpg",
                            (library / "2023/06/11/shot1.jpg").read_bytes())):
            info = tarfile.TarInfo(f"Takeout/Google Photos/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    def by_name(self, name):
        raise AssertionError(f"looked {name} up by name")

    monkeypatch.setattr(tarfile.TarFile, "getmember", by_name)
    synced = []
    real_fsync = os.fsync

    def fsync(fd):
        try:
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            synced.append("?")
        return real_fsync(fd)

    monkeypatch.setattr(importer_mod.os, "fsync", fsync)
    importer = importer_mod.Importer(cfg, lambda: db.connect(cfg.db_path))
    assert importer.start(str(exports), cfg.active_root, admin.id) == {"started": True}
    importer.join(60)
    status = importer.status()
    assert status["copied"] == 1 and status["duplicates"] == 2 and status["failed"] == 0
    staged = [p for p in synced if p.endswith(".part")]
    if "?" not in synced:
        assert len(staged) == 1, synced


# -- the archive --------------------------------------------------------------

def test_the_archive_rate_counts_copying_time_only(tmp_path):
    from ninaivu import archive
    from ninaivu.archive import database as adb
    from ninaivu.archive.scanner import ArchiveJob

    archive.configure(tmp_path)
    adb.close_db()
    adb.init_db()
    try:
        job = ArchiveJob([str(tmp_path / "s")], str(tmp_path / "d"))
        assert job.work_seconds() is None, "no rate during the count before copying"
        job.started_at = time.time() - 600            # ten minutes of counting
        job.work_started_at = time.time() - 60
        assert 59 <= job.work_seconds() <= 61
        job.gate.pause()
        job.gate._paused_at -= 30                       # paused half a minute
        assert 29 <= job.work_seconds() <= 31
        job.gate.resume()
        assert 29 <= job.work_seconds() <= 31
    finally:
        adb.close_db()


def test_a_copy_is_not_charged_again_for_hashing_and_reading_back(tmp_path):
    from ninaivu.archive.pacing import ArchivePacer, SystemConditions
    from ninaivu.archive.scanner import Cancelled, Gate, Uncharged, hash_file

    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (4 << 20))
    sleeps = []
    gate = Gate()
    gate.pacer = ArchivePacer(lambda: SystemConditions(True, 70, 45), sleeps.append, 0)
    hash_file(path, Uncharged(gate))
    assert sleeps == []
    gate.pause()
    gate.cancel()
    with pytest.raises(Cancelled):
        hash_file(path, Uncharged(gate))            # a stop still stops it
    gate = Gate()
    gate.pacer = ArchivePacer(lambda: SystemConditions(True, 70, 45), sleeps.append, 0)
    hash_file(path, gate)
    assert sum(sleeps) > 0, "the audit, which only reads, is still paced"


def test_a_nearly_full_archive_disk_is_rechecked_for_space_only(tmp_path, monkeypatch):
    import shutil
    from ninaivu.archive import guardian as guardian_mod
    from test_archive_guardian import recorded_archive

    nearly_full = shutil.disk_usage("/")._replace(total=1000, used=950, free=50)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: nearly_full)
    recorded_archive(tmp_path)
    guardian = guardian_mod.ArchiveGuardian(lambda: False, sample_size=8)
    first = guardian.run_once()
    assert first["status"] == "warning" and first["checked"] == 1
    assert not guardian.sample_due(), "the five-minute retry reads nothing back"

    monkeypatch.setattr(guardian_mod, "hash_file",
                        lambda *_a: pytest.fail("read a file back on the space check"))
    again = guardian.run_once(sample=False)
    assert again["status"] == "warning" and again["matched"] == 1
    assert again["sampled_at"] == first["sampled_at"]
    guardian._state["sampled_at"] = time.time() - guardian.interval
    assert guardian.sample_due(), "the sample keeps to its daily round"
