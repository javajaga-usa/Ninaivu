"""How a file that fails to archive is reported, retried and recovered.

Each failure leaves a row the admin can read (a plain reason, not a
traceback with two paths), a failure that may pass on its own gets one more
try at the end of the same run, a full archive drive stops the run instead of
failing every file after it, and anything left failed is copied after Retry
failed or the next Start.
"""

import errno
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ninaivu import archive                                     # noqa: E402
from ninaivu.archive import database as db                      # noqa: E402
from ninaivu.archive import scanner                             # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                  # noqa: E402


@pytest.fixture()
def work(tmp_path, monkeypatch):
    archive.configure(tmp_path)
    db.close_db()
    db.init_db()
    monkeypatch.setattr(scanner, 'RETRY_PAUSE_SECONDS', 0)
    try:
        yield tmp_path
    finally:
        db.close_db()


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(data)
    return str(path)


def _rows():
    return {os.path.basename(r['source_path']): r for r in db.get_recent_files(100)}


def _partials(dest):
    return [n for _, _, names in os.walk(dest) for n in names
            if n.startswith(scanner.PARTIAL_PREFIX)]


def _clip(tag):
    return b'\x00\x00\x00\x18ftypmp42' + tag * 4096


def _failing_copy(monkeypatch, fail_for, make_error, times=1):
    """Make `_copy_and_hash` raise for one file name, `times` times."""
    original = ArchiveJob._copy_and_hash
    left = {'n': times}

    def copy(self, src, tmp):
        if os.path.basename(src) == fail_for and left['n'] > 0:
            left['n'] -= 1
            with open(tmp, 'wb'):
                pass
            raise make_error()
        return original(self, src, tmp)
    monkeypatch.setattr(ArchiveJob, '_copy_and_hash', copy)


# ---- the words ------------------------------------------------------------

@pytest.mark.parametrize('code, words', [
    (errno.EPERM, 'not allowed to read or write'),
    (errno.EACCES, 'no permission'),
    (errno.ENOENT, 'was gone by the time it was copied'),
    (errno.ENAMETOOLONG, 'too long'),
    (errno.EIO, 'read or write error'),
])
def test_a_system_error_reads_as_a_reason_without_the_paths(code, words):
    exc = OSError(code, os.strerror(code), '/Volumes/a/.pam-partial-1.tmp', None,
                  '/Volumes/a/clip.MP4')
    text = scanner.describe_failure(exc)
    assert words in text
    assert errno.errorcode[code] in text
    assert '/Volumes' not in text


def test_the_archives_own_messages_are_kept_as_written():
    assert scanner.describe_failure(IOError('source changed during the copy')) \
        == 'source changed during the copy'
    exc = PermissionError(errno.EPERM, 'x')
    exc.plain = 'said plainly'
    assert scanner.describe_failure(exc) == 'said plainly'


# ---- retried in the same run ------------------------------------------------

def test_a_file_busy_on_the_first_try_is_archived_on_the_second(work, monkeypatch):
    src, dest = work / 'card', work / 'archive'
    _write(src / 'busy.MP4', _clip(b'b'))
    _write(src / 'fine.MP4', _clip(b'f'))
    _failing_copy(monkeypatch, 'busy.MP4',
                  lambda: OSError(errno.EBUSY, 'Resource busy'))

    job = ArchiveJob([str(src)], str(dest))
    job.run()

    rows = _rows()
    assert rows['busy.MP4']['status'] == 'verified', rows['busy.MP4']
    assert rows['fine.MP4']['status'] == 'verified'
    assert job.failed == 0
    assert job.processed == 2
    assert not _partials(dest)
    assert 'could not be copied' not in db.latest_job()['message']


def test_a_lasting_failure_gets_only_one_more_try(work, monkeypatch):
    src, dest = work / 'card', work / 'archive'
    _write(src / 'busy.MP4', _clip(b'b'))
    _failing_copy(monkeypatch, 'busy.MP4',
                  lambda: OSError(errno.EBUSY, 'Resource busy'), times=5)

    job = ArchiveJob([str(src)], str(dest))
    job.run()

    row = _rows()['busy.MP4']
    assert row['status'] == 'error'
    assert 'in use by another program' in row['error']
    assert job.failed == 1
    assert '1 files could not be copied' in db.latest_job()['message']


# ---- reported, then recovered by Retry failed ------------------------------

def test_a_permission_failure_is_not_retried_but_recovers_after_retry_failed(work, monkeypatch):
    src, dest = work / 'card', work / 'archive'
    _write(src / 'private.MP4', _clip(b'p'))
    calls = []
    original = ArchiveJob._copy_and_hash

    def copy(self, s, tmp):
        calls.append(os.path.basename(s))
        if len(calls) == 1:
            raise PermissionError(errno.EACCES, 'Permission denied', s)
        return original(self, s, tmp)
    monkeypatch.setattr(ArchiveJob, '_copy_and_hash', copy)

    ArchiveJob([str(src)], str(dest)).run()
    row = _rows()['private.MP4']
    assert row['status'] == 'error'
    assert row['error'].startswith('no permission')
    assert calls == ['private.MP4'], 'a permission failure is not tried again by itself'

    assert db.reset_errors() == 1          # the console's Retry failed
    ArchiveJob([str(src)], str(dest)).run()
    assert _rows()['private.MP4']['status'] == 'verified'


# ---- a full drive stops the run ------------------------------------------------

def test_a_full_archive_drive_stops_the_run_and_loses_nothing(work, monkeypatch):
    src, dest = work / 'card', work / 'archive'
    _write(src / 'a.MP4', _clip(b'a'))
    _failing_copy(monkeypatch, 'a.MP4',
                  lambda: OSError(errno.ENOSPC, 'No space left on device'))

    ArchiveJob([str(src)], str(dest)).run()

    job = db.latest_job()
    assert job['state'] == 'failed'
    assert 'archive drive is full' in job['message']
    assert _rows()['a.MP4']['status'] == 'pending', 'queued for the next Start, not failed'
    assert not _partials(dest)

    ArchiveJob([str(src)], str(dest)).run()
    assert _rows()['a.MP4']['status'] == 'verified'
