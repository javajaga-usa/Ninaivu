"""The archive counts and starts quickly, without deciding anything differently.

Measured on USB hard disks, a cold estimate spent nearly all its time waiting
for the disk, and Start then walked every source again before its first copy.
Four things change, and each must leave every decision exactly as it was:

* the walk lists folders and reads file headers ahead of itself on worker
  threads, while its own order and decisions stay on one thread;
* a file too small to keep is not opened to find out what it is;
* Start uses a recent estimate of the same job instead of counting again;
* the archive is only swept for leftover temporaries when a run could have
  left one.
"""

import os
import pathlib
import sys
import threading

import pytest
from PIL import Image

from ninaivu import archive
from ninaivu.archive import database as db
from ninaivu.archive import readahead, scanner
from ninaivu.archive.scanner import ArchiveJob


@pytest.fixture()
def work(tmp_path):
    archive.configure(tmp_path / "state")
    db.close_db()
    db.init_db()
    scanner.forget_estimate()
    try:
        yield tmp_path
    finally:
        scanner.forget_estimate()
        db.close_db()


def _photo(path, shade=0, size=(64, 48)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (shade % 256, 90, 200)).save(path, "JPEG")
    return path


def _tree(root):
    """Folders a few levels deep, media beside things that are not."""
    n = 0
    for a in range(4):
        for b in range(5):
            folder = root / f"drive{a}" / f"album{b}"
            for _c in range(3):
                n += 1
                _photo(folder / f"IMG_{n:04}.jpg", shade=n)
            (folder / "notes.txt").write_text("not media")
            (folder / f"CLIP{b}").write_bytes(b"\x00\x00\x00\x18ftypisom" + bytes(700 + b))
            (folder / "tiny.bin").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(20))
        (root / f"drive{a}" / "$RECYCLE.BIN").mkdir(exist_ok=True)
    return root


def _walk_record(job):
    walked = [(os.path.relpath(root, job.sources[0]), name, job.last_size)
              for root, name in job._walk()]
    tallies = (job.non_media, job.too_small, job.filtered_out, dict(job.included_by_kind),
               job.skipped_system_dirs, job.pruned_destination)
    return walked, tallies


# ---------------------------------------------------------------------------
# read-ahead changes the time taken, never the answer
# ---------------------------------------------------------------------------

def test_reading_ahead_walks_the_same_files_in_the_same_order(work):
    source = _tree(work / "source")
    alone = ArchiveJob([str(source)], str(work / "dest"))
    alone.read_ahead_threads = 0
    together = ArchiveJob([str(source)], str(work / "dest"))
    together.read_ahead_threads = 8

    assert _walk_record(together) == _walk_record(alone)
    assert _walk_record(together)[0], "the fixture walked nothing"


def test_a_folder_that_fails_to_list_ahead_is_still_reported(work, monkeypatch):
    source = _tree(work / "source")
    real_scandir = os.scandir

    def flaky(path="."):
        if os.path.basename(os.fspath(path)) == "album3":
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky)
    job = ArchiveJob([str(source)], str(work / "dest"))
    job.read_ahead_threads = 8
    walked = list(job._walk())
    assert walked
    assert len(job.unreadable_dirs) == 4, job.unreadable_dirs
    assert all("album3" in path for path, _ in job.unreadable_dirs)


def test_read_ahead_reads_on_the_walks_thread_when_nothing_is_ready(tmp_path):
    folder = tmp_path / "f"
    _photo(folder / "a.jpg")
    with readahead.ReadAhead(0) as ahead:
        listing = ahead.listing(str(folder))
    assert listing.files == ["a.jpg"] and listing.error is None


def test_read_ahead_uses_worker_results_and_stops_cleanly(tmp_path):
    for i in range(30):
        _photo(tmp_path / f"d{i}" / "x.jpg", shade=i)
    reads = []

    def reader(path):
        reads.append(threading.current_thread().name)
        return os.path.basename(path)

    ahead = readahead.ReadAhead(
        4, header_jobs=lambda listing: [("probe", e.path) for e in listing.entries.values()],
        readers={"probe": reader})
    try:
        top = ahead.listing(str(tmp_path))
        results = []
        for name in top.dirs:
            child = ahead.listing(os.path.join(str(tmp_path), name))
            results += [ahead.read("probe", child.entries[f].path) for f in child.files]
    finally:
        ahead.close()
    assert results == ["x.jpg"] * 30
    assert any(name == "archive-readahead" for name in reads), "nothing was read ahead"


