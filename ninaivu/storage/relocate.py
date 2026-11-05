"""Relocate a closed state folder; leave the original as a recovery copy.

The move is asked for from the console (:func:`request`) and done at the next
start, before anything opens the index (:func:`apply_pending`):

* The new folder may be new, empty, or one that already holds a copy of this
  index: a move that stopped part-way, a copy made by hand, or the recovery
  copy of an earlier move. A file already there with the same size and time is
  not copied again, so a second attempt continues instead of starting over.
* Every file is then read back from the new folder, from the drive rather than
  from memory where the system allows it, and compared with the original. A
  file that comes back different is copied once more; if it still differs,
  the result says whether the original or the copy is at fault.
* Nothing points at the new folder until all of it is verified. A failed
  attempt goes on using the original, and keeps what it copied (marked with
  ``index-move-partial.json``) for the next attempt.

Each step is written to the server's log, and :func:`status` is what the
console shows.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

REDIRECT = 'index-location.json'
PENDING = 'index-move-pending.json'
RESULT = 'index-move-result.json'
MANIFEST = 'index-recovery-manifest.json'
#: In a folder being filled by a move: whose copy it is, so a later attempt
#: from the same index may continue it.
PARTIAL = 'index-move-partial.json'
_LOCKS = ('ninaivu-server.lock', 'ninaivu-starting.lock')
#: Bookkeeping at the top of the index folder that is never copied: the
#: move's own notes, the locks, and the notes of the process that is running.
_NOT_COPIED = frozenset({PENDING, REDIRECT, MANIFEST, RESULT, PARTIAL, *_LOCKS})
_RUNNING_NOTES = ('ninaivu.run', 'ninaivu.starting')
_CHUNK = 1024 * 1024
_HEADROOM = 64 * 1024 * 1024
#: File systems that keep times to the nearest 2 seconds (FAT, exFAT).
_SAME_TIME = 2.0

Progress = Callable[[str, int, int], None]


def _say(message: str) -> None:
    """Into the server's log. This runs before logging is set up, when the log
    is what the process prints (as storage/full_export.py does)."""
    print(f'Index move: {message}', flush=True)


def _size(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def _skipped(name: str) -> bool:
    return name in _NOT_COPIED or name.startswith(_RUNNING_NOTES)


def _record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _destination(record: object) -> Path:
    destination = record.get('destination') if isinstance(record, dict) else None
    if not isinstance(destination, str) or not destination or not Path(destination).is_absolute():
        raise ValueError('The saved index location must name an absolute folder path.')
    return Path(destination)


def resolve(folder: Path) -> Path:
    seen = set()
    folder = folder.expanduser().resolve()
    while (folder / REDIRECT).exists():
        if folder in seen:
            raise ValueError('The saved index locations contain a loop.')
        seen.add(folder)
        record = json.loads((folder / REDIRECT).read_text(encoding='utf-8'))
        folder = _destination(record).resolve()
    # Intermediate folders may contain only their forwarding note after the
    # recovery data has been removed. Only the final destination needs an index.
    if seen and not (folder / 'index.db').is_file():
        raise ValueError('The relocated index is unavailable. '
                         'Reconnect its drive before starting Ninaivu.')
    return folder


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    # The rename itself, durable too: the forwarding note above all, which
    # says where the index is after a power cut.
    if os.name != 'nt':
        try:
            handle = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        except OSError:
            pass


# --------------------------------------------------------------------------
# What may be moved where


def _files(folder: Path) -> dict[str, int]:
    """Every file to copy from *folder*, by its path inside it, with its size."""
    found: dict[str, int] = {}
    for top, dirs, names in os.walk(folder):
        here = Path(top)
        if here == folder:
            dirs[:] = [d for d in dirs if not _skipped(d)]
            names = [n for n in names if not _skipped(n)]
        for name in (*dirs, *names):
            if (here / name).is_symlink():
                _say(f'{(here / name).relative_to(folder).as_posix()} is a symbolic link')
                raise ValueError('The index folder contains a symbolic link. Move it manually.')
        for name in names:
            path = here / name
            found[path.relative_to(folder).as_posix()] = path.stat().st_size
    return found


def _held(folder: Path) -> dict[str, Path]:
    """Every file (and link) already in *folder*, the move's own notes aside."""
    found: dict[str, Path] = {}
    for top, dirs, names in os.walk(folder):
        here = Path(top)
        links = [d for d in dirs if (here / d).is_symlink()]
        dirs[:] = [d for d in dirs if d not in links]
        for name in (*names, *links):
            if here == folder and (name == PARTIAL or name in _LOCKS
                                   or name.startswith('ninaivu.starting')):
                continue
            found[(here / name).relative_to(folder).as_posix()] = here / name
    return found


