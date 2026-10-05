"""The consolidation engine's own guarantees, verified inside Ninaivu.

Carried over whole from the standalone tool, because the promises they check
are exactly the ones a family is trusting with the only copy of their photos:
nothing lost, nothing corrupted, nothing half-written, nothing deleted. Each
test maps to a defect that was once observed in the wild or to one of the
engine's safety invariants — so if folding the tool into Ninaivu broke any of
them, these say so.

They are behavioural rather than unit tests: they run real jobs over real
files in a temporary tree and then look at what is on disk.

The only change from the original is the harness. It ran itself with a
hand-rolled ``@test`` decorator; here that decorator is a no-op and the
per-test isolation it provided — a fresh database and a fresh tree — comes
from the ``work`` fixture instead, so pytest reports all 99 individually.
"""

import hashlib
import json
import io
import importlib.util
import os
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ninaivu import archive                                     # noqa: E402
from ninaivu.archive import database as db                      # noqa: E402
from ninaivu.archive import safety, scanner, status_kit         # noqa: E402
from ninaivu.archive.safety import validate_job                 # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                  # noqa: E402


@pytest.fixture()
def work(tmp_path):
    """A fresh archive database and a fresh tree, per test.

    The engine keeps its state in a module-level path, which is what lets it
    resume a run across restarts. Point it at this test's own directory so
    tests cannot see each other's work.
    """
    archive.configure(tmp_path)
    db.close_db()
    db.init_db()
    tree = tmp_path / "tree"
    tree.mkdir()
    try:
        yield str(tree)
    finally:
        db.close_db()


def test(fn):
    """Kept so the 99 bodies below need no edits; pytest does the running."""
    return fn


test.__test__ = False        # it is a decorator, not a test case

#: Some of these turn on a capture date, and the fixtures below are minimal
#: JPEGs that carry real EXIF but are not decodable images — deliberately, so
#: the date path is exercised against a real parser rather than a stub. Pillow
#: will not open them, so ExifRead is the only reader that can, and
#: requirements.txt lists ExifRead as optional.
#:
#: Skipped rather than failed, so `pip install -r requirements.txt` minus the
#: optional extras still gives a green suite and a real regression stays
#: visible instead of hiding among eight known reds.
needs_exifread = pytest.mark.skipif(
    importlib.util.find_spec("exifread") is None,
    reason="exifread is an optional extra; these tests read a capture date "
           "out of a fixture only ExifRead can parse")



# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def jpeg_with_date(date_str, payload=b'', size=None):
    """
    A minimal but genuinely parseable JPEG carrying an EXIF DateTimeOriginal,
    so the date path is exercised against a real parser rather than a stub.
    `date_str` uses EXIF form 'YYYY:MM:DD HH:MM:SS'.
    """
    date_b = date_str.encode('ascii')
    assert len(date_b) == 19

    # --- TIFF header + IFD0 with one ExifIFDPointer, then the Exif SubIFD ---
    def rational(num, den):
        return struct.pack('>II', num, den)

    tiff = bytearray()
    tiff += b'MM\x00\x2a'                      # big-endian, magic 42
    tiff += struct.pack('>I', 8)               # IFD0 at offset 8

    # IFD0: 1 entry -> ExifIFDPointer (0x8769)
    ifd0_offset = 8
    ifd0_size = 2 + 12 + 4
    exif_ifd_offset = ifd0_offset + ifd0_size
    tiff += struct.pack('>H', 1)
    tiff += struct.pack('>HHII', 0x8769, 4, 1, exif_ifd_offset)
    tiff += struct.pack('>I', 0)               # no IFD1

    # Exif SubIFD: 1 entry -> DateTimeOriginal (0x9003), ASCII, 20 bytes
    subifd_size = 2 + 12 + 4
    data_offset = exif_ifd_offset + subifd_size
    tiff += struct.pack('>H', 1)
    tiff += struct.pack('>HHII', 0x9003, 2, 20, data_offset)
    tiff += struct.pack('>I', 0)
    tiff += date_b + b'\x00'

    exif_payload = b'Exif\x00\x00' + bytes(tiff)
    app1 = b'\xff\xe1' + struct.pack('>H', len(exif_payload) + 2) + exif_payload

    body = payload or b'\x00' * 64
    jpg = b'\xff\xd8' + app1 + b'\xff\xdb\x00\x43\x00' + body + b'\xff\xd9'
    if size and len(jpg) < size:
        # Pad before EOI so the file stays a valid-ish JPEG of the target size.
        jpg = jpg[:-2] + b'\x00' * (size - len(jpg)) + b'\xff\xd9'
    return jpg


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _is_archive_metadata(name):
    """The root marker and the status kit describe the archive; they are not
    archived photos."""
    return name == safety.ARCHIVE_MARKER or name in status_kit.KIT_FILES


def tree_snapshot(root):
    """path -> (size, mtime_int, sha256) for every file under root."""
    out = {}
    for base, _, files in os.walk(root):
        for name in files:
            if _is_archive_metadata(name):
                continue
            p = os.path.join(base, name)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, int(st.st_mtime), sha(p))
    return out


def archived_files(dest):
    out = []
    for base, _, files in os.walk(dest):
        out += [os.path.join(base, f) for f in files if not _is_archive_metadata(f)]
    return out


def run_job(sources, dest):
    job = ArchiveJob(sources, dest)
    job.run()
    return job


def statuses():
    return {os.path.basename(r['source_path']): r['status']
            for r in db.get_recent_files(1000)}


# ==========================================================================
# The regressions
# ==========================================================================

@test
def test_errored_file_does_not_erase_its_healthy_twin(work):
    """
    THE DATA-LOSS BUG. Two byte-identical copies on two drives. The first is
    unreadable. The old code marked the second a 'duplicate' of the row that
    never landed, so the archive ended up with neither.
    """
    src1 = os.path.join(work, 'drive1')
    src2 = os.path.join(work, 'drive2')
    dest = os.path.join(work, 'archive')
    payload = jpeg_with_date('2011:06:15 10:00:00', b'AUNT MARGARET WEDDING' * 40)
    write(os.path.join(src1, 'wedding.jpg'), payload)
    write(os.path.join(src2, 'wedding_backup.jpg'), payload)

    real_open = io.open
    def flaky_open(path, mode='r', *a, **k):
        if 'drive1' in str(path) and 'b' in mode and 'r' in mode:
            raise OSError(5, 'Input/output error (simulated bad sector)')
        return real_open(path, mode, *a, **k)

    import builtins
    builtins.open = flaky_open
    try:
        run_job([src1, src2], dest)
    finally:
        builtins.open = real_open

    st = statuses()
    assert st.get('wedding.jpg') == 'error', f'expected drive1 to error, got {st}'
    assert st.get('wedding_backup.jpg') == 'verified', \
        f'the healthy twin must still be archived, got {st}'

    landed = archived_files(dest)
    assert len(landed) == 1, f'expected exactly one archived copy, got {landed}'
    assert sha(landed[0]) == hashlib.sha256(payload).hexdigest(), \
        'archived copy does not match the original bytes'


@test
def test_zero_byte_files_are_recorded_not_swallowed(work):
    """The old code UPDATEd a row it never INSERTed, so empty files vanished."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'empty.jpg'), b'')
    write(os.path.join(src, 'real.jpg'), jpeg_with_date('2015:01:02 03:04:05'))
    run_job([src], dest)

    st = statuses()
    assert 'empty.jpg' in st, 'the zero-byte file left no record at all'
    assert st['empty.jpg'] == 'skipped', f"expected 'skipped', got {st['empty.jpg']}"
    assert st['real.jpg'] == 'verified'


@test
def test_interrupted_file_is_retried_on_the_next_run(work):
    """
    RESUME. The old code inserted the row, then failed the copy, then skipped
    the file forever afterwards because the INSERT collided on re-run.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'a.jpg'), jpeg_with_date('2012:03:04 05:06:07', b'A' * 200))
    write(os.path.join(src, 'b.jpg'), jpeg_with_date('2012:03:04 05:06:07', b'B' * 200))

    # Stop the job the moment b.jpg starts copying.
    original = ArchiveJob._copy_and_hash
    def stop_on_b(self, s, tmp):
        if os.path.basename(s) == 'b.jpg':
            self.gate.cancel()
            self.gate.check()          # raises Cancelled
        return original(self, s, tmp)
    ArchiveJob._copy_and_hash = stop_on_b
    try:
        run_job([src], dest)
    finally:
        ArchiveJob._copy_and_hash = original

    st = statuses()
    assert st['b.jpg'] != 'verified', 'b.jpg should not have completed'
    assert not [p for p in archived_files(dest) if 'b.jpg' in p], \
        'a stopped copy must not leave a file in the archive'

    # Start again == resume.
    run_job([src], dest)
    st = statuses()
    assert st['a.jpg'] == 'verified', f'a.jpg regressed: {st}'
    assert st['b.jpg'] == 'verified', f'RESUME BROKEN: b.jpg is {st["b.jpg"]}'
    assert len(archived_files(dest)) == 2, archived_files(dest)


@test
def test_stop_mid_copy_leaves_no_partial_files(work):
    """I-4: nothing half-written, at a real path or anywhere else."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i in range(6):
        write(os.path.join(src, f'v{i}.jpg'),
              jpeg_with_date('2019:08:0%d 12:00:00' % (i + 1),
                             bytes([i]) * 10, size=3 * 1024 * 1024))

    job = ArchiveJob([src], dest)
    # Cancel from another thread while copies are in flight.
    threading.Timer(0.05, job.gate.cancel).start()
    job.run()

    leftovers = [p for p in archived_files(dest)
                 if os.path.basename(p).startswith(scanner.PARTIAL_PREFIX)]
    assert not leftovers, f'orphaned partial files: {leftovers}'

    # Everything that DID land must be byte-perfect.
    for p in archived_files(dest):
        name = os.path.basename(p)
        assert sha(p) == sha(os.path.join(src, name)), f'{name} is corrupt'


@test
def test_kill_then_resume_completes(work):
    """Simulates a hard kill: the process dies, rows stay mid-flight, restart."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i in range(8):
        write(os.path.join(src, f'k{i}.jpg'),
              jpeg_with_date('2017:05:%02d 09:00:00' % (i + 1), bytes([i]) * 500))

    # First run: die after 3 files.
    job = ArchiveJob([src], dest)
    original = ArchiveJob.process_file
    def die_after_three(self, path, name):
        if self.processed >= 3:
            raise KeyboardInterrupt('simulated kill -9')
        return original(self, path, name)
    ArchiveJob.process_file = die_after_three
    try:
        job.run()
    except KeyboardInterrupt:
        pass
    finally:
        ArchiveJob.process_file = original
    db.close_db()

    # Second run: fresh job object, same DB - this is what restarting the app does.
    run_job([src], dest)

    st = statuses()
    assert all(v == 'verified' for v in st.values()), f'after resume: {st}'
    assert len(st) == 8, f'expected 8 files, got {len(st)}'
    survivors = [p for p in archived_files(dest)
                 if os.path.basename(p).startswith(scanner.PARTIAL_PREFIX)]
    assert not survivors, f'resume did not sweep partials: {survivors}'


@needs_exifread
@test
def test_junk_exif_dates_do_not_create_junk_folders(work):
    """'0000:00:00' used to produce a literal 0000/00/00 folder."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'deadclock.jpg'), jpeg_with_date('0000:00:00 00:00:00'))
    write(os.path.join(src, 'ancient.jpg'), jpeg_with_date('1802:01:01 00:00:00'))
    write(os.path.join(src, 'good.jpg'), jpeg_with_date('2014:11:23 16:45:00'))
    run_job([src], dest)

    dirs = {os.path.relpath(os.path.dirname(p), dest) for p in archived_files(dest)}
    assert not any(d.startswith('0000') for d in dirs), f'junk folder created: {dirs}'
    assert not any(d.startswith('1802') for d in dirs), f'junk folder created: {dirs}'
    assert os.path.join('2014', '11', '23') in dirs, f'good date misfiled: {dirs}'
    # The dead-clock files fall back to filesystem mtime, which is today.
    assert len(archived_files(dest)) == 3, 'no file may be dropped over a bad date'


# ==========================================================================
# The invariants
# ==========================================================================

@test
def test_sources_are_never_modified(work):
    """I-1. The whole point of the exercise."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'a', 'one.jpg'), jpeg_with_date('2013:02:03 04:05:06', b'1' * 900))
    write(os.path.join(src, 'b', 'two.jpg'), jpeg_with_date('2013:02:04 04:05:06', b'2' * 900))
    write(os.path.join(src, 'b', 'dup.jpg'), jpeg_with_date('2013:02:04 04:05:06', b'2' * 900))
    write(os.path.join(src, 'b', 'empty.jpg'), b'')

    before = tree_snapshot(src)
    run_job([src], dest)
    after = tree_snapshot(src)
    assert before == after, (
        'source tree changed:\n'
        f'  only before: {set(before) - set(after)}\n'
        f'  only after:  {set(after) - set(before)}\n'
        f'  differing:   {[k for k in before if k in after and before[k] != after[k]]}')


