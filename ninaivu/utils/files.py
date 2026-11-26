"""Reading files the same way everywhere.

Five parts of Ninaivu hashed a file to say whether a copy matched -- the
second copy, the repair, the index backup, the restore test and the backup
tool -- and each had its own loop for it. One here, so a change to how it
reads (or a bug in it) is made once.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import sys
import shutil
import stat
from pathlib import Path

__all__ = ["CHUNK", "LOCK_FLAGS", "copystat_unlocked", "create_new", "file_flags", "real_path",
           "open_file_limit", "remove_own", "same_bytes", "sha256_file", "stays_inside",
           "sync_folder", "unlock", "atomic_write", "move_no_replace",
           "publish_no_replace"]

#: Bytes read at a time when a whole file is hashed or copied. Large enough
#: that a photograph is one read; small enough that a film does not sit in
#: memory.
CHUNK = 4 * 1024 * 1024


#: How many files Ninaivu asks to be allowed to hold open at once. A Mac
#: gives an app opened from Finder 256: the server's own threads each keep the
#: index open (three files apiece, with its write-ahead log), and a full
#: export summing 961,507 files on top of that ran out — "Too many open
#: files" — 700,000 files in, with the console's own pages failing alongside
#: it. Linux gives 1,024 and Windows has no such limit.
OPEN_FILES_WANTED = 10240


def open_file_limit(wanted: int = OPEN_FILES_WANTED) -> tuple[int, int] | None:
    """Raise this process's limit on open files to *wanted*, or as near it as
    the system allows; never lower it. ``(before, after)``, or None where
    there is no such limit to ask about (Windows)."""
    try:
        import resource                                  # noqa: PLC0415
    except ImportError:
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None
    if soft == resource.RLIM_INFINITY or soft >= wanted:
        return soft, soft
    target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    while target > soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            return soft, target
        except (OSError, ValueError):
            # A Mac refuses more than kern.maxfilesperproc, whatever the hard
            # limit says (which may say "unlimited").
            target //= 2
    return soft, soft


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


def real_path(path: Path | str) -> str:
    r"""*path* with every link along it followed, as ``os.path.realpath``
    gives it, but never in the ``\\?\`` long-path form.

    On Windows ``realpath`` of a path that does not exist yet looks twice,
    and when another thread makes one of its folders in between (four
    originals arriving at once into the same new day's folder), the answer
    comes back as ``\\?\C:\...``. That is not "inside" ``C:\...``, and the
    original was refused as a path no library holds. No real path begins
    with ``\\?\``, so taking it off is safe on every system.
    """
    real = os.path.realpath(path)
    if real.startswith("\\\\?\\UNC\\"):
        return "\\\\" + real[8:]
    if real.startswith("\\\\?\\"):
        return real[4:]
    return real


def stays_inside(base: Path | str, path: Path | str) -> bool:
    """Whether *path* is inside *base* once links are followed.

    ``abspath`` only tidies the spelling, so ``base/linked/photo.jpg`` passed
    even when ``linked`` was a link to somewhere else entirely, and whatever
    was written there landed outside the folder that was chosen. ``realpath``
    follows every link that already exists along the way; the parts that do
    not exist yet are kept as written, and those are made as real folders.
    """
    return Path(real_path(path)).is_relative_to(real_path(base))


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


def atomic_write(path: Path | str, data: str | bytes, *, encoding: str = "utf-8") -> None:
    """Replace *path* with *data*, whole or not at all, and make it last.

    Written beside it under a dot-name, synced, renamed over it and the folder
    synced, so a power cut leaves the old file or the new one and never an
    empty one. That is what a Pi on an SD card needs; the hand-written
    write-then-rename this replaces skipped both syncs (audit C-24).
    """
    path = Path(path)
    temporary = path.with_name(f".{path.name}.part")
    body = data.encode(encoding) if isinstance(data, str) else data
    try:
        with open(temporary, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    sync_folder(path.parent)


#: Errors meaning "this filesystem or system cannot do that", after which the
#: next, slower way is tried. EEXIST is never among them.
_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.EXDEV, errno.EPERM,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL)}


def _libc():
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None


def _rename_no_replace(src: str, dst: str) -> bool:
    """Rename *src* to *dst* in one step that fails if *dst* exists.

    True when done; False where the system or filesystem has no such rename
    (the caller then falls back). Raises FileExistsError when *dst* is taken,
    and any other OSError as it comes.
    """
    if os.name == "nt":
        # Windows' rename never replaces: an existing name is an error.
        try:
            os.rename(src, dst)
        except FileExistsError:
            raise
        except OSError as error:
            if error.errno in _UNSUPPORTED:
                return False
            raise
        return True
    libc = _libc()
    if libc is None:
        return False
    if sys.platform.startswith("linux"):
        call = getattr(libc, "renameat2", None)
        if call is None:
            return False
        at_fdcwd, rename_noreplace = -100, 1
        result = call(at_fdcwd, os.fsencode(src), at_fdcwd, os.fsencode(dst),
                      rename_noreplace)
    elif sys.platform == "darwin":
        call = getattr(libc, "renamex_np", None)
        if call is None:
            return False
        rename_excl = 0x00000004
        result = call(os.fsencode(src), os.fsencode(dst), rename_excl)
    else:
        return False
    if result == 0:
        return True
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), dst)
    if code in _UNSUPPORTED:
        return False
    raise OSError(code, os.strerror(code), dst)


def _copy_into_new(src: str, dst: str) -> None:
    """Copy *src* to a name made for it here, then drop *src*.

    The name is taken with ``O_EXCL``, so a file, folder or link that is there
    already, or appears at any moment, is never replaced: the create fails.
    Until the copy is whole and synced the name holds a part-written file; if
    the copy fails it is removed (only if it is still the one made here) and
    *src* is kept, so nothing is lost.
    """
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(dst, flags, 0o666)
    made = os.fstat(fd)
    try:
        with open(src, "rb") as source, os.fdopen(fd, "wb", closefd=False) as out:
            while chunk := source.read(CHUNK):
                out.write(chunk)
            out.flush()
            os.fsync(fd)
        info = os.stat(src)
        try:
            os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns),
                     follow_symlinks=False)
        except (NotImplementedError, OSError):
            os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns))
    except BaseException:
        os.close(fd)
        try:
            now = os.lstat(dst)
            if (now.st_dev, now.st_ino) == (made.st_dev, made.st_ino):
                os.unlink(dst)
        except OSError:
            pass
        raise
    os.close(fd)
    os.unlink(src)


def publish_no_replace(src: Path | str, dst: Path | str) -> None:
    """Move the finished file *src* to *dst*, never over anything at *dst*.

    Raises FileExistsError if *dst* is taken, now or by something that
    appears while this runs (audit F1: a check followed by a rename, on a
    drive without hard links, replaced a photograph imported in between).
    On any failure *src* is left where it was.

    In order: a hard link (atomic; most drives), a rename that refuses an
    existing name (Linux ``renameat2``, macOS ``renamex_np``, Windows'
    rename), then a copy into a name created exclusively (exFAT and FAT,
    which have neither).
    """
    src, dst = os.fspath(src), os.fspath(dst)
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        pass
    else:
        os.unlink(src)
        return
    move_no_replace(src, dst)


def move_no_replace(src: Path | str, dst: Path | str) -> None:
    """Rename *src* to *dst* without ever replacing anything there.

    The no-hard-link half of :func:`publish_no_replace`: a rename that refuses
    an existing name where the system has one, else a copy into a name made
    exclusively. FileExistsError if *dst* is taken; *src* kept on failure.
    """
    src, dst = os.fspath(src), os.fspath(dst)
    if _rename_no_replace(src, dst):
        return
    _copy_into_new(src, dst)