def _settled(ahead, timeout=10):
    """Wait for the workers to finish what is in flight."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with ahead._lock:
            busy = any(state == readahead._RUNNING for state in ahead._state.values())
        if not busy:
            return
        time.sleep(0.02)
    raise AssertionError("read-ahead workers never settled")


def test_a_subtree_the_walk_skips_gives_its_read_ahead_back(tmp_path):
    """Folders listed ahead inside a subtree the walk never enters must not
    fill the budget, or read-ahead stops for the rest of the drive."""
    for course in range(6):
        for lesson in range(8):
            (tmp_path / f"course{course}" / f"lesson{lesson}").mkdir(parents=True)
    ahead = readahead.ReadAhead(4, budget=20)
    try:
        top = ahead.listing(str(tmp_path))
        for name in top.dirs:
            child = os.path.join(str(tmp_path), name)
            ahead.listing(child)
            _settled(ahead)
            ahead.discard(child)                  # held back whole, like a course
        _settled(ahead)
        with ahead._lock:
            assert ahead._outstanding == 0, ahead._outstanding
            assert not ahead._state, ahead._state
    finally:
        ahead.close()


def test_a_failing_header_reader_never_hangs_the_walk(tmp_path):
    for i in range(20):
        _photo(tmp_path / f"d{i}" / "x.jpg", shade=i)

    def broken_jobs(listing):
        raise RuntimeError("bug in deciding what to read ahead")

    def broken_reader(path):
        raise RuntimeError("bug in reading ahead")

    result = {}

    def walk():
        with readahead.ReadAhead(4, header_jobs=broken_jobs, readers={"x": broken_reader}) as ahead:
            top = ahead.listing(str(tmp_path))
            result["files"] = sum(len(ahead.listing(os.path.join(str(tmp_path), d)).files)
                                  for d in top.dirs)
            with pytest.raises(RuntimeError):
                ahead.read("x", str(tmp_path / "d0" / "x.jpg"))

    thread = threading.Thread(target=walk, daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive(), "the walk hung on a read-ahead failure"
    assert result["files"] == 20


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_a_junction_loop_is_walked_once(work):
    import _winapi

    source = work / "source"
    inner = source / "inner"
    _photo(inner / "ok.jpg")
    try:
        _winapi.CreateJunction(str(source), str(inner / "loop"))
    except OSError:
        pytest.skip("cannot create a junction here")
    job = ArchiveJob([str(source)], str(work / "dest"))
    names = [name for _root, name in job._walk()]
    assert names == ["ok.jpg"], names


def test_the_archive_inside_a_source_is_still_stepped_around(work):
    root = work / "Drive"
    _photo(root / "Pics" / "keep.jpg")
    _photo(root / "Master" / "2001" / "01" / "01" / "already.jpg", shade=7)
    job = ArchiveJob([str(root)], str(root / "master"))     # a different case, same folder
    found = {name for _root, name in job._walk()}
    if os.path.normcase("A") == "a":
        assert found == {"keep.jpg"}, found
        assert job.pruned_destination == 1
    else:
        assert "keep.jpg" in found


# ---------------------------------------------------------------------------
# a file too small to keep is never opened
# ---------------------------------------------------------------------------

def test_an_unrecognised_file_under_the_floor_is_not_opened(work, monkeypatch):
    source = work / "source"
    source.mkdir()
    (source / "Thing.class").write_bytes(b"\xca\xfe\xba\xbe" + bytes(500))
    (source / "renamed.txt").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(5000))
    (source / "photo_no_ext").write_bytes(b"\xff\xd8\xff\xe0" + bytes(5000))
    opened = []
    real_sniff = scanner.sniff_media

    def counting(path):
        opened.append(os.path.basename(path))
        return real_sniff(path)

    monkeypatch.setattr(scanner, "sniff_media", counting)
    monkeypatch.setattr(scanner, "MIN_MEDIA_BYTES", 1024)
    for threads in (0, 8):
        opened.clear()
        job = ArchiveJob([str(source)], str(work / "dest"))
        job.read_ahead_threads = threads
        taken = sorted(name for _root, name in job._walk())
        assert taken == ["photo_no_ext", "renamed.txt"], taken
        assert "Thing.class" not in opened, f"a file under the floor was opened ({threads} threads)"
        assert job.too_small == 1


def test_a_small_ts_file_is_not_opened_to_rule_out_typescript(work, monkeypatch):
    source = work / "source"
    source.mkdir()
    (source / "index.d.ts").write_text("export declare const x: number;\n")
    packets = (b"\x47" + bytes(187)) * 20                     # a real transport stream
    (source / "broadcast.ts").write_bytes(packets * 10)
    opened = []
    real_sniff = scanner.sniff_media
    monkeypatch.setattr(scanner, "sniff_media",
                        lambda path: opened.append(os.path.basename(path)) or real_sniff(path))
    monkeypatch.setattr(scanner, "MIN_MEDIA_BYTES", 1024)
    for threads in (0, 8):
        opened.clear()
        job = ArchiveJob([str(source)], str(work / "dest"))
        job.read_ahead_threads = threads
        assert [name for _root, name in job._walk()] == ["broadcast.ts"]
        assert "index.d.ts" not in opened, f"a tiny TypeScript file was opened ({threads} threads)"


# ---------------------------------------------------------------------------
# Start uses a recent estimate of the same job
# ---------------------------------------------------------------------------

def _estimate(job):
    files = total = 0
    for _root, _name in job._walk():
        files += 1
        total += job.last_size
    scanner.remember_estimate(job, files, total)
    return files, total


def test_start_skips_counting_when_the_estimate_matches(work, monkeypatch):
    source = _tree(work / "source")
    dest = work / "dest"
    files, total = _estimate(ArchiveJob([str(source)], str(dest)))

    def no_second_count(self):
        raise AssertionError("Start walked the sources again just to count them")

    monkeypatch.setattr(ArchiveJob, "preflight", no_second_count)
    job = ArchiveJob([str(source)], str(dest))
    job.run()
    assert (job.total_files, job.total_bytes) == (files, total)
    assert job.processed == files
    assert db.latest_job()["state"] == "completed", db.latest_job()


@pytest.mark.parametrize("change", ["kinds", "deep scan", "decision", "expired"])
def test_a_different_or_stale_estimate_is_not_used(work, monkeypatch, change):
    source = _tree(work / "source")
    dest = work / "dest"
    _estimate(ArchiveJob([str(source)], str(dest)))

    kwargs = {}
    if change == "kinds":
        kwargs["media_types"] = {"image"}
    elif change == "deep scan":
        kwargs["deep_scan"] = False
    elif change == "decision":
        db.set_folder_decision("some-folder", str(source), "exclude")
    elif change == "expired":
        monkeypatch.setattr(scanner, "ESTIMATE_REUSE_SECONDS", -1)

    counted = []
    real_preflight = ArchiveJob.preflight
    monkeypatch.setattr(ArchiveJob, "preflight",
                        lambda self: counted.append(1) or real_preflight(self))
    ArchiveJob([str(source)], str(dest), **kwargs).run()
    assert counted, f"an estimate was reused after a change of {change}"


def test_progress_never_reads_past_everything(work):
    source = _tree(work / "source")
    dest = work / "dest"
    files, total = _estimate(ArchiveJob([str(source)], str(dest)))
    _photo(source / "added-after-the-estimate.jpg", shade=99)

    job = ArchiveJob([str(source)], str(dest))
    job.run()
    assert job.processed == files + 1
    assert job.total_files >= job.processed


# ---------------------------------------------------------------------------
# the archive is swept only when a run could have left something
# ---------------------------------------------------------------------------

def _plant_partial(dest, folder=None):
    base = pathlib.Path(folder) if folder else dest / "2020" / "01" / "01"
    leftover = base / f"{scanner.PARTIAL_PREFIX}old.tmp"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_bytes(b"half a photo")
    return leftover


def _watch_walks(monkeypatch):
    listed = []
    real_walk = os.walk
    monkeypatch.setattr(scanner.os, "walk",
                        lambda top, *a, **k: listed.append(top) or real_walk(top, *a, **k))
    return listed


def test_a_destination_with_no_history_here_is_swept(work):
    source = _tree(work / "source")
    dest = work / "dest"
    leftover = _plant_partial(dest)
    ArchiveJob([str(source)], str(dest)).run()
    assert not leftover.exists()


def test_after_clean_runs_the_archive_is_not_listed_for_leftovers(work, monkeypatch):
    source = _tree(work / "source")
    dest = work / "dest"
    ArchiveJob([str(source)], str(dest)).run()
    assert db.latest_job()["state"] == "completed"

    listed = []
    real_walk = os.walk
    monkeypatch.setattr(scanner.os, "walk",
                        lambda top, *a, **k: listed.append(top) or real_walk(top, *a, **k))
    ArchiveJob([str(source)], str(dest)).run()
    assert not listed, "the whole archive was listed although every run ended cleanly"


def test_a_run_that_never_finished_gets_its_leftovers_swept_once(work, monkeypatch):
    source = _tree(work / "source")
    dest = work / "dest"
    ArchiveJob([str(source)], str(dest)).run()
    crashed = db.latest_job()["id"]
    db.update_job(crashed, state="running")        # the app was closed mid-copy
    # A temporary is only ever made in a folder the job was writing into.
    leftover = _plant_partial(dest, db.job_folders([crashed])[0])

    ArchiveJob([str(source)], str(dest)).run()
    assert not leftover.exists(), "the leftover of an unfinished run was not swept"

    listed = []
    real_walk = os.walk
    monkeypatch.setattr(scanner.os, "walk",
                        lambda top, *a, **k: listed.append(top) or real_walk(top, *a, **k))
    ArchiveJob([str(source)], str(dest)).run()
    assert not listed, "one unfinished run made every later Start sweep again"


def test_an_unfinished_run_is_swept_without_listing_the_archive(work, monkeypatch):
    """Only the folders it was writing into are looked in.

    Listing the archive for leftovers was 2 minutes 39 seconds before the
    first copy on a 319,000-file archive on a USB disk, every time Start came
    after a restart.
    """
    source = _tree(work / "source")
    dest = work / "dest"
    ArchiveJob([str(source)], str(dest)).run()
    crashed = db.latest_job()["id"]
    folders = db.job_folders([crashed])
    assert folders, "the job recorded no folders it wrote into"
    db.update_job(crashed, state="running")
    leftover = _plant_partial(dest, folders[-1])

    listed = _watch_walks(monkeypatch)
    ArchiveJob([str(source)], str(dest)).run()
    assert not leftover.exists(), "the leftover of an unfinished run was not swept"
    assert str(dest) not in " ".join(map(str, listed)),         "the whole archive was listed to find one run's leftovers"


def test_a_run_from_before_folders_were_recorded_is_swept_whole(work, monkeypatch):
    """No record of where it wrote means it could have written anywhere."""
    source = _tree(work / "source")
    dest = work / "dest"
    ArchiveJob([str(source)], str(dest)).run()
    crashed = db.latest_job()["id"]
    db.update_job(crashed, state="running")
    # As an archive whose unfinished run predates the record looks.
    db.set_config(f"temp-folders-tracked:{scanner.normalise(str(dest))}", str(crashed + 1))
    leftover = _plant_partial(dest)                 # somewhere it never wrote

    ArchiveJob([str(source)], str(dest)).run()
    assert not leftover.exists(), "a run with no record was not swept whole"


# ---------------------------------------------------------------------------
# The database answers its per-file questions from indexes
# ---------------------------------------------------------------------------

def _plan(sql, *params):
    return " ".join(row[3] for row in db.get_db().execute(
        "EXPLAIN QUERY PLAN " + sql, params))


def test_questions_about_an_archive_path_are_answered_from_an_index(work):
    """Every file a dry run plans asks whether its archive path is taken, and
    without an index each answer read every row planned so far."""
    names = {r[1] for r in db.get_db().execute("PRAGMA index_list(files)")}
    assert "idx_dest_status" in names
    assert "idx_file_hash" not in names and "idx_size" not in names, (
        "prefixes of the composite indexes: SQLite never used them")
    planned = _plan("SELECT 1 FROM files WHERE destination_path=? AND status='planned' "
                    "LIMIT 1", "/x")
    assert "idx_dest_status" in planned, planned
    recorded = _plan("SELECT size, dest_hash, file_hash FROM files "
                     "WHERE destination_path=? AND status='verified' LIMIT 1", "/x")
    assert "idx_dest_status" in recorded, recorded


def test_an_older_database_loses_the_indexes_it_no_longer_needs(work):
    conn = db.get_db()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_hash ON files(file_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_size ON files(size)")
    conn.commit()
    db.init_db()
    names = {r[1] for r in conn.execute("PRAGMA index_list(files)")}
    assert not {"idx_file_hash", "idx_size"} & names


def _verified(source, dest, size, digest="a" * 64):
    db.claim_file(source, os.path.basename(source), size, 1.0, 1)
    db.set_status(source, "verified", file_hash=digest, dest_hash=digest,
                  destination_path=dest)


def test_a_size_known_in_another_archive_costs_no_extra_read(work):
    """Moving archive A into a new archive B: every file's size is "known",
    from the run that built A, so every file was hashed before being copied
    and then hashed again during the copy. The duplicate check this stands in
    for is scoped to the archive being written, so the size check is too."""
    a, b = work / "A", work / "B"
    _verified(str(work / "src" / "one.jpg"), str(a / "2019" / "one.jpg"), 1234)
    assert db.size_is_known(1234, destination_root=str(a))
    assert not db.size_is_known(1234, destination_root=str(b))
    assert not db.size_is_known(4321, destination_root=str(a))
    assert db.size_is_known(1234), "unscoped, the answer is what it always was"


def test_the_audit_asks_cheaply_whether_there_is_anything_to_audit(work):
    assert db.has_verified() is False
    _verified(str(work / "src" / "one.jpg"), str(work / "dest" / "one.jpg"), 10)
    assert db.has_verified() is True
    assert bool(db.verified_rows()) is db.has_verified()


def test_the_audit_writes_only_what_changed(work, monkeypatch):
    """Most rows an audit reads are already verified with the hash it finds;
    writing that back forced a commit per file for nothing."""
    import hashlib
    source = _tree(work / "source")
    dest = work / "dest"
    ArchiveJob([str(source)], str(dest)).run()
    rows = db.verified_rows()
    assert rows
    # One row recorded no hash of the written file, as a copy that was
    # interrupted after the write might have.
    stale = rows[0]
    db.get_db().execute("UPDATE files SET dest_hash=NULL WHERE source_path=?",
                        (stale["source_path"],))
    db.get_db().commit()

    writes = []
    real = db.set_status

    def counting(source_path, status, **fields):
        writes.append((source_path, status))
        return real(source_path, status, **fields)

    monkeypatch.setattr(scanner.db, "set_status", counting)
    ArchiveJob([str(source)], str(dest), mode=scanner.MODE_VERIFY).run()
    assert db.latest_job()["state"] == "completed"
    assert writes == [(stale["source_path"], "verified")]
    repaired = db.get_db().execute("SELECT dest_hash FROM files WHERE source_path=?",
                                   (stale["source_path"],)).fetchone()[0]
    assert repaired == hashlib.sha256(
        pathlib.Path(stale["destination_path"]).read_bytes()).hexdigest()


def test_the_destination_is_normalised_once_not_per_file(monkeypatch):
    from ninaivu.archive import safety

    calls = []
    real = safety.normalise

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(safety, "normalise", counting)
    safety._normalised_parent.cache_clear()
    parent = os.path.join(os.sep, "archive-root-for-this-test")
    for i in range(5):
        assert safety.is_within(os.path.join(parent, "2019", f"{i}.jpg"), parent)
    assert not safety.is_within(os.path.join(os.sep, "elsewhere", "x.jpg"), parent)
    assert calls.count(parent) == 1, "the parent was normalised again per file"
    assert len(calls) == 7
    safety._normalised_parent.cache_clear()