@test
def test_every_archived_file_matches_its_source_hash(work):
    """I-2, checked independently of the app's own bookkeeping."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    names = ['plain.jpg', 'with spaces.jpg', 'br[ack]ets.jpg', 'accénts-ü.jpg',
             "quote's.jpg", 'MiXeDcAsE.JPG']
    for i, n in enumerate(names):
        write(os.path.join(src, n),
              jpeg_with_date('2016:0%d:1%d 08:00:00' % (i % 9 + 1, i % 9), bytes([i]) * 700))
    write(os.path.join(src, 'big.jpg'),
          jpeg_with_date('2016:12:25 08:00:00', b'', size=5 * 1024 * 1024))

    run_job([src], dest)

    src_hashes = {}
    for base, _, files in os.walk(src):
        for n in files:
            src_hashes.setdefault(sha(os.path.join(base, n)), os.path.join(base, n))

    landed = archived_files(dest)
    assert len(landed) == len(names) + 1, f'expected {len(names)+1} files, got {len(landed)}'
    for p in landed:
        assert sha(p) in src_hashes, f'{p} matches no source file'

    st = statuses()
    assert all(v == 'verified' for v in st.values()), st


@test
def test_nothing_is_deleted_from_sources_and_duplicates_are_recorded(work):
    """I-3: duplicates are logged and left alone, never removed."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    payload = jpeg_with_date('2010:10:10 10:10:10', b'SAME' * 300)
    write(os.path.join(src, 'x', 'orig.jpg'), payload)
    write(os.path.join(src, 'y', 'copy.jpg'), payload)
    write(os.path.join(src, 'z', 'copy2.jpg'), payload)

    run_job([src], dest)

    assert len(archived_files(dest)) == 1, 'the same bytes were archived more than once'
    st = statuses()
    assert sorted(st.values()) == ['duplicate', 'duplicate', 'verified'], st

    for rel in ('x/orig.jpg', 'y/copy.jpg', 'z/copy2.jpg'):
        assert os.path.exists(os.path.join(src, *rel.split('/'))), f'{rel} was removed'

    rows = {os.path.basename(r['source_path']): r for r in db.get_recent_files(100)}
    for name in ('copy.jpg', 'copy2.jpg'):
        assert rows[name]['duplicate_of'], f'{name} has no duplicate_of pointer'


@test
def test_corrupted_landing_is_caught_by_verification(work):
    """
    I-2's real teeth: bytes that read fine from the source but land wrong.
    Simulated by corrupting the file between the rename and the verify read.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'c.jpg'), jpeg_with_date('2018:04:04 04:04:04', b'C' * 4000))

    real_replace = os.replace
    def corrupt_after_replace(a, b, *args, **kw):
        real_replace(a, b, *args, **kw)
        with open(b, 'r+b') as f:      # flip a byte behind the app's back
            f.seek(100)
            f.write(b'\xff')
    os.replace = corrupt_after_replace
    try:
        run_job([src], dest)
    finally:
        os.replace = real_replace

    st = statuses()
    assert st['c.jpg'] == 'error', f'corruption was not detected: {st}'
    row = db.get_recent_files(10)[0]
    assert 'verification failed' in (row['error'] or ''), row['error']

    # The bad copy must not be left in the archive looking like a photograph:
    # it is this module's own unverified output and is discarded like a
    # .partial, and the record no longer points at a path that holds it.
    assert archived_files(dest) == [], (
        f'a corrupt copy stayed in the archive as a photograph: {archived_files(dest)}')
    assert row['destination_path'] is None, row['destination_path']

    # Run again with the disk behaving: the good copy takes the plain name,
    # not c_1.jpg beside a corrupt c.jpg nothing would ever look at again.
    run_job([src], dest)
    assert statuses()['c.jpg'] == 'verified'
    names = sorted(os.path.basename(p) for p in archived_files(dest))
    assert names == ['c.jpg'], names
    leftovers = [f for _, _, fs in os.walk(dest) for f in fs if f.startswith(scanner.PARTIAL_PREFIX)]
    assert leftovers == [], f'the quarantined copy was not swept: {leftovers}'


@test
def test_a_source_that_grows_during_the_copy_is_not_archived_short(work):
    """A camera-sync client still writing the file when the walk reached it.
    Read to its end the copy hashes fine, so it used to verify at whatever
    length it had and be recorded at the length the walk saw."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    path = os.path.join(src, 'g.jpg')
    write(path, jpeg_with_date('2018:05:05 05:05:05', b'G' * 4000))

    real_open = open
    grown = {'done': False}

    def grow_on_first_read(file, *args, **kw):
        handle = real_open(file, *args, **kw)
        if not grown['done'] and os.fspath(file).endswith('g.jpg') and 'r' in (args[0] if args else kw.get('mode', 'r')):
            grown['done'] = True
            with real_open(path, 'ab') as more:
                more.write(b'G' * 500)
        return handle

    import builtins
    builtins.open = grow_on_first_read
    try:
        run_job([src], dest)
    finally:
        builtins.open = real_open

    assert statuses()['g.jpg'] == 'error', statuses()
    assert 'changed during the copy' in (db.get_recent_files(10)[0]['error'] or '')
    assert archived_files(dest) == [], 'a short copy was left in the archive'

    run_job([src], dest)            # nothing writing now: it goes through whole
    assert statuses()['g.jpg'] == 'verified'
    [copy] = archived_files(dest)
    assert os.path.getsize(copy) == os.path.getsize(path)


@test
def test_collision_of_different_files_with_the_same_name(work):
    """Same filename, same date, different bytes - both must survive."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'a', 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 12:00:00', b'FIRST' * 100))
    write(os.path.join(src, 'b', 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 12:00:00', b'SECOND' * 100))
    run_job([src], dest)

    landed = sorted(os.path.basename(p) for p in archived_files(dest))
    assert landed == ['IMG_0001.jpg', 'IMG_0001_1.jpg'], landed
    hashes = {sha(p) for p in archived_files(dest)}
    assert len(hashes) == 2, 'the two distinct files collapsed into one'


@test
def test_rerun_is_idempotent(work):
    """Running twice must not duplicate, re-copy, or churn the archive."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i in range(5):
        write(os.path.join(src, f'i{i}.jpg'),
              jpeg_with_date('2021:03:1%d 11:00:00' % i, bytes([i]) * 600))

    run_job([src], dest)
    first = tree_snapshot(dest)
    run_job([src], dest)
    second = tree_snapshot(dest)
    assert first == second, f'second run changed the archive:\n{first}\n{second}'
    assert len(first) == 5, first


@test
def test_google_takeout_dates_are_recovered(work):
    """
    Without this, a Takeout export lands entirely in the month it was exported.
    The JPEG here deliberately carries no EXIF date.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'IMG_9.jpg'), b'\xff\xd8\xff\xdb\x00\x43\x00' + b'N' * 400 + b'\xff\xd9')
    ts = int(time.mktime(time.strptime('2009-05-17', '%Y-%m-%d')))
    write(os.path.join(src, 'IMG_9.jpg.json'),
          ('{"photoTakenTime": {"timestamp": "%d"}}' % ts).encode())

    run_job([src], dest)
    landed = [p for p in archived_files(dest) if p.endswith('IMG_9.jpg')]
    assert landed, archived_files(dest)
    rel = os.path.relpath(os.path.dirname(landed[0]), dest)
    assert rel == os.path.join('2009', '05', '17'), f'filed under {rel}, expected 2009/05/17'

    row = [r for r in db.get_recent_files(50) if r['filename'] == 'IMG_9.jpg'][0]
    assert row['status'] == 'verified'


@test
def test_sidecars_travel_with_their_photo(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'P1.jpg'), jpeg_with_date('2022:02:22 22:22:22', b'P' * 300))
    write(os.path.join(src, 'P1.xmp'), b'<x:xmpmeta>face regions</x:xmpmeta>')
    run_job([src], dest)

    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert 'P1.jpg' in landed, landed
    assert 'P1.xmp' in landed, f'the XMP sidecar was left behind: {landed}'


@test
def test_undated_files_go_to_a_named_folder_not_a_broken_path(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    p = write(os.path.join(src, 'nodate.jpg'), b'\xff\xd8' + b'Z' * 300 + b'\xff\xd9')
    # Force an implausible mtime so the filesystem fallback is rejected too.
    os.utime(p, (0, 0))
    run_job([src], dest)

    landed = archived_files(dest)
    assert len(landed) == 1, landed
    rel = os.path.relpath(os.path.dirname(landed[0]), dest)
    # Under the folder it came from (`s`), so undated imports do not all share
    # one flat folder.
    assert rel == os.path.join(scanner.UNDATED_FOLDER, 's'), f'filed under {rel}'


@needs_exifread
@test
def test_existing_archive_file_is_adopted_not_recopied(work):
    """
    Pointing the tool at an archive a previous run already filled (or one built
    by hand) must recognise the existing file as this source's verified copy,
    not label it a duplicate of a file nothing has a record of.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    payload = jpeg_with_date('2013:08:19 14:00:00', b'ALREADY THERE' * 50)
    write(os.path.join(src, 'old.jpg'), payload)
    # Pre-place the file exactly where the archive would put it.
    write(os.path.join(dest, '2013', '08', '19', 'old.jpg'), payload)

    run_job([src], dest)

    st = statuses()
    assert st['old.jpg'] == 'verified', f'existing archive copy not adopted: {st}'
    assert len(archived_files(dest)) == 1, archived_files(dest)
    row = db.get_recent_files(10)[0]
    assert row['dest_hash'] == hashlib.sha256(payload).hexdigest()


@test
def test_legacy_duplicate_rows_without_an_original_are_requeued(work):
    """
    Upgrade repair. The old scanner could mark a file 'duplicate' against a row
    that never reached the archive. Those rows carry no duplicate_of, cannot be
    trusted, and must be re-evaluated rather than treated as terminal.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    payload = jpeg_with_date('2011:11:11 11:11:11', b'ORPHAN' * 80)
    p = write(os.path.join(src, 'orphan.jpg'), payload)

    # Forge the legacy state: marked duplicate, no original recorded, never copied.
    conn = db.get_db()
    conn.execute("INSERT INTO files(source_path, filename, size, status) "
                 "VALUES(?,?,?,'duplicate')", (p, 'orphan.jpg', os.path.getsize(p)))
    conn.commit()
    db.set_config('schema_version', '1')          # force the migration to run
    db.init_db()

    row = db.get_recent_files(10)[0]
    assert row['status'] == 'pending', f'legacy row was trusted: {row["status"]}'

    run_job([src], dest)
    assert statuses()['orphan.jpg'] == 'verified', statuses()
    assert len(archived_files(dest)) == 1, 'the requeued file never reached the archive'


@test
def test_genuine_duplicate_rows_survive_the_migration(work):
    """The repair must not undo correct history - only the untrustworthy rows."""
    src = os.path.join(work, 's')
    p = write(os.path.join(src, 'real_dup.jpg'), jpeg_with_date('2011:01:01 01:01:01'))
    conn = db.get_db()
    conn.execute("INSERT INTO files(source_path, filename, size, status, duplicate_of) "
                 "VALUES(?,?,?,'duplicate',?)",
                 (p, 'real_dup.jpg', os.path.getsize(p), '/somewhere/original.jpg'))
    conn.commit()
    db.set_config('schema_version', '1')
    db.init_db()
    assert db.get_recent_files(10)[0]['status'] == 'duplicate', \
        'a duplicate with a recorded original was wrongly requeued'


# ==========================================================================
# No quality loss, nothing skipped
# ==========================================================================

@test
def test_files_are_copied_bit_for_bit_with_no_recompression(work):
    """
    The archived file must be the *same bytes*, not a re-encode. Nothing in the
    pipeline decodes or re-writes image data - a photo is streamed through
    unchanged - and this asserts it at the byte level for every kind of file.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    originals = {
        'photo.jpg': jpeg_with_date('2015:05:05 05:05:05', b'\x01\x02\x03' * 900),
        'shot.png':  b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 12,
        'clip.mp4':  b'\x00\x00\x00\x18ftypmp42' + bytes(range(256)) * 40,
        'song.mp3':  b'ID3\x04\x00\x00\x00\x00\x00\x00' + bytes(range(256)) * 30,
        'raw.dng':   b'II*\x00' + bytes(range(256)) * 50,
    }
    for name, data in originals.items():
        write(os.path.join(src, name), data)

    run_job([src], dest)

    landed = {os.path.basename(p): p for p in archived_files(dest)}
    assert set(landed) == set(originals), landed
    for name, data in originals.items():
        with open(landed[name], 'rb') as f:
            got = f.read()
        assert got == data, f'{name} was altered in transit ({len(got)} vs {len(data)} bytes)'
        assert os.path.getsize(landed[name]) == len(data), f'{name} changed size'


