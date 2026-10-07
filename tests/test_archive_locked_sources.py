"""Locked source files (a dashcam's protected clips) archive on macOS.

A camera or dashcam marks a protected clip read-only on its card, and macOS
shows that as "Locked" (the ``uchg`` flag). ``shutil.copystat`` copied the
flag onto the archive's temporary, and macOS refuses to rename a locked file:
every protected clip failed with ``PermissionError: [Errno 1] Operation not
permitted`` at the final rename, and the locked temporary could not be
removed either.

Linux has no file flags, so the tests stand in for macOS: a set of locked
paths, with ``chflags``, ``copystat``, ``os.replace`` and ``os.remove``
behaving as they do there.
"""

import errno
import os
import shutil
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ninaivu import archive                                     # noqa: E402
from ninaivu.archive import database as db                      # noqa: E402
from ninaivu.archive import scanner                             # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                  # noqa: E402
from ninaivu.utils import files                                 # noqa: E402


@pytest.fixture()
def work(tmp_path):
    archive.configure(tmp_path)
    db.close_db()
    db.init_db()
    try:
        yield tmp_path
    finally:
        db.close_db()


@pytest.fixture()
def macos_locks(monkeypatch):
    """Paths in the returned set carry the Locked flag, macOS style."""
    locked = set()
    key = os.path.abspath
    real_copystat, real_replace, real_remove = shutil.copystat, os.replace, os.remove

    def file_flags(path):
        return stat.UF_IMMUTABLE if key(path) in locked else 0

    def chflags(path, flags, follow_symlinks=True):
        (locked.add if flags & files.LOCK_FLAGS else locked.discard)(key(path))

    def copystat(src, dst, *, follow_symlinks=True):
        real_copystat(src, dst, follow_symlinks=follow_symlinks)
        if key(src) in locked:          # copystat copies st_flags on macOS
            locked.add(key(dst))

    def refuse_if_locked(path):
        if key(path) in locked:
            raise PermissionError(errno.EPERM, 'Operation not permitted', path)

    def replace(src, dst, *args, **kwargs):
        refuse_if_locked(src)
        real_replace(src, dst, *args, **kwargs)

    def remove(path, *args, **kwargs):
        refuse_if_locked(path)
        real_remove(path, *args, **kwargs)

    monkeypatch.setattr(files, 'file_flags', file_flags)
    monkeypatch.setattr(os, 'chflags', chflags, raising=False)
    monkeypatch.setattr(shutil, 'copystat', copystat)
    monkeypatch.setattr(os, 'replace', replace)
    monkeypatch.setattr(os, 'remove', remove)
    return locked


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(data)
    return str(path)


def _archived(dest):
    return [os.path.join(base, name) for base, _, names in os.walk(dest)
            for name in names]


def test_a_locked_clip_is_archived_and_the_copy_is_not_locked(work, macos_locks):
    src, dest = work / 'card', work / 'archive'
    clip = _write(src / '20250913_102818_7814_E_C.MP4', b'\x00\x00\x00\x18ftypmp42' + b'v' * 4096)
    _write(src / 'plain.MP4', b'\x00\x00\x00\x18ftypmp42' + b'p' * 4096)
    macos_locks.add(os.path.abspath(clip))

    ArchiveJob([str(src)], str(dest)).run()

    rows = {os.path.basename(r['source_path']): r for r in db.get_recent_files(100)}
    assert rows['20250913_102818_7814_E_C.MP4']['status'] == 'verified', rows
    assert rows['plain.MP4']['status'] == 'verified', rows
    landed = rows['20250913_102818_7814_E_C.MP4']['destination_path']
    assert os.path.abspath(landed) not in macos_locks, 'the archive copy kept the lock'
    assert os.path.abspath(clip) in macos_locks, 'the source must be left as it was'
    assert not [p for p in _archived(dest)
                if os.path.basename(p).startswith(scanner.PARTIAL_PREFIX)]


def test_a_rename_that_stays_refused_gives_a_plain_message(work, macos_locks, monkeypatch):
    src, dest = work / 'card', work / 'archive'
    _write(src / 'clip.MP4', b'\x00\x00\x00\x18ftypmp42' + b'c' * 4096)
    real_replace = os.replace

    def refuse_partials(a, b, *args, **kwargs):
        if os.path.basename(a).startswith(scanner.PARTIAL_PREFIX):
            raise PermissionError(errno.EPERM, 'Operation not permitted', a)
        real_replace(a, b, *args, **kwargs)
    monkeypatch.setattr(os, 'replace', refuse_partials)

    ArchiveJob([str(src)], str(dest)).run()

    row = db.get_recent_files(10)[0]
    assert row['status'] == 'error'
    assert 'could not be renamed into the archive folder' in row['error']
    assert 'tried again on the next run' in row['error']
    assert scanner.PARTIAL_PREFIX not in row['error']


def test_the_sweep_removes_a_locked_leftover_temporary(tmp_path, macos_locks):
    leftover = _write(tmp_path / f'{scanner.PARTIAL_PREFIX}1-2-3.tmp', b'x')
    macos_locks.add(os.path.abspath(leftover))

    files.remove_own(leftover)

    assert not os.path.exists(leftover)