def _copy_of(source: Path, destination: Path) -> bool:
    """Whether *destination* already holds a copy of the index in *source*."""
    if _record(destination / PARTIAL).get('source') == str(source):
        return True       # an attempt from here that stopped part-way
    if _record(destination / REDIRECT).get('destination') == str(source):
        return True       # where this index moved from: it is moving back
    if not (destination / 'index.db').is_file():
        return False
    if _record(destination / MANIFEST).get('source') == str(source):
        return True
    mine, theirs = _record(source / 'config.json'), _record(destination / 'config.json')
    return bool(mine.get('roots')) and mine.get('roots') == theirs.get('roots')


def validate(source: Path, destination: Path) -> Path:
    source = source.resolve()
    destination = destination.expanduser()
    if not destination.is_absolute():
        raise ValueError('Choose an absolute folder path on the Ninaivu computer.')
    destination = destination.resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError('Choose a folder outside the current index folder.')
    if destination.exists():
        if not destination.is_dir():
            raise ValueError('That is a file, not a folder.')
        if any(destination.iterdir()) and not _copy_of(source, destination):
            raise ValueError('That folder already holds other files. Choose an empty or new '
                             'folder, or one that holds a copy of this index.')
    elif not destination.parent.is_dir():
        raise ValueError('The parent of the new folder must already exist.')
    return destination


def _same(original: Path, copy: Path) -> bool:
    """Whether *copy* looks like *original* without reading either (the
    reading-back afterwards decides)."""
    try:
        a, b = original.stat(), copy.lstat()
    except OSError:
        return False
    return (not copy.is_symlink() and copy.is_file() and a.st_size == b.st_size
            and abs(a.st_mtime - b.st_mtime) < _SAME_TIME)


def _writable(folder: Path) -> None:
    try:
        handle, name = tempfile.mkstemp(dir=folder, prefix='.ninaivu-write-test-')
        os.close(handle)
        os.unlink(name)
    except OSError as exc:
        _say(f'cannot write in {folder}: {exc}')
        raise ValueError('Ninaivu cannot write in that folder. Check who owns it, '
                         'or choose another.') from exc


def request(source: Path, destination: Path) -> dict:
    """Ask for the move at the next start, after checking all that can be
    checked now, so a restart is not spent on a move that cannot work."""
    source = Path(source).resolve()
    destination = validate(source, destination)
    if not (source / 'index.db').is_file():
        raise ValueError('There is no index to move.')
    if (source / 'import-pending.json').exists() or (source / PENDING).exists():
        raise ValueError('An index import or move is already waiting. Finish it first.')
    files = _files(source)
    total = sum(files.values())
    there = (sum(size for rel, size in files.items() if _same(source / rel, destination / rel))
             if destination.exists() else 0)
    room = destination if destination.exists() else destination.parent
    _writable(room)
    free = shutil.disk_usage(room).free
    if free < total - there + _HEADROOM:
        _say(f'{destination} has {_size(free)} free; {_size(total - there)} has to be copied')
        raise ValueError('The destination does not have enough free space.')
    record = {'destination': str(destination), 'asked_at': time.time(),
              'files': len(files), 'bytes': total, 'bytes_to_copy': total - there}
    write(source / PENDING, record)
    # What the console shows is about this move from now on.
    (source / RESULT).unlink(missing_ok=True)
    _say(f'asked to move {source} to {destination} at the next start: {len(files)} files, '
         f'{_size(total)}, {_size(total - there)} of it to copy')
    return record


# --------------------------------------------------------------------------
# The move, at start


def _digest(path: Path) -> bytes:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').digest()


def _hash_copy(original: Path, copy: Path) -> bytes:
    """Copy *original* to *copy*; the sha256 of what was read."""
    digest = hashlib.sha256()
    copy.parent.mkdir(parents=True, exist_ok=True)
    with original.open('rb') as reading, copy.open('wb') as writing:
        while chunk := reading.read(_CHUNK):
            digest.update(chunk)
            writing.write(chunk)
    shutil.copystat(original, copy)
    return digest.digest()