@needs_exifread
@test
def test_exif_survives_the_copy_intact(work):
    """
    Byte-identical implies metadata-identical, but this checks it through a real
    parser too, because "did my capture dates survive" is the question that
    actually matters to the person running this.
    """
    exifread = pytest.importorskip(
        "exifread",
        reason="exifread is an optional extra; see requirements.txt")
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    p = write(os.path.join(src, 'meta.jpg'),
              jpeg_with_date('2007:09:14 18:30:45', b'M' * 800))

    def tags_of(path):
        with open(path, 'rb') as f:
            t = exifread.process_file(f, details=False)
        return {k: str(v) for k, v in t.items()}

    before = tags_of(p)
    assert before, 'the fixture carries no EXIF, so this test would prove nothing'

    run_job([src], dest)
    archived = archived_files(dest)[0]
    after = tags_of(archived)

    assert before == after, (
        f'EXIF changed:\n  lost: {set(before) - set(after)}\n'
        f'  changed: {[k for k in before if after.get(k) != before[k]]}')
    assert 'EXIF DateTimeOriginal' in after
    assert after['EXIF DateTimeOriginal'] == '2007:09:14 18:30:45'
    # and the file was filed by that date, not by today
    rel = os.path.relpath(os.path.dirname(archived), dest)
    assert rel == os.path.join('2007', '09', '14'), rel


@test
def test_filesystem_timestamps_are_preserved(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    p = write(os.path.join(src, 't.jpg'), jpeg_with_date('2016:04:01 12:00:00', b'T' * 500))
    stamp = time.mktime(time.strptime('2016-04-01 12:00:00', '%Y-%m-%d %H:%M:%S'))
    os.utime(p, (stamp, stamp))

    run_job([src], dest)
    archived = archived_files(dest)[0]
    assert abs(os.path.getmtime(archived) - stamp) < 2, (
        f'mtime not preserved: {os.path.getmtime(archived)} vs {stamp}')


@test
def test_media_with_an_unknown_extension_is_still_archived(work):
    """
    The extension list can never be complete. Anything it misses is caught by
    reading the file's first bytes, so no photo, video or audio file is ever
    passed over in silence.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    cases = {
        'IMG_0042':          jpeg_with_date('2009:02:02 02:02:02', b'N' * 400),  # no extension
        'CLIP.DAT':          b'\x00\x00\x00\x18ftypisom' + b'V' * 600,           # camcorder
        'renamed_photo.txt': b'\x89PNG\r\n\x1a\n' + b'P' * 600,                  # wrong extension
        'voice.rec':         b'ID3\x04\x00\x00\x00\x00\x00\x00' + b'A' * 600,    # odd recorder
        'movie.bin':         b'\x1aE\xdf\xa3' + b'K' * 600,                      # Matroska
        'notes.txt':         b'this is genuinely just text, leave it alone',     # NOT media
    }
    for name, data in cases.items():
        write(os.path.join(src, name), data)

    job = ArchiveJob([src], dest)
    job.run()

    landed = {os.path.basename(p) for p in archived_files(dest)}
    expected = set(cases) - {'notes.txt'}
    assert landed == expected, f'missed: {expected - landed}; unexpected: {landed - expected}'
    assert job.sniffed == len(expected), f'sniff count {job.sniffed}'
    assert job.non_media == 1, f'non-media count {job.non_media}'
    assert 'notes.txt' not in landed, 'a plain text file was archived as media'

    for name in expected:
        with open(os.path.join(src, name), 'rb') as f:
            original = f.read()
        match = [p for p in archived_files(dest) if os.path.basename(p) == name][0]
        with open(match, 'rb') as f:
            assert f.read() == original, f'{name} was altered'


@test
def test_the_extension_lists_cover_the_common_formats(work):
    """A regression guard: these must never quietly fall out of the lists."""
    must_be_images = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'tif', 'tiff', 'heic',
                      'heif', 'webp', 'avif', 'svg', 'psd', 'ico', 'jxl', 'dng',
                      'cr2', 'cr3', 'nef', 'arw', 'orf', 'raf', 'rw2', 'pef',
                      'srw', 'x3f', '3fr', 'iiq', 'kdc', 'raw']
    must_be_video = ['mp4', 'mov', 'avi', 'mkv', 'webm', 'wmv', 'flv', '3gp',
                     'mpg', 'mpeg', 'm4v', 'mts', 'm2ts', 'vob', 'ts', 'mxf',
                     'divx', 'rm', 'dv', 'ogv']
    must_be_audio = ['mp3', 'wav', 'flac', 'aac', 'ogg', 'opus', 'wma', 'm4a',
                     'aiff', 'aif', 'amr', 'ape', 'wv', 'ac3', 'mid', 'midi',
                     'caf', 'mka', 'au']
    for ext in must_be_images:
        assert scanner.classify_by_extension(f'x.{ext}') == 'image', ext
        assert scanner.classify_by_extension(f'X.{ext.upper()}') == 'image', ext
    for ext in must_be_video:
        assert scanner.classify_by_extension(f'x.{ext}') == 'video', ext
    for ext in must_be_audio:
        assert scanner.classify_by_extension(f'x.{ext}') == 'audio', ext
    assert scanner.classify_by_extension('notes.txt') is None
    assert scanner.classify_by_extension('setup.exe') is None


@test
def test_sniffer_identifies_formats_correctly(work):
    samples = {
        'image': [b'\xff\xd8\xff\xe0' + b'\x00' * 20,
                  b'\x89PNG\r\n\x1a\n' + b'\x00' * 20,
                  b'GIF89a' + b'\x00' * 20,
                  b'II*\x00' + b'\x00' * 20,
                  b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 20,
                  b'\x00\x00\x00\x18ftypheic' + b'\x00' * 12,
                  b'8BPS' + b'\x00' * 20,
                  b'FUJIFILMCCD-RAW' + b'\x00' * 20],
        'video': [b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 12,
                  b'\x1aE\xdf\xa3' + b'\x00' * 20,
                  b'RIFF\x00\x00\x00\x00AVI ' + b'\x00' * 20,
                  b'\x30\x26\xb2\x75' + b'\x00' * 20,
                  b'FLV\x01' + b'\x00' * 20],
        'audio': [b'ID3\x04\x00\x00\x00\x00\x00\x00' + b'\x00' * 20,
                  b'fLaC' + b'\x00' * 20,
                  b'OggS' + b'\x00' * 20,
                  b'RIFF\x00\x00\x00\x00WAVE' + b'\x00' * 20,
                  b'\xff\xfb\x90\x00' + b'\x00' * 20,
                  b'MThd' + b'\x00' * 20,
                  b'\x00\x00\x00\x18ftypM4A ' + b'\x00' * 12],
        None:    [b'just some text here at all events',
                  b'MZ\x90\x00' + b'\x00' * 20,
                  b'PK\x03\x04' + b'\x00' * 20,
                  b'%PDF-1.7' + b'\x00' * 20],
    }
    for expected, blobs in samples.items():
        for i, blob in enumerate(blobs):
            p = write(os.path.join(work, f'{expected}_{i}.bin'), blob)
            got = scanner.sniff_media(p)
            assert got == expected, f'{blob[:12]!r} sniffed as {got}, expected {expected}'


# ==========================================================================
# Dry run, audit, capacity, filters
# ==========================================================================

@test
def test_dry_run_writes_absolutely_nothing(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i in range(4):
        write(os.path.join(src, f'd{i}.jpg'),
              jpeg_with_date('2019:0%d:12 09:00:00' % (i + 1), bytes([i]) * 400))
    before = tree_snapshot(src)

    job = ArchiveJob([src], dest, mode=scanner.MODE_DRY_RUN)
    job.run()

    assert tree_snapshot(src) == before, 'a dry run modified the source tree'
    assert not os.path.exists(dest) or not archived_files(dest), \
        f'a dry run wrote to the destination: {archived_files(dest)}'
    st = statuses()
    assert len(st) == 4 and all(v == 'planned' for v in st.values()), st

    rows = db.get_recent_files(10)
    assert all(r['destination_path'] for r in rows), 'the plan has no target paths'


@test
def test_dry_run_predictions_do_not_block_the_real_run(work):
    """Plan rows are predictions. A real run must not mistake them for history."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i in range(3):
        write(os.path.join(src, f'r{i}.jpg'),
              jpeg_with_date('2020:0%d:05 09:00:00' % (i + 1), bytes([i]) * 400))

    ArchiveJob([src], dest, mode=scanner.MODE_DRY_RUN).run()
    assert all(v == 'planned' for v in statuses().values())

    run_job([src], dest)
    st = statuses()
    assert all(v == 'verified' for v in st.values()), f'plan rows blocked the run: {st}'
    assert len(archived_files(dest)) == 3, archived_files(dest)


@test
def test_dry_run_predicts_the_same_paths_the_real_run_uses(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'a.jpg'), jpeg_with_date('2014:03:09 08:00:00', b'A' * 300))
    write(os.path.join(src, 'b', 'a.jpg'), jpeg_with_date('2014:03:09 08:00:00', b'B' * 300))
    write(os.path.join(src, 'dupe.jpg'), jpeg_with_date('2014:03:09 08:00:00', b'A' * 300))

    ArchiveJob([src], dest, mode=scanner.MODE_DRY_RUN).run()
    predicted = {r['source_path']: (r['status'], r['destination_path'])
                 for r in db.get_recent_files(50)}

    run_job([src], dest)
    actual = {r['source_path']: (r['status'], r['destination_path'])
              for r in db.get_recent_files(50)}

    # A prediction is only worth anything if it matches what actually happens.
    equivalent = {'planned': 'verified', 'plan-duplicate': 'duplicate',
                  'plan-skip': 'skipped'}
    assert set(predicted) == set(actual), 'the dry run saw a different set of files'
    for path, (pstatus, ppath) in predicted.items():
        astatus, apath = actual[path]
        assert equivalent[pstatus] == astatus, (
            f'{os.path.basename(path)}: predicted {pstatus}, actually {astatus}')
        assert ppath == apath, (
            f'{os.path.basename(path)}: predicted {ppath}, actually landed at {apath}')

    counts = sorted(s for s, _ in actual.values())
    assert counts == ['duplicate', 'verified', 'verified'], counts


