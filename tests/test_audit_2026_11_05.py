"""The audit of 5 November 2026: gaps across the app, and the update copy on
every core.

The copy before an update spent most of its eight minutes in gzip level 9 on
one core. It is now deflated on several threads into one ordinary gzip member
(utils/pgzip.py), so the size and every reader stay as they were. Restores
and the daily bundle check read a bundle once instead of three times, and sum
its files on several threads. And a handful of background threads and
downloads that one odd answer from the network could end for good.
"""

from __future__ import annotations

import gc
import gzip
import http.client
import io
import os
import socket
import tarfile
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.storage import backup
from ninaivu.utils.pgzip import ParallelGzipWriter


def _sample(size: int) -> bytes:
    """Compressible like an index: repeated records with some noise."""
    rows = b"".join(b"row %08d caption family photo %s\n" % (i, os.urandom(4).hex().encode())
                    for i in range(size // 40 + 1))
    return rows[:size]


def _members(blob: bytes) -> int:
    """How many gzip members *blob* is."""
    count = 0
    while blob:
        unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
        unpacker.decompress(blob)
        assert unpacker.eof
        count += 1
        blob = unpacker.unused_data
    return count


# -- the parallel gzip writer ---------------------------------------------------

@pytest.mark.parametrize("size", [0, 1, 32 * 1024 - 1, 32 * 1024, 64 * 1024 + 7, 300_000])
def test_what_goes_in_comes_out_as_one_gzip_member(size):
    data = _sample(size)
    out = io.BytesIO()
    with ParallelGzipWriter(out, 9, workers=3, chunk=64 * 1024) as writer:
        for start in range(0, len(data), 10_000):
            writer.write(data[start:start + 10_000])
    assert gzip.decompress(out.getvalue()) == data
    assert _members(out.getvalue()) == 1


def test_it_is_as_small_as_gzip_on_one_core():
    data = _sample(4_000_000)
    out = io.BytesIO()
    with ParallelGzipWriter(out, 9, workers=4) as writer:
        writer.write(data)
    alone = len(gzip.compress(data, 9))
    assert len(out.getvalue()) <= alone * 1.01


def test_the_streaming_tar_reader_reads_the_whole_of_it(tmp_path):
    # tarfile's r|gz stops at the end of the first gzip member: joined gzip
    # files would have read as a bundle cut short, here and in every older
    # Ninaivu restoring one.
    folder = tmp_path / "state"
    folder.mkdir()
    for i in range(40):
        (folder / f"f{i}.bin").write_bytes(_sample(50_000))
    out = io.BytesIO()
    with ParallelGzipWriter(out, 9, workers=4, chunk=64 * 1024) as packer, \
            tarfile.open(fileobj=packer, mode="w|") as tar:
        tar.add(folder, arcname="state")
    with tarfile.open(fileobj=io.BytesIO(out.getvalue()), mode="r|gz") as tar:
        names = [member.name for member in tar]
    assert len(names) == 41


def test_it_deflates_on_threads_of_its_own():
    names = set()
    real = zlib.compressobj

    def watched(*args, **kwargs):
        names.add(threading.current_thread().name)
        return real(*args, **kwargs)

    out = io.BytesIO()
    zlib_compressobj, zlib.compressobj = zlib.compressobj, watched
    try:
        with ParallelGzipWriter(out, 6, workers=3, chunk=32 * 1024) as writer:
            writer.write(_sample(1_000_000))
    finally:
        zlib.compressobj = zlib_compressobj
    assert names and all(name.startswith("ninaivu-gzip") for name in names)


def test_leaving_on_an_error_writes_no_trailer():
    out = io.BytesIO()
    with pytest.raises(RuntimeError), ParallelGzipWriter(out, 9, workers=2) as writer:
        writer.write(_sample(100_000))
        raise RuntimeError("stopped")
    with pytest.raises((EOFError, OSError)):
        gzip.decompress(out.getvalue())
    with pytest.raises(ValueError):
        writer.write(b"more")


# -- bundles: written on every core, read once ----------------------------------

def _state(tmp_path: Path) -> Path:
    import sqlite3
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "index.db") as conn:
        conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, caption TEXT)")
        conn.executemany("INSERT INTO assets (caption) VALUES (?)",
                         [(f"family photo {i}",) for i in range(2000)])
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    (state / "config.json").write_text("{}", encoding="utf-8")
    return state


