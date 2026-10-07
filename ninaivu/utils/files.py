"""Reading files the same way everywhere.

Five parts of Ninaivu hashed a file to say whether a copy matched -- the
second copy, the repair, the index backup, the restore test and the backup
tool -- and each had its own loop for it. One here, so a change to how it
reads (or a bug in it) is made once.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

__all__ = ["CHUNK", "LOCK_FLAGS", "copystat_unlocked", "create_new", "file_flags", "remove_own",
           "same_bytes", "sha256_file", "stays_inside", "sync_folder", "unlock"]

#: Bytes read at a time when a whole file is hashed or copied. Large enough
#: that a photograph is one read; small enough that a film does not sit in
#: memory.
CHUNK = 4 * 1024 * 1024


def sha256_file(path: Path | str, chunk: int = CHUNK) -> str:
    """The SHA-256 of the file's bytes, as hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def same_bytes(a: Path | str, b: Path | str, chunk: int = CHUNK) -> bool:
    """Whether two files hold exactly the same bytes. False if either cannot
    be read."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as one, open(b, "rb") as other:
            while True:
                left, right = one.read(chunk), other.read(chunk)
                if left != right:
                    return False
                if not left:
                    return True
    except OSError:
        return False


def stays_inside(base: Path | str, path: Path | str) -> bool:
    """Whether *path* is inside *base* once links are followed.

    ``abspath`` only tidies the spelling, so ``base/linked/photo.jpg`` passed
    even when ``linked`` was a link to somewhere else entirely, and whatever
    was written there landed outside the folder that was chosen. ``realpath``
    follows every link that already exists along the way; the parts that do
    not exist yet are kept as written, and those are made as real folders.
    """
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(base))


def create_new(path: Path | str):
    """Open a temporary file at *path* for writing, never through a link.

    Repair, the second copy and the sidecar writer each write under a
    predictable name beside the real file (``.photo.jpg.ninaivu-part``), and
    ``open(path, "wb")`` follows a link planted at that name: the bytes landed
    wherever it pointed, outside the library. Here the file is created only if
    no entry has that name (``O_EXCL``, which does not follow a final link),
    and with ``O_NOFOLLOW`` where the platform has it. A leftover regular file
    from an earlier crash is removed first; a link is refused, not removed and
    not followed.
    """
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    for _ in range(2):
        try:
            fd = os.open(path, flags, 0o666)
        except FileExistsError:
            if os.path.islink(path):
                raise OSError(f"refusing to write through a link at {path}") from None
            os.unlink(path)
            continue
        return os.fdopen(fd, "wb")
    raise OSError(f"could not create {path}: something keeps putting a file there")


#: The owner-settable lock flags: "Locked" in Finder (``uchg``) and
#: append-only (``uappnd``). ``shutil.copystat`` copies them from the source
#: on macOS, and a locked file can be neither renamed nor removed: the rename
#: of the finished temporary into place fails with EPERM. Camera and dashcam
#: cards mark protected clips read-only, which macOS shows as exactly this.
LOCK_FLAGS = stat.UF_IMMUTABLE | stat.UF_APPEND


def file_flags(path: Path | str) -> int:
    """The file's BSD flags, or 0 where the platform has none (Linux, Windows)."""
    try:
        return getattr(os.lstat(path), "st_flags", 0) or 0
    except OSError:
        return 0


def unlock(path: Path | str) -> bool:
    """Clear the lock flags on a file Ninaivu wrote. True if any were cleared.

    Only for our own copies: the source is never touched. Never raises; a
    file that stays locked is reported by whatever then fails on it.
    """
    chflags = getattr(os, "chflags", None)
    if chflags is None:
        return False
    flags = file_flags(path)
    if not flags & LOCK_FLAGS:
        return False
    try:
        chflags(path, flags & ~LOCK_FLAGS, follow_symlinks=False)
    except (OSError, NotImplementedError):
        return False
    return True


def copystat_unlocked(src: Path | str, dst: Path | str) -> None:
    """``shutil.copystat`` without carrying a lock across to the copy.

    Times, permissions and extended attributes still come with it; the
    "Locked" flag does not, so the copy can be renamed into place, hidden,
    and later moved or deleted by Ninaivu like any other file.
    """
    try:
        shutil.copystat(src, dst)
    finally:
        unlock(dst)


def remove_own(path: Path | str) -> None:
    """Remove a file Ninaivu wrote, unlocking it first if it was locked."""
    try:
        os.remove(path)
    except PermissionError:
        if not unlock(path):
            raise
        os.remove(path)


def sync_folder(folder: Path | str) -> None:
    """Make the renames and new names in *folder* survive a power cut.

    Syncing a file makes its bytes durable, not its name: after ``os.replace``
    the new name lives in the folder, and until the folder is synced a power
    cut can bring back the old one. Not on Windows, where a folder cannot be
    opened this way and NTFS journals the rename itself. Never raises: this
    makes a good outcome likelier, and its failure is not a failed copy.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