@test
def test_audit_detects_a_file_changed_after_archiving(work):
    """Bit rot, a bad cable, or someone editing the archive by hand."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'x.jpg'), jpeg_with_date('2016:02:29 12:00:00', b'X' * 900))
    write(os.path.join(src, 'y.jpg'), jpeg_with_date('2016:03:01 12:00:00', b'Y' * 900))
    run_job([src], dest)
    assert all(v == 'verified' for v in statuses().values())

    victim = [p for p in archived_files(dest) if p.endswith('x.jpg')][0]
    before = sha(victim)
    with open(victim, 'r+b') as f:               # flip a byte to a different value
        f.seek(50)
        original = f.read(1)
        f.seek(50)
        f.write(bytes([original[0] ^ 0xFF]))
    assert sha(victim) != before, 'the test failed to actually alter the file'

    ArchiveJob([src], dest, mode=scanner.MODE_VERIFY).run()
    st = statuses()
    assert st['x.jpg'] == 'error', f'audit missed the altered file: {st}'
    assert st['y.jpg'] == 'verified', f'audit falsely failed a good file: {st}'
    row = [r for r in db.get_recent_files(50) if r['filename'] == 'x.jpg'][0]
    assert 'audit failed' in (row['error'] or ''), row['error']


@test
def test_audit_detects_a_file_deleted_from_the_archive(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'g.jpg'), jpeg_with_date('2015:07:07 07:07:07', b'G' * 500))
    run_job([src], dest)
    os.remove(archived_files(dest)[0])

    ArchiveJob([src], dest, mode=scanner.MODE_VERIFY).run()
    assert statuses()['g.jpg'] == 'error', statuses()
    row = db.get_recent_files(10)[0]
    assert 'missing' in (row['error'] or '').lower(), row['error']


@test
def test_audit_passes_a_healthy_archive(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i in range(5):
        write(os.path.join(src, f'h{i}.jpg'),
              jpeg_with_date('2012:0%d:0%d 10:00:00' % (i + 1, i + 1), bytes([i]) * 400))
    run_job([src], dest)
    job = ArchiveJob([src], dest, mode=scanner.MODE_VERIFY)
    job.run()
    assert all(v == 'verified' for v in statuses().values()), statuses()
    assert 'audit complete' in (db.latest_job()['message'] or ''), db.latest_job()
    assert '5 files matched' in db.latest_job()['message'], db.latest_job()['message']


@test
def test_media_type_filter_excludes_video_and_audio(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'photo.jpg'), jpeg_with_date('2018:01:01 00:00:00', b'P' * 300))
    write(os.path.join(src, 'clip.mp4'), b'\x00\x00\x00\x18ftypmp42' + b'V' * 400)
    write(os.path.join(src, 'song.mp3'), b'ID3' + b'M' * 400)

    ArchiveJob([src], dest, media_types={'image'}).run()
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'photo.jpg'}, landed

    ArchiveJob([src], dest, media_types={'image', 'video', 'audio'}).run()
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'photo.jpg', 'clip.mp4', 'song.mp3'}, landed


@test
def test_every_subfolder_is_scanned_however_deep(work):
    """
    Selecting a folder scans its entire subtree. Selecting a drive root means
    every folder on the drive, at any depth.
    """
    src, dest = os.path.join(work, 'DriveRoot'), os.path.join(work, 'd')
    expected = set()

    # loose files at the top
    for i in range(2):
        name = f'root{i}.jpg'
        write(os.path.join(src, name), jpeg_with_date('2001:01:0%d 00:00:00' % (i + 1),
                                                      bytes([i]) * 400))
        expected.add(name)

    # a broad spread of top-level folders, as a drive root would have
    for i, folder in enumerate(['Users', 'Pictures', 'Backup 2009', 'My Documents',
                                'DCIM', 'a folder with spaces', 'accents-u']):
        name = f'f{i}.jpg'
        write(os.path.join(src, folder, 'inner', name),
              jpeg_with_date('2002:0%d:01 00:00:00' % (i % 9 + 1), bytes([i + 10]) * 400))
        expected.add(name)

    # and one file buried 15 levels down
    deep = src
    for level in range(15):
        deep = os.path.join(deep, f'level{level}')
    write(os.path.join(deep, 'buried.jpg'),
          jpeg_with_date('2003:03:03 00:00:00', b'B' * 400))
    expected.add('buried.jpg')

    job = ArchiveJob([src], dest)
    job.run()

    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == expected, f'missed: {expected - landed}; extra: {landed - expected}'
    assert job.deepest >= 15, f'walk only reached depth {job.deepest}'
    assert all(v == 'verified' for v in statuses().values()), statuses()


@test
def test_a_folder_that_cannot_be_listed_is_reported_not_silently_skipped(work):
    """
    os.walk's default swallows a listing error and skips the ENTIRE subtree
    below it without a word, while the run still reports success. On a
    whole-drive scan that is potentially thousands of photos vanishing into a
    report that says 'complete'.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'reachable.jpg'),
          jpeg_with_date('2011:01:01 00:00:00', b'R' * 400))
    for i in range(5):
        write(os.path.join(src, 'Vacation2009', 'sub', f'v{i}.jpg'),
              jpeg_with_date('2009:07:04 00:00:00', bytes([i]) * 400))

    real_scandir = os.scandir

    def flaky(path='.'):
        if 'Vacation2009' in str(path):
            raise PermissionError(13, 'Permission denied', str(path))
        return real_scandir(path)

    os.scandir = flaky
    try:
        job = ArchiveJob([src], dest)
        job.run()
    finally:
        os.scandir = real_scandir

    assert job.unreadable_dirs, 'the unreadable folder was not recorded'
    assert any('Vacation2009' in p for p, _ in job.unreadable_dirs), job.unreadable_dirs

    message = db.latest_job()['message'] or ''
    assert 'could not be read' in message, message

    errors = [r for r in db.get_recent_files(100) if r['status'] == 'error']
    assert errors, 'nothing appears in the report'
    assert any('Vacation2009' in (r['source_path'] or '') for r in errors), errors
    assert any('nothing inside it was scanned' in (r['error'] or '') for r in errors), errors


@test
def test_a_readable_sibling_still_completes_when_one_folder_fails(work):
    """One bad folder must not abort the rest of the drive."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for folder in ('good1', 'bad', 'good2'):
        # distinct bytes per folder, or the second file is a genuine duplicate
        # of the first and the test would measure dedup instead of recovery
        write(os.path.join(src, folder, f'{folder}.jpg'),
              jpeg_with_date('2012:02:02 00:00:00', folder.encode() * 90))

    real_scandir = os.scandir

    def flaky(path='.'):
        if os.path.basename(str(path)) == 'bad':
            raise OSError(5, 'Input/output error', str(path))
        return real_scandir(path)

    os.scandir = flaky
    try:
        ArchiveJob([src], dest).run()
    finally:
        os.scandir = real_scandir

    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'good1.jpg', 'good2.jpg'}, landed


@test
def test_deep_scan_finds_a_photo_with_a_document_extension(work):
    """Default mode: contents win over the extension, whatever it claims."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'renamed.txt'), b'\x89PNG\r\n\x1a\n' + b'P' * 600)
    write(os.path.join(src, 'notes.txt'), b'genuinely just text')

    ArchiveJob([src], dest, deep_scan=True).run()
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'renamed.txt'}, landed


@test
def test_fast_scan_trades_that_away_but_keeps_ambiguous_types(work):
    """
    Fast scan trusts the extension for known non-media types. The misnamed file
    is missed - that is the documented trade - but files with no extension and
    ambiguous ones like .dat are still sniffed in both modes.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'renamed.txt'), b'\x89PNG\r\n\x1a\n' + b'P' * 600)
    write(os.path.join(src, 'CLIP.DAT'), b'\x00\x00\x00\x18ftypisom' + b'V' * 600)
    write(os.path.join(src, 'IMG_0042'), jpeg_with_date('2010:04:04 04:04:04', b'N' * 400))

    ArchiveJob([src], dest, deep_scan=False).run()
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'CLIP.DAT', 'IMG_0042'}, landed
    assert 'renamed.txt' not in landed


@test
def test_system_folders_are_skipped_and_counted(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, '$RECYCLE.BIN', 'deleted.jpg'),
          jpeg_with_date('2015:01:01 00:00:00', b'R' * 400))
    write(os.path.join(src, 'System Volume Information', 'sys.jpg'),
          jpeg_with_date('2015:01:02 00:00:00', b'S' * 400))
    write(os.path.join(src, 'keep.jpg'), jpeg_with_date('2015:01:03 00:00:00', b'K' * 400))

    job = ArchiveJob([src], dest)
    job.run()
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'keep.jpg'}, landed
    assert job.skipped_system_dirs == 2, job.skipped_system_dirs


@test
def test_an_archive_used_as_a_source_is_fully_copied_into_a_new_archive(work):
    """
    THE MIGRATION BUG. Archive A is built, then handed back as the source for a
    new archive B. Every file must land in B.

    Before the fix, "duplicate" meant "these bytes appear somewhere in this
    database" rather than "these bytes are already in THIS archive". A's files
    matched the rows from the run that built A - so all of them were written off
    as duplicates of copies sitting in A, and B was left completely empty while
    the run reported success. Wiping A on the strength of that report would have
    destroyed everything.
    """
    drive = os.path.join(work, 'OldDrive')
    archive_a = os.path.join(work, 'ArchiveA')
    archive_b = os.path.join(work, 'ArchiveB')
    for i in range(4):
        write(os.path.join(drive, f'p{i}.jpg'),
              jpeg_with_date('201%d:03:0%d 10:00:00' % (i + 2, i + 1), bytes([i]) * 600))

    run_job([drive], archive_a)
    assert len(archived_files(archive_a)) == 4, archived_files(archive_a)

    run_job([archive_a], archive_b)

    landed = sorted(os.path.basename(p) for p in archived_files(archive_b))
    assert landed == ['p0.jpg', 'p1.jpg', 'p2.jpg', 'p3.jpg'], (
        f'the migration lost files: only {landed} reached the new archive')

    # and the bytes are right, not just the names
    a_hashes = {sha(p) for p in archived_files(archive_a)}
    b_hashes = {sha(p) for p in archived_files(archive_b)}
    assert a_hashes == b_hashes, 'the migrated copies do not match the originals'

    # the source archive is left completely alone
    assert len(archived_files(archive_a)) == 4, 'the source archive was modified'


@test
def test_merging_two_archives_adds_the_new_and_marks_the_overlap(work):
    """
    Fold archive A into archive B where the two share some photos: the shared
    ones are recorded as duplicates of the copies already in B, and only the
    genuinely new ones are copied.
    """
    a_src = os.path.join(work, 'Card1')
    b_src = os.path.join(work, 'Card2')
    archive_a = os.path.join(work, 'ArchiveA')
    archive_b = os.path.join(work, 'ArchiveB')

    shared = [jpeg_with_date('2012:0%d:05 10:00:00' % (i + 1), b'SHARED%d' % i * 200)
              for i in range(2)]
    for i, payload in enumerate(shared):
        write(os.path.join(a_src, f's{i}.jpg'), payload)
        write(os.path.join(b_src, f's{i}.jpg'), payload)
    write(os.path.join(a_src, 'only1.jpg'), jpeg_with_date('2013:01:01 10:00:00', b'ONE' * 400))
    write(os.path.join(b_src, 'only2.jpg'), jpeg_with_date('2014:01:01 10:00:00', b'TWO' * 400))

    run_job([a_src], archive_a)
    run_job([b_src], archive_b)
    assert len(archived_files(archive_b)) == 3, archived_files(archive_b)

    run_job([archive_a], archive_b)

    rows = {os.path.relpath(r['source_path'], archive_a): r
            for r in db.get_recent_files(200)
            if r['source_path'].startswith(archive_a)}
    statuses_seen = sorted(r['status'] for r in rows.values())
    assert statuses_seen == ['duplicate', 'duplicate', 'verified'], statuses_seen

    landed = sorted(os.path.basename(p) for p in archived_files(archive_b))
    assert landed == ['only1.jpg', 'only2.jpg', 's0.jpg', 's1.jpg'], landed

    # each duplicate points at the copy already living in the NEW archive
    for rel, row in rows.items():
        if row['status'] == 'duplicate':
            assert row['destination_path'].startswith(archive_b), (
                f'{rel} was matched against a copy outside the target archive: '
                f'{row["destination_path"]}')


@test
def test_a_duplicate_must_live_in_the_archive_being_written(work):
    """
    The precise rule: 'duplicate' means the bytes are already in THIS
    destination. A copy sitting in some other archive is irrelevant and must
    never suppress a copy into the one being built now.
    """
    src = os.path.join(work, 's')
    first = os.path.join(work, 'first')
    second = os.path.join(work, 'second')
    write(os.path.join(src, 'a.jpg'), jpeg_with_date('2015:05:05 05:05:05', b'A' * 500))

    run_job([src], first)
    row = db.get_recent_files(10)[0]
    assert row['status'] == 'verified' and row['destination_path'].startswith(first)

    # A verified row exists with this hash, but its copy is in `first`.
    # Asking about `second` must not find it.
    assert db.find_verified_duplicate(row['file_hash'], '/nowhere',
                                      destination_root=second) is None
    assert db.find_verified_duplicate(row['file_hash'], '/nowhere',
                                      destination_root=first) is not None


@test
def test_migrating_twice_is_idempotent(work):
    """Re-running the migration must not duplicate or renumber anything."""
    drive = os.path.join(work, 'drive')
    archive_a = os.path.join(work, 'A')
    archive_b = os.path.join(work, 'B')
    for i in range(3):
        write(os.path.join(drive, f'q{i}.jpg'),
              jpeg_with_date('2016:0%d:07 10:00:00' % (i + 1), bytes([i + 9]) * 600))

    run_job([drive], archive_a)
    run_job([archive_a], archive_b)
    snapshot = tree_snapshot(archive_b)
    assert len(snapshot) == 3, snapshot

    run_job([archive_a], archive_b)
    assert tree_snapshot(archive_b) == snapshot, 'the second migration changed the archive'


@test
def test_the_original_drive_and_the_archive_can_both_be_sources(work):
    """
    Belt and braces: hand the tool both the old drive AND the archive built
    from it, targeting a fresh archive. Every distinct photo appears once.
    """
    drive = os.path.join(work, 'drive')
    archive_a = os.path.join(work, 'A')
    archive_b = os.path.join(work, 'B')
    for i in range(3):
        write(os.path.join(drive, f'r{i}.jpg'),
              jpeg_with_date('2017:0%d:08 10:00:00' % (i + 1), bytes([i + 40]) * 600))

    run_job([drive], archive_a)
    run_job([drive, archive_a], archive_b)

    landed = sorted(os.path.basename(p) for p in archived_files(archive_b))
    assert landed == ['r0.jpg', 'r1.jpg', 'r2.jpg'], landed
    assert len({sha(p) for p in archived_files(archive_b)}) == 3, 'photos collapsed'


@test
def test_same_name_same_date_different_bytes_all_survive(work):
    """
    Three cameras all produce IMG_0001.jpg on the same day. Every one of them
    is a different photo, so every one must reach the archive under a name of
    its own - never overwritten, never dropped.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    payloads = {}
    for card in ('card_a', 'card_b', 'card_c'):
        payloads[card] = jpeg_with_date('2014:07:04 12:00:00', card.encode() * 200)
        write(os.path.join(src, card, 'IMG_0001.jpg'), payloads[card])

    run_job([src], dest)

    landed = sorted(os.path.basename(p) for p in archived_files(dest))
    assert landed == ['IMG_0001.jpg', 'IMG_0001_1.jpg', 'IMG_0001_2.jpg'], landed

    # every original's bytes are present exactly once
    on_disk = {sha(p) for p in archived_files(dest)}
    for card, data in payloads.items():
        assert hashlib.sha256(data).hexdigest() in on_disk, f'{card} was lost'
    assert len(on_disk) == 3, 'two distinct photos collapsed into one'
    assert all(v == 'verified' for v in statuses().values()), statuses()