def _flush() -> None:
    """Everything written so far onto the drives, so that reading it back reads
    the drive. Once for the whole copy: a sync per file takes hours on a disk
    with a million thumbnails."""
    if hasattr(os, 'sync'):
        os.sync()


def _read_back(path: Path) -> bytes:
    """The sha256 of *path* as the drive holds it, not as memory remembers it
    (Linux; elsewhere the system decides)."""
    with path.open('rb') as stream:
        if hasattr(os, 'posix_fadvise'):
            try:
                os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
        return hashlib.file_digest(stream, 'sha256').digest()


#: How long a move waits for other programs to finish writing in the index
#: folder before it gives up and leaves the index where it is.
WRITERS_PATIENCE = 5 * 60
#: How many times files changed during the copy are copied again.
_SETTLE_ROUNDS = 3


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _writers(folder: Path) -> list[str]:
    """Other processes with a file in *folder* open for writing (Linux; an
    empty list elsewhere, where the copy's own re-check is what guards).

    Logs are left out: the Control Panel holds the server's log open, and a
    log that grows during the move is copied again but stops nothing."""
    proc = Path('/proc')
    if not (proc / 'self' / 'fdinfo').is_dir():
        return []
    me = os.getpid()
    inside = str(folder) + os.sep
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            fds = list((entry / 'fd').iterdir())
        except OSError:
            continue                      # gone, or another user's
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            name = target.rsplit(os.sep, 1)[-1]
            if (not target.startswith(inside) or target.endswith((' (deleted)', '.log'))
                    or name in _LOCKS):
                continue
            try:
                flags = next(line for line in (entry / 'fdinfo' / fd.name).read_text().splitlines()
                             if line.startswith('flags:'))
                writing = int(flags.split()[1], 8) & 3
            except (OSError, StopIteration, ValueError, IndexError):
                continue
            if writing:
                try:
                    command = (entry / 'comm').read_text().strip()
                except OSError:
                    command = '?'
                found.append(f'{command} (process {entry.name}, {target[len(inside):]})')
                break
    return found


def _wait_for_writers(folder: Path, tell: Progress) -> None:
    """Wait until no other program is writing in *folder*.

    Ninaivu's own work (describing, scanning, consolidating, thumbnails,
    backups, sending to the Secondary) runs in the server, which has not
    started yet: it is paused by the move and takes up again after it. This
    waits for anything else: a converter left running by the server that
    stopped, a command run from a terminal."""
    began = time.monotonic()
    said = False
    while True:
        busy = _writers(folder)
        if not busy:
            if said:
                _say('nothing else is writing in the index folder now')
            return
        if not said:
            _say(f'waiting for other programs to finish writing in {folder}: {", ".join(busy[:5])}')
            said = True
        waited = time.monotonic() - began
        if waited >= WRITERS_PATIENCE:
            raise ValueError(f'Other programs are still writing in the index folder: '
                             f'{", ".join(busy[:5])}. The index stays where it is; close them, '
                             f'or restart the computer, and move again.')
        tell('waiting', int(waited), WRITERS_PATIENCE)
        time.sleep(2)


def _again(source: Path, destination: Path, rel: str, first: bytes,
           stamp: tuple[int, int] | None = None) -> bytes:
    """*rel* read back different from what was copied: copy it once more, and
    when it is still different, say which side is at fault."""
    _say(f'{rel} read back differently from {destination}; copying it again')
    changed = stamp is not None and _stamp(source / rel) != stamp
    second = _hash_copy(source / rel, destination / rel)
    _flush()
    back = _read_back(destination / rel)
    if back == second and (second == first or changed):
        # Right now; a file another program changed meanwhile is copied as it
        # is now, and the re-check after the copy sees that it changed.
        _say(f'{rel} is right on the second copy' + (' (it had changed meanwhile)' if changed else ''))
        return second
    if second != first or _digest(source / rel) != second:
        _say(f'{rel} reads differently each time it is read from {source}')
        raise ValueError(f'The original file reads differently each time: {rel}. The disk that '
                         f'holds the current index, or the memory of this computer, may be '
                         f'failing. The index stays where it is.')
    _say(f'{rel} keeps coming back different from {destination}')
    raise ValueError(f'The copy keeps coming back different from what was written: {rel}. The '
                     f'drive that holds {destination} may be failing, or its file system may '
                     f'not suit an index. The index stays where it is; try a folder on '
                     f'another drive.')