def test_a_backup_is_one_gzip_member_that_restores(tmp_path):
    bundle = backup.snapshot(_state(tmp_path), tmp_path / "out")
    assert bundle is not None
    assert _members(bundle.read_bytes()) == 1
    restored = backup.extract_verified(bundle, tmp_path / "restore")
    assert (restored / "index.db").is_file()


def test_the_daily_check_reads_a_bundle_once(tmp_path, monkeypatch):
    bundle = backup.snapshot(_state(tmp_path), tmp_path / "out")

    def three_passes(*_args, **_kwargs):
        raise AssertionError("extract() reads the bundle twice, verify_state once more")

    monkeypatch.setattr(backup, "extract", three_passes)
    result = backup.verify_bundle(bundle)
    assert result["ok"] is True and result["assets"] == 2000


def test_a_damaged_bundle_still_fails_the_daily_check(tmp_path):
    bundle = backup.snapshot(_state(tmp_path), tmp_path / "out")
    blob = bytearray(bundle.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    bundle.write_bytes(bytes(blob))
    result = backup.verify_bundle(bundle)
    assert result["ok"] is False and result["error"]


def test_verify_state_finds_a_changed_file_and_reports_in_order(tmp_path):
    state = _state(tmp_path)
    for i in range(30):
        (state / "avatars").mkdir(exist_ok=True)
        (state / "avatars" / f"{i}.png").write_bytes(os.urandom(100))
    staged = tmp_path / "unpacked"
    restored = backup.extract_verified(backup.snapshot(state, tmp_path / "out"), staged)
    told = []
    backup.verify_state(restored, checked=told.append)
    manifest = __import__("json").loads((restored / "backup_manifest.json").read_text())
    assert told == [meta["size"] for meta in manifest["files"].values()]
    victim = restored / "avatars" / "7.png"
    victim.write_bytes(bytes(100))
    with pytest.raises(ValueError, match="mismatch"):
        backup.verify_state(restored)


# -- background threads one odd answer ended -----------------------------------

def test_a_garbled_answer_from_the_mini_does_not_end_the_probing(tmp_path):
    from ninaivu.replica.front import Front

    def garbled(*_args, **_kwargs):
        raise http.client.BadStatusLine("SSH-2.0-OpenSSH")

    cfg = SimpleNamespace(replica_primary_url="http://mini.local")
    front = Front(cfg, opener=garbled, sender=lambda packet: None)
    assert front.look() is False


def test_the_probing_loop_outlives_anything_a_probe_raises(monkeypatch):
    from ninaivu.replica import front as module

    cfg = SimpleNamespace(replica_primary_url="http://mini.local")
    door = module.Front(cfg, sender=lambda packet: None)
    calls = []

    def look():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("something nobody thought of")
        door._stop.set()
        return True

    monkeypatch.setattr(door, "look", look)
    monkeypatch.setattr(module, "PROBE_EVERY", 0.01)
    door._loop()
    assert len(calls) == 2


def test_the_archive_guardian_outlives_an_unexpected_error(tmp_path, monkeypatch):
    from ninaivu import archive
    from ninaivu.archive import database
    from ninaivu.archive.guardian import ArchiveGuardian

    archive.configure(tmp_path / "state")
    database.close_db()
    database.init_db()
    asked = []

    def is_running():
        asked.append(1)
        if len(asked) == 1:
            raise KeyError("a setting nobody expected")
        return True

    guardian = ArchiveGuardian(is_running)
    waits = []

    class QuickStop(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) >= 3:
                self.set()
            return self.is_set()

    guardian._stop = QuickStop()
    guardian._state = {"checked_at": 0}
    guardian._loop()
    assert len(asked) == 2                 # asked again after the error
    assert guardian._state.get("retry_soon") is True


class _CutShort:
    """An HTTP answer whose body stops early."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *_args):
        raise http.client.IncompleteRead(b"partial", 1000)

    readinto = None


def test_a_model_download_cut_short_leaves_no_part_file(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    from ninaivu.media import orientnet

    monkeypatch.setattr(orientnet.urllib.request, "urlopen", lambda *a, **k: _CutShort())
    result = orientnet.fetch_model(tmp_path, force=True)
    assert result["ready"] is False and result["error"]
    assert not list(tmp_path.rglob("*.part"))


def test_a_garbled_webhook_answer_still_lets_the_email_go():
    from ninaivu.utils.notify import Notifier

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def answer():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b"this is not http\r\n\r\n")
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    port = listener.getsockname()[1]
    try:
        result = Notifier(webhook_url=f"http://127.0.0.1:{port}/hook")._post("t", "d", "test")
    finally:
        listener.close()
    assert result == "Could not reach the webhook."


# -- originals sent for from the Secondary -------------------------------------

def test_the_lock_for_an_original_goes_once_nobody_holds_it():
    from ninaivu.mugil import fetch

    with fetch._lock_for("ab" * 32):
        assert "ab" * 32 in fetch._locks
    gc.collect()
    assert "ab" * 32 not in fetch._locks


def test_trim_leaves_a_download_still_going_on(tmp_path):
    from ninaivu.mugil import fetch

    base = fetch.folder(tmp_path)
    old = base / "aa" / ("aa" * 32)
    going = base / "bb" / ("bb" * 32)
    for home in (old, going):
        home.mkdir(parents=True)
        (home / "photo.jpg").write_bytes(b"x" * 1000)
        os.utime(home, (1, 1))
    (going / f".video.mp4{fetch.PARTIAL}1234").write_bytes(b"y" * 1000)
    os.utime(going, (0, 0))                     # the older of the two
    freed = fetch.trim(tmp_path, most=0)
    assert going.is_dir() and not old.exists()
    assert freed == 1000


def test_a_partial_left_by_a_stopped_server_does_not_keep_its_folder(tmp_path):
    from ninaivu.mugil import fetch

    home = fetch.folder(tmp_path) / "cc" / ("cc" * 32)
    home.mkdir(parents=True)
    stale = home / f".x.jpg{fetch.PARTIAL}beef"
    stale.write_bytes(b"z" * 10)
    hour_ago = time.time() - 3600
    os.utime(stale, (hour_ago, hour_ago))
    fetch.trim(tmp_path, most=0)
    assert not home.exists()


# -- restoring the index from Drive --------------------------------------------

def test_a_drive_copy_that_fails_its_check_leaves_nothing_unpacked(tmp_path, monkeypatch):
    from ninaivu.cloud import index_copy, restore

    # A real bundle, damaged late on: the first files unpack before the
    # check fails, which is what used to be left behind.
    state = _state(tmp_path)
    (state / "avatars").mkdir()
    for i in range(20):
        (state / "avatars" / f"{i}.bin").write_bytes(os.urandom(20_000))
    blob = bytearray(backup.snapshot(state, tmp_path / "out").read_bytes())
    blob[int(len(blob) * 0.8)] ^= 0xFF
    blob = bytes(blob)
    monkeypatch.setattr(restore, "fetch_range",
                        lambda _c, _id, start, end: blob[start:end + 1])
    work = tmp_path / "work"
    entry = {"id": "1", "name": "copy.tar.gz", "size": len(blob)}
    with pytest.raises((ValueError, tarfile.TarError, EOFError, OSError, zlib.error)):
        index_copy.fetch(SimpleNamespace(), entry, None, work)
    assert not (work / "unpacked").exists()


def test_sums_keep_a_few_per_thread_in_hand_and_stop_at_once(tmp_path, monkeypatch):
    """A million files are not queued at once, and a mismatch stops the rest."""
    import threading
    from contextlib import closing
    from ninaivu.storage import backup
    asked = []
    lock = threading.Lock()

    def sha(path):
        with lock:
            asked.append(path)
        return f"sum-{path.name}"
    monkeypatch.setattr(backup, "sha256_file", sha)
    paths = [tmp_path / f"f{i}" for i in range(10_000)]
    with closing(backup._sums(paths)) as sums:
        first = [next(sums) for _ in range(3)]
    assert first == ["sum-f0", "sum-f1", "sum-f2"]
    assert len(asked) <= 3 + backup.HASH_WORKERS * 4 + 1      # not ten thousand
    with closing(backup._sums(paths[:500])) as sums:
        assert list(sums) == [f"sum-f{i}" for i in range(500)]       # in order, all of them