@test
def test_same_name_same_date_identical_bytes_is_a_duplicate_not_a_suffix(work):
    """A real duplicate must not become IMG_0001_1.jpg - that is just clutter."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    payload = jpeg_with_date('2014:07:04 12:00:00', b'SAME' * 200)
    write(os.path.join(src, 'card_a', 'IMG_0001.jpg'), payload)
    write(os.path.join(src, 'backup', 'IMG_0001.jpg'), payload)

    run_job([src], dest)

    landed = [os.path.basename(p) for p in archived_files(dest)]
    assert landed == ['IMG_0001.jpg'], landed
    # keyed on the full source path: both files share a basename, so the
    # basename-keyed statuses() helper would collapse them into one entry
    rows = {r['source_path']: r['status'] for r in db.get_recent_files(50)}
    assert sorted(rows.values()) == ['duplicate', 'verified'], rows

    dup = [r for r in db.get_recent_files(50) if r['status'] == 'duplicate'][0]
    assert dup['duplicate_of'], 'the duplicate does not record what it matched'


@test
def test_the_suffix_keeps_the_extension_usable(work):
    """IMG_0001_1.jpg, not IMG_0001.jpg_1 - the file must still open."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i, tag in enumerate((b'A', b'B')):
        write(os.path.join(src, f'f{i}', 'holiday.photo.jpeg'),
              jpeg_with_date('2015:08:08 08:08:08', tag * 300))
    run_job([src], dest)

    landed = sorted(os.path.basename(p) for p in archived_files(dest))
    assert landed == ['holiday.photo.jpeg', 'holiday.photo_1.jpeg'], landed
    for name in landed:
        assert name.endswith('.jpeg'), name


@test
def test_a_renamed_collision_takes_its_sidecar_with_it(work):
    """The XMP must follow its photo to the suffixed name, not be orphaned."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'a', 'P1.jpg'), jpeg_with_date('2016:06:06 06:06:06', b'A' * 300))
    write(os.path.join(src, 'a', 'P1.xmp'), b'<x>first</x>')
    write(os.path.join(src, 'b', 'P1.jpg'), jpeg_with_date('2016:06:06 06:06:06', b'B' * 300))
    write(os.path.join(src, 'b', 'P1.xmp'), b'<x>second</x>')

    run_job([src], dest)

    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert 'P1.jpg' in landed and 'P1_1.jpg' in landed, landed
    assert 'P1.xmp' in landed, f'first sidecar missing: {landed}'
    assert 'P1_1.xmp' in landed, f'second sidecar was not renamed with its photo: {landed}'

    folder = os.path.dirname([p for p in archived_files(dest) if p.endswith('P1.jpg')][0])
    assert open(os.path.join(folder, 'P1.xmp'), 'rb').read() == b'<x>first</x>'
    assert open(os.path.join(folder, 'P1_1.xmp'), 'rb').read() == b'<x>second</x>'


@test
def test_many_collisions_do_not_cost_quadratic_hashing(work):
    """
    Resolving a collision asks "are these the same bytes?". Answering it by
    re-reading every neighbour every time is quadratic: 120 same-named photos
    cost 7379 hash operations and read 1.4 GB to move 24 MB. Size is checked
    first, and a digest already on record is reused, which brings it back to
    two hashes per file - one on the way out, one to verify what landed.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    n = 40
    for i in range(n):
        write(os.path.join(src, f'f{i:03d}', 'image.jpg'),
              jpeg_with_date('2010:05:05 12:00:00', bytes([i % 251]) * 20000))

    calls = {'n': 0}
    original = scanner.hash_file

    def counting(path, gate=None):
        calls['n'] += 1
        return original(path, gate)

    scanner.hash_file = counting
    try:
        run_job([src], dest)
    finally:
        scanner.hash_file = original

    assert len(archived_files(dest)) == n, len(archived_files(dest))
    # two per file, plus a little slack; quadratic would be ~n^2/2 = 800+
    assert calls['n'] <= n * 3, (
        f'{calls["n"]} hash operations for {n} files - collision handling has '
        f'gone quadratic again')


@needs_exifread
@test
def test_a_pre_existing_archive_file_is_still_hashed_properly(work):
    """
    The digest shortcut only applies to files this tool recorded. Something
    already sitting in the archive from elsewhere has no record, so it must be
    read and hashed rather than assumed to differ.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    payload = jpeg_with_date('2017:09:09 09:09:09', b'PRESENT' * 100)
    write(os.path.join(src, 'x.jpg'), payload)
    # place identical bytes in the archive by hand - no database row for it
    write(os.path.join(dest, '2017', '09', '09', 'x.jpg'), payload)

    run_job([src], dest)

    assert len(archived_files(dest)) == 1, archived_files(dest)
    assert statuses()['x.jpg'] == 'verified', statuses()


@needs_exifread
@test
def test_a_different_pre_existing_file_of_the_same_name_is_suffixed(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'y.jpg'), jpeg_with_date('2017:09:09 09:09:09', b'MINE' * 100))
    write(os.path.join(dest, '2017', '09', '09', 'y.jpg'),
          jpeg_with_date('2017:09:09 09:09:09', b'THEIRS' * 100))

    run_job([src], dest)

    landed = sorted(os.path.basename(p) for p in archived_files(dest))
    assert landed == ['y.jpg', 'y_1.jpg'], landed
    assert len({sha(p) for p in archived_files(dest)}) == 2, 'a file was overwritten'


@test
def test_collision_numbering_is_stable_across_runs(work):
    """A second run must not renumber or re-copy what the first run placed."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i, tag in enumerate((b'A', b'B', b'C')):
        write(os.path.join(src, f'f{i}', 'IMG.jpg'),
              jpeg_with_date('2018:10:10 10:10:10', tag * 300))

    run_job([src], dest)
    first = tree_snapshot(dest)
    run_job([src], dest)
    assert tree_snapshot(dest) == first, 'the second run disturbed the archive'


@test
def test_pruning_works_under_a_simulated_windows_long_path_walk(work):
    r"""
    High-fidelity reproduction of the Windows failure.

    On Windows the walk starts at long_path(source) = \\?\C:\... and every
    folder it lists carries that prefix. The destination, meanwhile,
    is stored in its plain form. This test recreates exactly that shape - the
    walk hands out prefixed roots while the destination stays plain - and
    asserts the archive is still recognised and pruned.

    Before the fix this test fails on Linux too, which is the point: the earlier
    tests all passed on Linux precisely because long_path() is a no-op here, so
    nothing ever exercised the mismatch.
    """
    root = os.path.join(work, 'DriveRoot')
    dest = os.path.join(root, 'Master')
    write(os.path.join(root, 'Pics', 'keep.jpg'),
          jpeg_with_date('2011:01:01 00:00:00', b'K' * 500))
    write(os.path.join(dest, '2001', '01', '01', 'already.jpg'),
          jpeg_with_date('2001:01:01 00:00:00', b'Z' * 500))

    PREFIX = '\\\\?\\'
    real_scandir = os.scandir

    def windows_style_scandir(path='.'):
        # accept a prefixed folder and list the real one; the walk joins names
        # onto the prefixed folder it asked for, so every root stays prefixed
        return real_scandir(safety.strip_long_prefix(os.fspath(path)))

    def windows_style_long_path(path):
        p = safety.strip_long_prefix(path)
        return p if p.startswith(PREFIX) else PREFIX + p

    original_long = scanner.long_path
    original_short = scanner.short_path
    os.scandir = windows_style_scandir
    scanner.long_path = windows_style_long_path
    scanner.short_path = safety.strip_long_prefix
    try:
        job = ArchiveJob([root], dest)
        # the walk must hand out prefixed roots, or this test proves nothing
        seen = [r for r, _ in job._walk()]
        assert seen, 'the simulated walk yielded nothing'
        job2 = ArchiveJob([root], dest)
        found = {name for _, name in job2._walk()}
    finally:
        os.scandir = real_scandir
        scanner.long_path = original_long
        scanner.short_path = original_short

    assert 'already.jpg' not in found, (
        'the archive was NOT pruned under a long-path walk - a file inside the '
        'destination was picked up as a source')
    assert 'keep.jpg' in found, found
    assert job2.pruned_destination >= 1, job2.pruned_destination


@test
def test_a_long_path_prefix_never_breaks_a_path_comparison(work):
    r"""
    THE WINDOWS BUG. The walk runs through the \\?\ long-path form, so a folder
    inside it arrives as \\?\C:\Root\Master while the destination is the plain
    C:\Root\Master. Compared as raw strings they never match, so the archive was
    never recognised and never pruned - on Windows only, which is why the Linux
    test run stayed green while the real machine failed.

    Every comparison now goes through normalise(), which strips the prefix.
    """
    real = os.path.join(work, 'Root', 'Master')
    os.makedirs(real, exist_ok=True)
    prefixed = '\\\\?\\' + real

    assert safety.strip_long_prefix(prefixed) == real, safety.strip_long_prefix(prefixed)
    assert safety.normalise(prefixed) == safety.normalise(real), (
        f'{safety.normalise(prefixed)} != {safety.normalise(real)}')

    # UNC form too: \\?\UNC\server\share -> \\server\share
    assert safety.strip_long_prefix(r'\\?\UNC\server\share') == r'\\server\share', \
        safety.strip_long_prefix(r'\\?\UNC\server\share')

    # and an ordinary path must come through untouched
    assert safety.strip_long_prefix(real) == real


@test
def test_the_scanner_compares_the_destination_through_normalise(work):
    """
    Regression guard for the above: the pruning check must not compare raw
    strings. Feeding it the prefixed form of the destination has to still be
    recognised as the destination.
    """
    root = os.path.join(work, 'DriveRoot')
    dest = os.path.join(root, 'Master')
    os.makedirs(dest, exist_ok=True)
    write(os.path.join(root, 'a.jpg'), jpeg_with_date('2011:01:01 00:00:00', b'A' * 400))

    job = ArchiveJob([root], dest)
    # Reproduce the comparison the walk performs, with the prefixed form the
    # long-path walk actually produces on Windows.
    assert safety.normalise('\\\\?\\' + dest) == safety.normalise(job.destination)
    job.run()
    assert job.pruned_destination >= 1, job.pruned_destination


@test
def test_destination_inside_a_source_is_stepped_around_not_refused(work):
    """
    THE COMMON CASE. Archive on the same drive you are scanning: source is the
    drive root, destination is a folder on it. Refusing that outright made
    whole-drive scanning impossible whenever the archive lived on the same disk.
    The walk now steps around the archive instead.
    """
    root = os.path.join(work, 'DriveRoot')
    dest = os.path.join(root, 'Master')
    write(os.path.join(root, 'Photos', 'a.jpg'),
          jpeg_with_date('2011:01:01 00:00:00', b'A' * 500))
    write(os.path.join(root, 'Docs', 'b.jpg'),
          jpeg_with_date('2012:02:02 00:00:00', b'B' * 500))

    assert validate_job([root], dest) == [], validate_job([root], dest)
    notices = safety.job_notices([root], dest)
    assert notices and 'skipped during the scan' in notices[0], notices

    job = ArchiveJob([root], dest)
    job.run()

    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'a.jpg', 'b.jpg'}, landed
    assert all(v == 'verified' for v in statuses().values()), statuses()


@test
def test_a_second_run_does_not_re_ingest_the_archive(work):
    """
    Without pruning, run two would find run one's output sitting inside the
    source and archive it all over again. The count must not grow.
    """
    root = os.path.join(work, 'DriveRoot')
    dest = os.path.join(root, 'Master')
    for i in range(4):
        write(os.path.join(root, 'Pics', f'p{i}.jpg'),
              jpeg_with_date('201%d:03:03 00:00:00' % (i + 2), bytes([i]) * 500))

    ArchiveJob([root], dest).run()
    first = tree_snapshot(dest)
    assert len(first) == 4, first

    ArchiveJob([root], dest).run()
    second = tree_snapshot(dest)
    assert second == first, (
        f'the archive was re-ingested: {sorted(set(second) - set(first))}')

    rows = db.get_recent_files(200)
    assert not any(r['source_path'].startswith(dest) for r in rows), \
        'archived files were recorded as sources'