def apply_pending(source: Path, *, starts: ExitStack | None = None,
                  progress: Progress | None = None) -> Path | None:
    """Called before opening any database, with the source starting lock held.

    A server passes its startup stack so the destination stays locked until
    it acquires the server lock. Otherwise a second start could open the new
    folder between publication and the original start's handover.

    *progress* is told ``(stage, done, total)`` as it goes: ``waiting``
    (seconds) while another program is still writing in the index folder,
    then ``copying`` and ``verifying`` (bytes).
    """
    marker = source / PENDING
    if not marker.exists():
        return None
    from ..server import runfile
    tell = progress or (lambda _stage, _done, _total: None)
    began = time.time()
    destination = None
    created = False
    published = False
    copied = 0
    try:
        destination = validate(source, _destination(json.loads(marker.read_text(encoding='utf-8'))))
        # The settings are rewritten for the new folder at the end; a file
        # that cannot be is refused before anything is copied.
        settings = None
        if (source / 'config.json').exists():
            settings = json.loads((source / 'config.json').read_text(encoding='utf-8'))
            if not isinstance(settings, dict):
                raise ValueError('The index settings must contain a JSON object.')
        with runfile.server_lock(source), ExitStack() as copying:
            (starts if starts is not None else copying).enter_context(
                runfile.starting_lock(destination))
            if not destination.exists():
                destination.mkdir(mode=0o700)
                created = True
            # Nobody may be using a copy that is being brought up to date.
            copying.enter_context(runfile.server_lock(destination))
            write(destination / PARTIAL, {'source': str(source), 'started_at': began})
            _wait_for_writers(source, tell)
            files = _files(source)
            stamps = {rel: _stamp(source / rel) for rel in files}
            total = sum(files.values())
            stale = [rel for rel in _held(destination) if rel not in files]
            for rel in stale:
                # Older than the index being moved (a copy made earlier, a
                # journal left by a copy taken while Ninaivu ran): the index
                # alone says what belongs.
                (destination / rel).unlink()
            kept = {rel for rel in files if _same(source / rel, destination / rel)}
            needed = sum(size for rel, size in files.items() if rel not in kept)
            free = shutil.disk_usage(destination).free
            if free < needed + _HEADROOM:
                _say(f'{destination} has {_size(free)} free; {_size(needed)} has to be copied')
                raise ValueError('The destination does not have enough free space.')
            _say(f'moving {source} to {destination}: {len(files)} files, {_size(total)}; '
                 f'{len(kept)} files already there, {len(stale)} out-of-date files removed')

            digests: dict[str, bytes] = {}
            done = 0
            tell('copying', 0, total)
            for rel, size in files.items():
                if rel not in kept:
                    digests[rel] = _hash_copy(source / rel, destination / rel)
                    copied += size
                done += size
                tell('copying', done, total)
            _flush()
            _say(f'copied {_size(copied)} in {time.time() - began:.0f} s; reading it back')

            recovery_files = {}
            retried = []
            done = 0
            tell('verifying', 0, total)
            for rel, size in files.items():
                expected = digests.get(rel) or _digest(source / rel)
                if _read_back(destination / rel) != expected:
                    expected = _again(source, destination, rel, expected, stamps[rel])
                    retried.append(rel)
                recovery_files[rel] = {'sha256': expected.hex(), 'size': size}
                done += size
                tell('verifying', done, total)

            # Anything that changed while it was copied (another program
            # writing after all) is copied again, until nothing does.
            for attempt in range(1, _SETTLE_ROUNDS + 1):
                now = _files(source)
                gone = [rel for rel in files if rel not in now]
                changed = [rel for rel in now if rel not in files or _stamp(source / rel) != stamps[rel]]
                for rel in gone:
                    (destination / rel).unlink(missing_ok=True)
                    files.pop(rel)
                    recovery_files.pop(rel, None)
                for rel in changed:
                    stamps[rel] = _stamp(source / rel)
                    digest = _hash_copy(source / rel, destination / rel)
                    _flush()
                    if _read_back(destination / rel) != digest:
                        digest = _again(source, destination, rel, digest, stamps[rel])
                    files[rel] = now[rel]
                    recovery_files[rel] = {'sha256': digest.hex(), 'size': now[rel]}
                    copied += now[rel]
                moving = [rel for rel in (*changed, *gone) if not rel.endswith('.log')]
                if not moving:
                    break
                _say(f'{len(moving)} files changed while they were copied '
                     f'({", ".join(moving[:5])}); copied again')
                if attempt == _SETTLE_ROUNDS:
                    raise ValueError(f'Files in the index folder kept changing while it was copied '
                                     f'({", ".join(moving[:5])}): another program is still writing '
                                     f'there. The index stays where it is; close it and move again.')
                _wait_for_writers(source, tell)
            total = sum(files.values())

            if settings is not None:
                settings['state_dir'] = str(destination)
                write(destination / 'config.json', settings)
            seconds = round(time.time() - began, 1)
            write(destination / MANIFEST, {'source': str(source), 'destination': str(destination),
                                           'files': recovery_files})
            write(destination / RESULT, {
                'ok': True, 'at': time.time(), 'seconds': seconds,
                'source': str(source), 'destination': str(destination),
                'files': len(files), 'bytes': total, 'copied_bytes': copied,
                'already_there': len(kept), 'copied_again': retried,
                'recovery_files': len(recovery_files), 'recovery_bytes': total})
            (destination / PARTIAL).unlink(missing_ok=True)
            # The copy is the original byte for byte; whether the original was
            # sound is checked, and repaired if need be, as the start goes on.
            from . import index_check
            index_check.ask(destination, 'moved')
            write(source / REDIRECT, {'destination': str(destination)})
            published = True
            marker.unlink(missing_ok=True)
            (source / RESULT).unlink(missing_ok=True)
            _say(f'done in {seconds:.0f} s. {destination} is the index now; {source} is kept '
                 f'as a recovery copy')
            return destination
    except (OSError, ValueError, KeyError, runfile.AlreadyRunning) as error:
        # A failed attempt keeps the current library usable and reports why.
        if published:
            return destination
        kept_copy = False
        if destination is not None and destination.is_dir() and (destination / PARTIAL).exists():
            if created and not _held(destination):
                shutil.rmtree(destination, ignore_errors=True)
            else:
                kept_copy = True
        marker.unlink(missing_ok=True)
        write(source / RESULT, {
            'ok': False, 'at': time.time(), 'error': str(error), 'source': str(source),
            'destination': str(destination) if destination is not None else None,
            'kept_copy': kept_copy, 'copied_bytes': copied})
        _say(f'not done: {error}' + (f' What was copied stays in {destination}; moving there '
                                      f'again continues from it.' if kept_copy else ''))
        return None