@test
def test_the_archive_is_pruned_however_deep_it_sits(work):
    root = os.path.join(work, 'DriveRoot')
    dest = os.path.join(root, 'a', 'b', 'c', 'Master')
    write(os.path.join(root, 'keep.jpg'), jpeg_with_date('2013:03:03 00:00:00', b'K' * 500))
    # something that would look like media, sitting inside the archive already
    write(os.path.join(dest, '2001', '01', '01', 'already.jpg'),
          jpeg_with_date('2001:01:01 00:00:00', b'Z' * 500))

    job = ArchiveJob([root], dest)
    job.run()

    rows = {r['filename'] for r in db.get_recent_files(100)}
    assert 'already.jpg' not in rows, 'a file inside the archive was scanned as a source'
    assert 'keep.jpg' in rows, rows
    assert job.pruned_destination >= 1, job.pruned_destination


@test
def test_pruning_is_reported_not_silent(work):
    root = os.path.join(work, 'DriveRoot')
    dest = os.path.join(root, 'Master')
    write(os.path.join(root, 'x.jpg'), jpeg_with_date('2014:04:04 00:00:00', b'X' * 500))
    os.makedirs(dest, exist_ok=True)

    ArchiveJob([root], dest).run()
    message = db.latest_job()['message'] or ''
    assert 'destination archive was skipped' in message, message


@test
def test_a_source_that_is_entirely_inside_the_archive_is_refused(work):
    """
    Nothing would be left to scan, so this is a real error rather than
    something to step around silently.
    """
    dest = os.path.join(work, 'Master')
    src = os.path.join(dest, '2015')
    os.makedirs(src)
    problems = validate_job([src], dest)
    assert problems, 'a source inside the archive must be refused'
    assert any('inside the destination' in p for p in problems), problems


@test
def test_source_equal_to_destination_is_still_refused(work):
    folder = os.path.join(work, 'same')
    os.makedirs(folder)
    assert validate_job([folder], folder), 'source == destination must be refused'


@test
def test_pruning_uses_the_real_path_so_a_link_cannot_smuggle_the_archive_in(work):
    """A symlink pointing at the archive must be pruned too, not followed."""
    root = os.path.join(work, 'DriveRoot')
    dest = os.path.join(work, 'Master')
    write(os.path.join(root, 'real.jpg'), jpeg_with_date('2016:06:06 00:00:00', b'R' * 500))
    write(os.path.join(dest, '2001', '01', '01', 'inside.jpg'),
          jpeg_with_date('2001:01:01 00:00:00', b'I' * 500))
    try:
        os.symlink(dest, os.path.join(root, 'link-to-archive'))
    except (OSError, NotImplementedError):
        return                                  # no symlink support here

    ArchiveJob([root], dest).run()
    rows = {r['filename'] for r in db.get_recent_files(100)}
    assert 'inside.jpg' not in rows, 'the archive was reached through a symlink'
    assert 'real.jpg' in rows, rows


@test
def test_other_sources_still_scan_when_one_is_inside_the_archive(work):
    """Pruning one source must not take the rest of the job down with it."""
    dest = os.path.join(work, 'Master')
    inside = os.path.join(dest, 'incoming')
    outside = os.path.join(work, 'Elsewhere')
    write(os.path.join(inside, 'in.jpg'), jpeg_with_date('2017:07:07 00:00:00', b'N' * 500))
    write(os.path.join(outside, 'out.jpg'), jpeg_with_date('2018:08:08 00:00:00', b'O' * 500))

    job = ArchiveJob([inside, outside], dest)
    job.run()

    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert 'out.jpg' in landed, landed
    rows = {r['filename'] for r in db.get_recent_files(100)}
    assert 'in.jpg' not in rows, 'a source inside the archive was scanned'


@test
def test_capacity_preview_excludes_the_archive(work):
    """The count shown before starting must match what the run will do."""
    root = os.path.join(work, 'DriveRoot')
    dest = os.path.join(root, 'Master')
    for i in range(3):
        write(os.path.join(root, 'Pics', f'p{i}.jpg'),
              jpeg_with_date('201%d:05:05 00:00:00' % (i + 1), bytes([i]) * 500))
    for i in range(7):
        write(os.path.join(dest, '2001', '01', '01', f'old{i}.jpg'),
              jpeg_with_date('2001:01:01 00:00:00', bytes([i + 50]) * 500))

    probe = ArchiveJob([root], dest)
    assert len(list(probe._walk())) == 3, [n for _, n in probe._walk()]


def _mixed_folder(base, tag=b'X'):
    """
    One folder containing a photo, a video and an audio file.

    `tag` makes each folder's contents distinct. Without it the files across
    folders are byte-identical and get correctly deduplicated, which would mask
    whatever the test was actually trying to measure.
    """
    write(os.path.join(base, 'shot.jpg'),
          jpeg_with_date('2016:05:05 05:05:05', tag * 400))
    write(os.path.join(base, 'clip.mp4'),
          b'\x00\x00\x00\x18ftypmp42' + tag * 500)
    write(os.path.join(base, 'song.mp3'),
          b'ID3\x04\x00\x00\x00\x00\x00\x00' + tag * 500)
    return base


@test
def test_each_source_folder_has_its_own_media_selection(work):
    """
    THE FEATURE. Three folders, three different selections, one job. An old
    camcorder card has no stills worth sifting; a scanned-prints folder has no
    video. A single global filter cannot express that.
    """
    photos_only = _mixed_folder(os.path.join(work, 'scans'), b'A')
    video_only = _mixed_folder(os.path.join(work, 'camcorder'), b'B')
    everything = _mixed_folder(os.path.join(work, 'phone'), b'C')
    dest = os.path.join(work, 'd')

    job = ArchiveJob([
        {'path': photos_only, 'types': ['image']},
        {'path': video_only, 'types': ['video']},
        {'path': everything, 'types': ['image', 'video', 'audio']},
    ], dest)
    job.run()

    # Which source did each archived file come from?
    landed = {}
    for r in db.get_recent_files(100):
        if r['status'] == 'verified':
            landed.setdefault(os.path.basename(os.path.dirname(r['source_path'])),
                              set()).add(r['filename'])

    assert landed.get('scans') == {'shot.jpg'}, landed
    assert landed.get('camcorder') == {'clip.mp4'}, landed
    assert landed.get('phone') == {'shot.jpg', 'clip.mp4', 'song.mp3'}, landed

    # 9 files present, 5 archived, 4 excluded by the per-folder selections.
    assert job.filtered_out == 4, job.filtered_out
    assert job.filtered_by_kind == {'video': 1, 'audio': 2, 'image': 1}, \
        job.filtered_by_kind


@test
def test_excluded_types_are_never_copied_to_the_destination(work):
    """The selection governs the archive, not just the scan."""
    src = _mixed_folder(os.path.join(work, 's'))
    dest = os.path.join(work, 'd')
    ArchiveJob([{'path': src, 'types': ['image']}], dest).run()

    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'shot.jpg'}, landed
    for name in ('clip.mp4', 'song.mp3'):
        assert not [p for p in archived_files(dest) if p.endswith(name)], \
            f'{name} reached the archive despite being deselected'
        assert os.path.exists(os.path.join(src, name)), \
            f'{name} was disturbed at the source'


@test
def test_excluded_files_are_counted_and_reported_not_silently_dropped(work):
    src = _mixed_folder(os.path.join(work, 's'))
    job = ArchiveJob([{'path': src, 'types': ['image']}], os.path.join(work, 'd'))
    job.run()
    message = db.latest_job()['message'] or ''
    assert 'not archived because their type was not selected' in message, message
    assert '2 files' in message, message
    assert 'video' in message and 'audio' in message, message


@test
def test_a_deselected_type_is_picked_up_when_re_enabled_later(work):
    """
    Excluding a type must not poison the record: turning it back on later has
    to archive those files, not treat them as already handled.
    """
    src = _mixed_folder(os.path.join(work, 's'))
    dest = os.path.join(work, 'd')

    ArchiveJob([{'path': src, 'types': ['image']}], dest).run()
    assert len(archived_files(dest)) == 1, archived_files(dest)

    ArchiveJob([{'path': src, 'types': ['image', 'video', 'audio']}], dest).run()
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'shot.jpg', 'clip.mp4', 'song.mp3'}, landed
    assert all(v == 'verified' for v in statuses().values()), statuses()


@test
def test_content_sniffing_respects_the_per_folder_selection(work):
    """A video identified by its bytes is still excluded if video is off."""
    src = os.path.join(work, 's')
    write(os.path.join(src, 'IMG_0042'), jpeg_with_date('2010:04:04 04:04:04', b'N' * 400))
    write(os.path.join(src, 'CLIP.DAT'), b'\x00\x00\x00\x18ftypisom' + b'V' * 500)
    dest = os.path.join(work, 'd')

    ArchiveJob([{'path': src, 'types': ['image']}], dest).run()
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'IMG_0042'}, landed


@test
def test_a_bare_path_source_defaults_to_every_media_type(work):
    """Default is everything on - a plain path means 'archive it all'."""
    src = _mixed_folder(os.path.join(work, 's'))
    dest = os.path.join(work, 'd')
    ArchiveJob([src], dest).run()
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'shot.jpg', 'clip.mp4', 'song.mp3'}, landed

    entries = safety.normalise_sources([src])
    assert entries[0]['types'] == frozenset(safety.MEDIA_KINDS), entries


@test
def test_a_source_with_nothing_selected_is_refused(work):
    src = _mixed_folder(os.path.join(work, 's'))
    dest = os.path.join(work, 'd')
    problems = validate_job([{'path': src, 'types': []}], dest)
    assert problems, 'a folder with no types selected must be refused'
    assert any('No media types are selected' in p for p in problems), problems


@test
def test_dry_run_honours_the_per_folder_selection(work):
    src = _mixed_folder(os.path.join(work, 's'))
    dest = os.path.join(work, 'd')
    ArchiveJob([{'path': src, 'types': ['audio']}], dest,
               mode=scanner.MODE_DRY_RUN).run()
    planned = {r['filename'] for r in db.get_recent_files(50)
               if r['status'] == 'planned'}
    assert planned == {'song.mp3'}, planned


@test
def test_the_job_record_stores_each_folders_selection(work):
    a = _mixed_folder(os.path.join(work, 'a'), b'A')
    b = _mixed_folder(os.path.join(work, 'b'), b'B')
    ArchiveJob([{'path': a, 'types': ['image']},
                {'path': b, 'types': ['video', 'audio']}],
               os.path.join(work, 'd')).run()
    stored = json.loads(db.latest_job()['sources'])
    assert stored[0]['types'] == ['image'], stored
    assert stored[1]['types'] == ['audio', 'video'], stored


@test
def test_job_is_refused_when_the_volume_is_too_small(work):
    """Better to refuse at the start than to die at 80% with a full disk."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'f.jpg'), jpeg_with_date('2018:01:01 00:00:00', b'F' * 5000))

    from ninaivu.archive import safety
    real = safety.check_free_space
    scanner.check_free_space = lambda d, n, headroom=1.10: (False, 1024, n * 2)
    try:
        job = ArchiveJob([src], dest)
        job.run()
    finally:
        scanner.check_free_space = real

    assert not archived_files(dest), 'files were copied despite insufficient space'
    j = db.latest_job()
    assert j['state'] == 'failed', j
    assert 'space' in (j['message'] or '').lower(), j['message']


@test
def test_run_produces_a_plain_text_log(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'logged.jpg'), jpeg_with_date('2017:03:03 03:03:03', b'L' * 400))
    original_dir = scanner.LOG_DIR
    scanner.LOG_DIR = os.path.join(work, 'logs')
    try:
        run_job([src], dest)
    finally:
        scanner.LOG_DIR = original_dir

    logs = os.listdir(os.path.join(work, 'logs'))
    assert logs, 'no log file was written'
    text = open(os.path.join(work, 'logs', logs[0])).read()
    assert 'VERIFIED' in text and 'logged.jpg' in text, text
    assert 'sha256=' in text, 'the log does not record the verification hash'


@test
def test_eta_and_progress_are_reported(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i in range(3):
        write(os.path.join(src, f'e{i}.jpg'),
              jpeg_with_date('2021:0%d:01 00:00:00' % (i + 1), bytes([i]) * 300))
    job = ArchiveJob([src], dest)
    job.run()
    assert job.total_files == 3, job.total_files
    assert job.total_bytes > 0, 'preflight did not measure the outstanding bytes'
    stats = db.get_stats()
    assert stats['total_files'] == 3, stats
    assert stats['bytes_copied'] > 0, stats


@needs_exifread
@test
def test_year_breakdown_reflects_real_capture_dates(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'p1.jpg'), jpeg_with_date('2005:06:01 00:00:00', b'1' * 300))
    write(os.path.join(src, 'p2.jpg'), jpeg_with_date('2005:07:01 00:00:00', b'2' * 300))
    write(os.path.join(src, 'p3.jpg'), jpeg_with_date('2018:01:01 00:00:00', b'3' * 300))
    run_job([src], dest)
    years = {r['year']: r['count'] for r in db.year_breakdown()}
    assert years == {'2005': 2, '2018': 1}, years


@test
def test_changing_the_destination_re_archives_everything(work):
    """
    'Already done' is only true relative to the archive it was done into.
    Pointing the tool at a new destination must fill it, not report success
    while doing nothing.
    """
    src = os.path.join(work, 's')
    first = os.path.join(work, 'archive-a')
    second = os.path.join(work, 'archive-b')
    for i in range(4):
        write(os.path.join(src, f'm{i}.jpg'),
              jpeg_with_date('2013:0%d:14 10:00:00' % (i + 1), bytes([i]) * 500))

    run_job([src], first)
    assert len(archived_files(first)) == 4, archived_files(first)

    run_job([src], second)
    assert len(archived_files(second)) == 4, \
        f'the new destination was left empty: {archived_files(second)}'
    assert len(archived_files(first)) == 4, 'the old archive was disturbed'
    st = statuses()
    assert all(v == 'verified' for v in st.values()), st

    # And re-running against the second is still a no-op.
    snapshot = tree_snapshot(second)
    run_job([src], second)
    assert tree_snapshot(second) == snapshot, 're-running churned the archive'


@test
def test_zero_byte_skip_survives_a_destination_change(work):
    """A zero-byte file is not archivable anywhere, so it stays skipped."""
    src = os.path.join(work, 's')
    write(os.path.join(src, 'empty.jpg'), b'')
    write(os.path.join(src, 'ok.jpg'), jpeg_with_date('2013:01:14 10:00:00', b'O' * 400))
    run_job([src], os.path.join(work, 'a'))
    run_job([src], os.path.join(work, 'b'))
    assert statuses()['empty.jpg'] == 'skipped', statuses()


@test
def test_settings_round_trip(work):
    db.save_settings([{'path': '/a', 'types': ['image']},
                      {'path': '/b', 'types': ['video', 'audio']}],
                     '/dest', {'image', 'video'})
    loaded = db.load_settings()
    assert [s['path'] for s in loaded['source_dirs']] == ['/a', '/b'], loaded
    assert loaded['source_dirs'][0]['types'] == ['image'], loaded
    assert loaded['source_dirs'][1]['types'] == ['audio', 'video'], loaded
    assert loaded['destination_dir'] == '/dest', loaded


@test
def test_settings_upgrade_from_a_flat_source_list(work):
    """A list saved before per-folder selection existed means 'everything'."""
    db.set_config('last_sources', '["/old/one", "/old/two"]')
    db.set_config('last_destination', '/dest')
    loaded = db.load_settings()
    assert [s['path'] for s in loaded['source_dirs']] == ['/old/one', '/old/two'], loaded
    for s in loaded['source_dirs']:
        assert s['types'] == ['audio', 'image', 'video'], s


# ==========================================================================
# Choosing a folder inside an existing archive
# ==========================================================================

def _build_archive(work, name='MasterArchive', years=(2015, 2018, 2021)):
    src = os.path.join(work, 's')
    arch = os.path.join(work, name)
    for y in years:
        for i in range(2):
            write(os.path.join(src, f'p_{y}_{i}.jpg'),
                  jpeg_with_date(f'{y}:0{i+1}:05 10:00:00', bytes([i]) * 800))
    run_job([src], arch)
    return src, arch


@test
def test_picking_a_subfolder_of_the_archive_does_not_duplicate_it(work):
    """
    THE NESTING BUG. Browsing into the archive and picking MasterArchive/2018 as
    the destination used to re-copy every photo NOT already under 2018 into
    MasterArchive/2018/<year>/... - a second archive nested inside the first,
    and a *partial* duplication, which is far harder to spot than a total one.
    """
    src, arch = _build_archive(work)
    before = tree_snapshot(arch)
    assert len(before) >= 6, before

    job = ArchiveJob([src], os.path.join(arch, '2018'))
    job.run()

    after = tree_snapshot(arch)
    assert after == before, (
        'the archive changed:\n  added: '
        f'{sorted(set(after) - set(before))}')
    assert not os.path.isdir(os.path.join(arch, '2018', '2015')), \
        'a nested archive was created inside the 2018 folder'
    assert job.destination == arch, \
        f'the job used {job.destination} instead of the archive root {arch}'


@test
def test_picking_a_deep_dated_subfolder_is_also_corrected(work):
    src, arch = _build_archive(work)
    before = tree_snapshot(arch)
    job = ArchiveJob([src], os.path.join(arch, '2015', '01', '05'))
    job.run()
    assert job.destination == arch, job.destination
    assert tree_snapshot(arch) == before, 'the archive was modified'


@test
def test_correction_works_on_an_archive_that_predates_the_marker(work):
    """
    An archive built by an older version has no marker file, so the layout
    itself has to give it away: a YYYY\\MM\\DD chain under a folder holding
    year-shaped subfolders.
    """
    src, arch = _build_archive(work)
    os.remove(os.path.join(arch, safety.ARCHIVE_MARKER))       # simulate old archive
    db.clear_db()                                              # and no job history
    for name in ('a.jpg',):
        write(os.path.join(src, name), jpeg_with_date('2019:03:03 10:00:00', b'A' * 700))

    resolution = safety.resolve_destination(os.path.join(arch, '2018'), [])
    assert resolution['corrected'], resolution
    assert resolution['destination'] == arch, resolution
    assert resolution['reason'] == 'layout', resolution


@test
def test_marker_identifies_the_root_from_anywhere_inside(work):
    src, arch = _build_archive(work)
    assert os.path.isfile(os.path.join(arch, safety.ARCHIVE_MARKER)), \
        'no marker was written at the archive root'
    for inside in ('2015', os.path.join('2015', '01'), os.path.join('2018', '01', '05')):
        root, reason = safety.find_archive_root(os.path.join(arch, inside))
        assert root == arch, f'{inside} -> {root}'
        assert reason == 'marker', reason


@test
def test_the_archive_root_itself_is_never_corrected(work):
    src, arch = _build_archive(work)
    resolution = safety.resolve_destination(arch, db.previous_destinations())
    assert not resolution['corrected'], resolution
    assert resolution['destination'] == arch


@test
def test_a_brand_new_destination_is_never_corrected(work):
    """No false positives: an ordinary new folder must be left exactly alone."""
    _build_archive(work)
    for candidate in (os.path.join(work, 'BrandNewArchive'),
                      os.path.join(work, 'Backups', '2015Photos'),
                      os.path.join(work, 'Photos2020'),
                      os.path.join(work, 'holiday', '2019 Italy')):
        resolution = safety.resolve_destination(candidate, db.previous_destinations())
        assert not resolution['corrected'], f'{candidate} was wrongly corrected: {resolution}'
        assert resolution['destination'] == candidate


@test
def test_a_year_named_folder_outside_any_archive_is_left_alone(work):
    """`D:\\Backups\\2015` is a fine destination when Backups is not an archive."""
    plain = os.path.join(work, 'Backups', '2015')
    os.makedirs(plain)
    resolution = safety.resolve_destination(plain, [])
    assert not resolution['corrected'], resolution


@test
def test_job_history_recognises_the_archive_without_a_marker(work):
    src, arch = _build_archive(work)
    os.remove(os.path.join(arch, safety.ARCHIVE_MARKER))
    # Strip the layout signal too, so only the recorded job history is left.
    root, reason = safety.find_archive_root(os.path.join(arch, 'somewhere', 'else'),
                                            db.previous_destinations())
    assert root == arch, root
    assert reason == 'history', reason


@test
def test_the_marker_is_not_treated_as_a_media_file(work):
    src, arch = _build_archive(work)
    # Point a fresh run at the archive as a SOURCE - the marker must not be
    # picked up as something to archive.
    dest2 = os.path.join(work, 'second')
    job = ArchiveJob([arch], dest2)
    job.run()
    landed = {os.path.basename(p) for p in archived_files(dest2)}
    assert safety.ARCHIVE_MARKER not in landed, landed


@test
def test_dry_run_does_not_stamp_a_marker(work):
    src = os.path.join(work, 's')
    dest = os.path.join(work, 'd')
    write(os.path.join(src, 'a.jpg'), jpeg_with_date('2015:01:01 00:00:00', b'A' * 500))
    ArchiveJob([src], dest, mode=scanner.MODE_DRY_RUN).run()
    assert not os.path.exists(os.path.join(dest, safety.ARCHIVE_MARKER)), \
        'a dry run wrote a marker file'


@needs_exifread
@test
def test_correction_survives_a_second_consolidation_into_the_same_archive(work):
    """The corrected path must behave exactly like naming the root directly."""
    src, arch = _build_archive(work)
    write(os.path.join(src, 'new.jpg'), jpeg_with_date('2022:06:06 12:00:00', b'N' * 700))

    job = ArchiveJob([src], os.path.join(arch, '2021'))
    job.run()

    assert job.destination == arch
    landed = [p for p in archived_files(arch) if p.endswith('new.jpg')]
    assert len(landed) == 1, landed
    rel = os.path.relpath(os.path.dirname(landed[0]), arch)
    assert rel == os.path.join('2022', '06', '06'), rel
    assert not os.path.isdir(os.path.join(arch, '2021', '2022')), 'nested again'


# ==========================================================================
# Validation
# ==========================================================================

@test
def test_destination_inside_source_is_allowed_and_pruned(work):
    """
    This used to be refused outright. It is the normal case for an archive
    living on a drive you are also scanning, so it is now allowed: the walk
    steps around the archive and says so in a notice.
    """
    src = os.path.join(work, 's')
    os.makedirs(src)
    dest = os.path.join(src, 'archive')
    assert validate_job([src], dest) == [], validate_job([src], dest)
    notices = safety.job_notices([src], dest)
    assert notices, 'the user should be told the archive is being stepped around'
    assert 'skipped during the scan' in notices[0], notices


@test
def test_source_equals_destination_is_refused(work):
    src = os.path.join(work, 's')
    os.makedirs(src)
    assert validate_job([src], src), 'source == destination must be refused'


@test
def test_source_inside_destination_is_refused(work):
    dest = os.path.join(work, 'archive')
    src = os.path.join(dest, 'incoming')
    os.makedirs(src)
    problems = validate_job([src], dest)
    assert problems, 'a source inside the destination must be refused'


@test
def test_nested_sources_are_flagged(work):
    outer = os.path.join(work, 'outer')
    inner = os.path.join(outer, 'inner')
    os.makedirs(inner)
    dest = os.path.join(work, 'd')
    problems = validate_job([outer, inner], dest)
    assert any('covered by' in p for p in problems), problems


@test
def test_valid_job_passes_validation(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    os.makedirs(src)
    assert validate_job([src], dest) == [], validate_job([src], dest)


# ==========================================================================
# Control
# ==========================================================================

@test
def test_pause_actually_stops_progress_and_resume_continues(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    for i in range(30):
        write(os.path.join(src, f'p{i}.jpg'),
              jpeg_with_date('2023:01:%02d 10:00:00' % (i % 28 + 1),
                             bytes([i % 251]) * 20000))

    job = ArchiveJob([src], dest)
    t = threading.Thread(target=job.run, daemon=True)
    t.start()
    time.sleep(0.25)
    job.gate.pause()
    time.sleep(0.15)
    a = job.processed
    time.sleep(0.5)
    b = job.processed
    assert b == a, f'progress continued while paused: {a} -> {b}'
    assert job.gate.is_paused

    job.gate.resume()
    t.join(timeout=30)
    assert not t.is_alive(), 'job did not finish after resume'
    assert job.processed == 30, f'only {job.processed} of 30 processed'
    assert len(archived_files(dest)) == 30


@test
def test_disconnected_source_waits_and_resumes_from_verified_progress(work, monkeypatch):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    offline = os.path.join(work, 's-unplugged')
    for i in range(3):
        write(os.path.join(src, f'p{i}.jpg'),
              jpeg_with_date(f'2023:02:0{i + 1} 10:00:00', bytes([i]) * 4000))

    job = ArchiveJob([src], dest)
    real_copy = job._copy_and_hash
    unplugged = False

    def disconnect_once(source, temporary):
        nonlocal unplugged
        if not unplugged:
            unplugged = True
            os.rename(src, offline)
            raise OSError('simulated removable drive disconnect')
        return real_copy(source, temporary)

    monkeypatch.setattr(job, '_copy_and_hash', disconnect_once)
    thread = threading.Thread(target=job.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not job.waiting_for and time.time() < deadline:
        time.sleep(0.02)
    assert job.waiting_for, 'the job did not enter safe reconnect waiting'
    assert db.latest_job()['phase'] == 'waiting-for-drive'
    assert not [p for p in os.listdir(dest) if p.startswith(scanner.PARTIAL_PREFIX)]

    os.rename(offline, src)
    thread.join(timeout=30)
    assert not thread.is_alive(), 'the job did not resume when the drive returned'
    assert job.reconnects == 1
    assert len(archived_files(dest)) == 3
    assert db.get_stats()['verified'] == 3


@test
def test_reconnect_refuses_a_different_volume_at_the_same_path(work, monkeypatch):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    os.makedirs(src)
    os.makedirs(dest)
    job = ArchiveJob([src], dest)
    remembered = {item['identity'] for item in job._drive_probes}
    assert remembered

    monkeypatch.setattr(scanner, '_volume_identity',
                        lambda _path: ('replacement-volume', 999))
    missing = job._unavailable_roots()
    assert missing
    assert all('different volume' in message for message in missing)


@needs_exifread
@test
def test_stop_takes_effect_inside_a_single_large_file(work):
    """
    The old loop only checked its flag between files, so Stop during a large
    video did nothing until that file finished. Chunk-level gating fixes it.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'huge.jpg'),
          jpeg_with_date('2024:06:01 00:00:00', b'', size=64 * 1024 * 1024))

    job = ArchiveJob([src], dest)
    t = threading.Thread(target=job.run, daemon=True)
    t.start()
    time.sleep(0.4)                      # well into the copy of a 64 MB file
    start = time.time()
    job.gate.cancel()
    t.join(timeout=10)
    elapsed = time.time() - start

    assert not t.is_alive(), 'stop did not take effect at all'
    assert elapsed < 3.0, f'stop took {elapsed:.1f}s to take effect mid-file'
    leftovers = [p for p in archived_files(dest)
                 if os.path.basename(p).startswith(scanner.PARTIAL_PREFIX)]
    assert not leftovers, leftovers