# --------------------------------------------------------------------------
# What the console shows


def _usual_places() -> list[Path]:
    """Where a launcher names the index: the folders that may forward here."""
    named = [os.environ.get('NINAIVU_STATE_DIR', '')]
    if os.environ.get('XDG_DATA_HOME'):
        named.append(str(Path(os.environ['XDG_DATA_HOME']) / 'ninaivu'))
    named.append(str(Path.home() / '.ninaivu'))
    return [Path(p).expanduser().absolute() for p in named if p]


def status(state: Path) -> dict:
    """Where the index is, what forwards to it, and its move's state."""
    state = Path(state).resolve()
    out: dict = {'folder': str(state), 'free_bytes': None, 'forwarded_from': None,
                 'pending': None, 'last': None, 'recovery_folder': None,
                 'recovery_present': False}
    try:
        out['free_bytes'] = shutil.disk_usage(state).free
    except OSError:
        pass
    manifest = _record(state / MANIFEST)
    candidates = _usual_places()
    if isinstance(manifest.get('source'), str):
        candidates.append(Path(manifest['source']))
        out['recovery_folder'] = manifest['source']
        out['recovery_present'] = (Path(manifest['source']) / 'index.db').is_file()
    for place in candidates:
        try:
            if place.resolve() != state and resolve(place) == state:
                out['forwarded_from'] = str(place)
                break
        except (OSError, ValueError):
            continue
    pending = _record(state / PENDING)
    if isinstance(pending.get('destination'), str):
        out['pending'] = pending
    last = _record(state / RESULT)
    if last.get('ok') is True:
        # A result copied in with the folder from somewhere else is not this
        # folder's: only the one written by the move that brought it here.
        if last.get('destination') == str(state):
            out['last'] = last
    elif last.get('ok') is False:
        if last.get('source') == str(state) or ('source' not in last and not out['forwarded_from']):
            out['last'] = last
    return out