@test
def test_symlink_loop_does_not_hang_the_walk(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    inner = os.path.join(src, 'inner')
    os.makedirs(inner)
    write(os.path.join(inner, 'ok.jpg'), jpeg_with_date('2015:09:09 09:09:09', b'L' * 200))
    try:
        os.symlink(src, os.path.join(inner, 'loop'))
    except (OSError, NotImplementedError):
        return                            # no symlink support; nothing to test
    job = ArchiveJob([src], dest)
    done = threading.Event()
    threading.Thread(target=lambda: (job.run(), done.set()), daemon=True).start()
    assert done.wait(timeout=25), 'the walk did not terminate - symlink loop'
    assert len(archived_files(dest)) == 1


@test
def test_skip_dirs_are_not_scanned(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, '$RECYCLE.BIN', 'deleted.jpg'),
          jpeg_with_date('2015:01:01 00:00:00', b'R' * 100))
    write(os.path.join(src, 'keep.jpg'), jpeg_with_date('2015:01:02 00:00:00', b'K' * 100))
    run_job([src], dest)
    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert landed == {'keep.jpg'}, landed


# ---------------------------------------------------------------------------
# The engine's state moves with Ninaivu
# ---------------------------------------------------------------------------

def test_the_engine_keeps_its_state_in_ninaivus_state_directory(tmp_path):
    """Standalone it sat beside the source files; here it belongs to Ninaivu.

    This is what makes "reinstall the app" safe: the record of every file
    already copied and verified is not in the folder you just overwrote.
    """
    paths = archive.configure(tmp_path / "state")
    assert paths["db"] == str(tmp_path / "state" / "archive.db")
    assert paths["logs"] == str(tmp_path / "state" / "archive-logs")
    assert db.DB_PATH == paths["db"]
    assert scanner.LOG_DIR == paths["logs"]

    db.init_db()
    assert (tmp_path / "state" / "archive.db").is_file()


def test_configure_is_idempotent(tmp_path):
    first = archive.configure(tmp_path / "s")
    assert archive.configure(tmp_path / "s") == first


def test_capture_dates_are_read_even_without_exifread(work, monkeypatch):
    """A missing optional dependency must not silently ruin every date.

    Standalone, no ExifRead meant every photo fell back to its filesystem
    timestamp and the whole archive piled into the month it was built. Ninaivu
    always has Pillow, so there is a second reader to fall back to.
    """
    from PIL import Image

    monkeypatch.setattr(scanner, "exifread", None)
    src = os.path.join(work, "s")
    os.makedirs(src, exist_ok=True)
    photo = os.path.join(src, "holiday.jpg")

    image = Image.new("RGB", (64, 48), (10, 90, 160))
    exif = image.getexif()
    exif[0x9003] = "2011:06:15 08:30:00"        # DateTimeOriginal
    image.save(photo, "JPEG", exif=exif)

    found = scanner._exif_date(photo)
    assert found is not None, "Pillow must cover for a missing ExifRead"
    assert (found.year, found.month, found.day) == (2011, 6, 15)


def test_both_readers_missing_is_still_not_fatal(work, monkeypatch):
    monkeypatch.setattr(scanner, "exifread", None)
    monkeypatch.setattr(scanner, "_PILImage", None)
    src, dest = os.path.join(work, "s"), os.path.join(work, "d")
    write(os.path.join(src, "a.jpg"), jpeg_with_date("2011:06:15 08:30:00", b"H" * 200))
    run_job([src], dest)          # falls back down the date chain, does not crash
    assert archived_files(dest)


# --- the parallelism that is deliberately not switched on ------------------

def test_more_than_one_worker_is_refused_rather_than_quietly_ignored(tmp_path):
    """A setting that is silently clamped is how somebody believes it is on.

    The worker machinery exists and runs, but two guarantees do not survive it
    yet — identical photos can be archived twice, and a dry run stops
    predicting the real run. Until they do, asking for it has to fail loudly.
    """
    import pytest as _pytest

    from ninaivu.archive.scanner import DEFAULT_WORKERS, ArchiveJob

    assert DEFAULT_WORKERS == 1
    with _pytest.raises(ValueError, match="not safe yet"):
        ArchiveJob([str(tmp_path)], str(tmp_path / "dest"), workers=4)

    # …and the ordinary construction still works.
    assert ArchiveJob([str(tmp_path)], str(tmp_path / "dest")).workers == 1


@test
def test_a_sidecar_that_fails_to_copy_is_reported_not_dropped(work, monkeypatch):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'P1.jpg'), jpeg_with_date('2022:02:22 22:22:22', b'P' * 300))
    write(os.path.join(src, 'P1.xmp'), b'<x:xmpmeta>captions</x:xmpmeta>')

    def refuse(*_args, **_kwargs):
        raise PermissionError(13, 'simulated locked sidecar')

    monkeypatch.setattr(scanner.shutil, 'copy2', refuse)
    job = run_job([src], dest)

    landed = {os.path.basename(p) for p in archived_files(dest)}
    assert 'P1.jpg' in landed, 'the photo must still be archived'
    assert job.sidecars_failed == 1
    assert 'could NOT be copied' in db.latest_job()['message']


@test
def test_a_disconnect_raised_by_the_last_workers_is_not_swallowed(work, monkeypatch):
    """The final queued copies finish in the pool's cleanup; a drive vanishing
    there must still stop the run rather than let it report itself complete.

    Parallel copying is refused at construction until it is safe, so the
    worker count is set directly: this guards the pool for the day it is on.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    os.makedirs(src)
    job = ArchiveJob([src], dest)
    job.workers = 2
    monkeypatch.setattr(job, '_walk', lambda: iter([(src, 'a.jpg'), (src, 'b.jpg'),
                                                    (src, 'c.jpg')]))

    def one_file(_path, name):
        if name == 'c.jpg':
            time.sleep(0.05)
            raise scanner.DriveDisconnected([dest])

    monkeypatch.setattr(job, '_one_file', one_file)
    with pytest.raises(scanner.DriveDisconnected):
        job._copy_everything()


# ---------------------------------------------------------------------------
# A drive that drops off the bus and comes straight back
# ---------------------------------------------------------------------------
#
# A USB enclosure that resets mid-walk fails whichever folder was being listed
# at that instant, with a device-level error, and answers again a second later.
# Found on a real run: 61 folders of family photographs — "Iniyan Birthday",
# "Papa birthday and Diwali 2023", "wedding songs" — were written off as
# unreadable and left out of the archive, and the run still said "complete".


@test
def test_a_long_quiet_stretch_still_leaves_progress_in_the_run_log(work):
    """Silence in a run log cannot be told apart from a hang.

    A resumed run steps over finished files and hashes large videos against the
    archive without copying anything, so it can work hard for many minutes with
    nothing to say. One run went twelve minutes without a line and was stopped
    by hand for looking stuck.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'a.jpg'),
          jpeg_with_date('2019:04:02 00:00:00', b'A' * 400))
    job = ArchiveJob([src], dest)
    lines = []
    job.log = lines.append
    job.processed, job.stepped_over, job.total_files = 1200, 1000, 5000
    job.started_at = time.time() - 120
    job._last_heartbeat = 0.0

    job._heartbeat()
    job._heartbeat()            # straight away, inside the quiet period

    assert len(lines) == 1, lines
    assert '1,200 of 5,000 files checked' in lines[0], lines[0]
    assert '200 new this run' in lines[0], lines[0]
    assert '1,000 already done' in lines[0], lines[0]


def device_gone(path):
    """The error Windows raises for a drive that is no longer on the bus."""
    exc = OSError(22, 'A device which does not exist was specified', str(path))
    exc.winerror = 433
    return exc


@test
def test_a_drive_that_blinks_is_read_again_rather_than_written_off(work):
    """The folder is perfectly readable a moment later — and must be read."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')

    write(os.path.join(src, 'Photos', 'birthday.jpg'),
          jpeg_with_date('2019:04:02 00:00:00', b'B' * 400))

    real_scandir = os.scandir
    blinked = []

    def flaky(path='.'):
        if 'Photos' in str(path) and not blinked:
            blinked.append(str(path))
            raise device_gone(path)
        return real_scandir(path)

    os.scandir = flaky
    try:
        job = ArchiveJob([src], dest)
        job.run()
    finally:
        os.scandir = real_scandir

    assert blinked, 'the fixture never simulated the reset'
    assert job.device_resets == 1
    assert not job.unreadable_dirs, job.unreadable_dirs

    assert statuses().get('birthday.jpg') == 'verified', statuses()


@test
def test_a_drive_that_keeps_resetting_stops_being_believed(work):
    """A cable on its way out resets every few seconds.

    Waiting and walking again for every one of them is a run that never ends,
    so after a few the folder is recorded as unreadable — which is what puts it
    in front of somebody instead of retrying for ever.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'Photos', 'birthday.jpg'),
          jpeg_with_date('2019:04:02 00:00:00', b'B' * 400))

    real_scandir = os.scandir

    def always_gone(path='.'):
        if 'Photos' in str(path):
            raise device_gone(path)
        return real_scandir(path)

    os.scandir = always_gone
    try:
        job = ArchiveJob([src], dest)
        job.run()
    finally:
        os.scandir = real_scandir

    assert job.device_resets == scanner.WALK_RESETS_TOLERATED
    assert job.unreadable_dirs, 'the folder must end up in front of somebody'


@test
def test_a_folder_that_is_genuinely_unreadable_is_not_retried(work):
    """Permission denied says something about the folder, not about the drive."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'Locked', 'a.jpg'),
          jpeg_with_date('2019:04:02 00:00:00', b'A' * 400))

    real_scandir = os.scandir

    def denied(path='.'):
        if 'Locked' in str(path):
            raise PermissionError(13, 'Permission denied', str(path))
        return real_scandir(path)

    os.scandir = denied
    try:
        job = ArchiveJob([src], dest)
        job.run()
    finally:
        os.scandir = real_scandir

    assert job.device_resets == 0
    assert job.unreadable_dirs


@test
def test_a_run_that_could_not_read_part_of_the_source_does_not_say_complete(work):
    """"complete - 61 folders could not be read" is a contradiction.

    The word people read is the first one, and it decides whether they go and
    look at the Errors tab or believe the archive holds everything.
    """
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(src, 'Locked', 'a.jpg'),
          jpeg_with_date('2019:04:02 00:00:00', b'A' * 400))

    real_scandir = os.scandir

    def denied(path='.'):
        if 'Locked' in str(path):
            raise PermissionError(13, 'Permission denied', str(path))
        return real_scandir(path)

    os.scandir = denied
    try:
        ArchiveJob([src], dest).run()
    finally:
        os.scandir = real_scandir

    message = db.latest_job()['message'] or ''
    assert message.startswith('INCOMPLETE'), message
    assert 'could not be read' in message, message


def test_an_unreadable_folder_that_cannot_be_reported_still_reaches_the_log(work, monkeypatch):
    """The report of folders that could not be listed exists so an unscanned
    subtree is visible. If the report itself fails, the run log must still say so."""
    job = ArchiveJob([work], os.path.join(os.path.dirname(work), "Master"))
    job.job_id = None
    job.unreadable_dirs = [(os.path.join(work, "Broken"), "Access is denied")]
    lines = []
    monkeypatch.setattr(job, "log", lines.append)
    monkeypatch.setattr(db, "claim_file", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    job._record_unreadable_dirs()
    assert any("NOT RECORDED" in line and "Broken" in line and "database is locked" in line
               for line in lines), lines
