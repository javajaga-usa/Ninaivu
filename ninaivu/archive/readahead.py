"""Reading a source tree a little ahead of the walk, several requests at a time.

Measured on USB hard disks, a cold walk spends nearly all of its time waiting
for the disk: listing folders, and opening files to read their first bytes.
One request at a time cost 85 ms an operation on one disk; eight at once, 17 ms,
because a drive given several requests serves them in an order that saves head
travel.

The walk itself stays on one thread, in sorted order — which of two identical
photos is kept, and a dry run predicting the real run, both depend on that — so
nothing here decides anything. Worker threads only list folders and read file
headers ahead of where the walk has got to, and the walk picks the answers up.
Asking for something not yet read simply reads it on the walk's own thread, so
turning read-ahead off changes nothing but the time taken.
"""
from __future__ import annotations

import os
import sys
import threading

#: Worker threads for a counting walk. Eight is where the measured gain on a
#: spinning disk was taken; an SSD does not mind.
DEFAULT_THREADS = 8
#: How many folders may be listed ahead of the walk and not yet collected. The
#: walk is depth first; this keeps read-ahead near it rather than across the
#: whole drive, and bounds the directory entries held in memory.
DEFAULT_BUDGET = 512

_REPARSE_POINT = 0x400        # FILE_ATTRIBUTE_REPARSE_POINT
_MISSING = object()
_QUEUED = 'queued'
_RUNNING = 'running'


class Listing:
    """One folder's contents, as ``os.walk`` would describe them.

    ``dirs`` and ``files`` are names. ``entries`` maps each file name to its
    ``os.DirEntry``, whose size costs nothing more on Windows. ``links`` are
    folder symlinks, listed but never walked into (``followlinks=False``);
    ``junctions`` are other reparse points, which ``os.walk`` does walk into,
    and whose real location has to be looked up rather than assumed.
    """

    __slots__ = ('path', 'dirs', 'files', 'entries', 'links', 'junctions', 'error')

    def __init__(self, path, error=None):
        self.path = path
        self.dirs, self.files, self.entries = [], [], {}
        self.links, self.junctions = set(), set()
        self.error = error


def _is_junction(entry):
    check = getattr(entry, 'is_junction', None)           # Python 3.12+
    if check is not None:
        return check()
    if sys.platform == 'win32':
        attributes = getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0)
        return bool(attributes & _REPARSE_POINT)
    return False


def list_folder(path):
    """List *path* the way ``os.walk`` does, keeping what it throws away."""
    listing = Listing(path)
    try:
        # os.scandir is looked up on each call, so a test standing in a failing
        # listing reaches worker threads too.
        with os.scandir(path) as it:
            found = list(it)
    except OSError as exc:
        listing.error = exc
        return listing
    for entry in found:
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            listing.files.append(entry.name)
            listing.entries[entry.name] = entry
            continue
        listing.dirs.append(entry.name)
        try:
            if entry.is_symlink():
                listing.links.add(entry.name)
            elif _is_junction(entry):
                listing.junctions.add(entry.name)
        except OSError:
            listing.junctions.add(entry.name)     # unsure: resolve it the careful way
    return listing


def entry_size(entry):
    """A file's size from its directory entry, or None when it cannot be read."""
    try:
        return entry.stat().st_size
    except OSError:
        return None


class ReadAhead:
    """Lists folders and reads file headers ahead of a walk, on worker threads.

    ``skip_dir(name)`` says which child folders are not worth listing ahead.
    ``header_jobs(listing)`` returns ``[(kind, path)]`` of file reads worth doing
    ahead for a folder, and ``readers`` maps each kind to its reading function.

    The walk collects results with :meth:`listing` and :meth:`read`; anything not
    ready is read on the walk's own thread instead. It calls :meth:`finished`
    after a folder's files, and :meth:`discard` for a subtree it will not enter,
    so nothing read ahead for them is kept - or counted against the budget.
    """

    def __init__(self, threads=DEFAULT_THREADS, budget=DEFAULT_BUDGET, *,
                 skip_dir=None, header_jobs=None, readers=None):
        self.threads = max(0, int(threads))
        self.budget = budget
        self.skip_dir = skip_dir or (lambda name: False)
        self.header_jobs = header_jobs or (lambda listing: ())
        self.readers = dict(readers or {})
        self._lock = threading.Lock()
        self._work = threading.Condition(self._lock)
        self._stack = []           # (kind, path); workers take the newest first
        self._state = {}           # (kind, path) -> _QUEUED | _RUNNING | result
        self._ready = {}           # (kind, path) -> Event set when a worker is done
        self._headers = {}         # folder -> header keys queued for its files
        self._unexpanded = set()   # folders read ahead whose children were not all queued
        self._discarded = set()    # subtrees the walk will not enter
        self._outstanding = 0      # folders queued, running, or read and not collected
        self._workers = []
        self._closed = False

    # --- the walk's side -------------------------------------------------------

    def listing(self, path):
        """The listing of *path*: read ahead if it was, otherwise read now."""
        result, ahead = self._collect('list', path, list_folder)
        if self.threads and result.error is None:
            jobs = [] if ahead else self._header_jobs(result)
            with self._lock:
                # A folder read here never had anything queued; one read ahead
                # did, unless the budget stopped its children part way. Files
                # go on last so workers, taking the newest first, read them
                # before the child folders.
                if not ahead or path in self._unexpanded:
                    self._unexpanded.discard(path)
                    self._queue_children_locked(path, result)
                self._queue_headers_locked(path, jobs)
        return result

    def read(self, kind, path, reader=None):
        """The result of reading *path* for *kind*: read ahead if it was, else now."""
        return self._collect(kind, path, reader or self.readers[kind])[0]

    def finished(self, folder):
        """The walk is done with *folder*'s files: drop their unused header reads."""
        if not self.threads:
            return
        with self._lock:
            for key in self._headers.pop(folder, ()):
                if self._state.get(key, _MISSING) not in (_MISSING, _RUNNING):
                    self._forget(key)

    def discard(self, folder):
        """The walk will not enter *folder*: stop reading ahead anything inside it."""
        if not self.threads:
            return
        inside = folder.rstrip('\\/') + os.sep
        with self._lock:
            self._discarded.add(folder)
            for key in [k for k in self._state if k[1] == folder or k[1].startswith(inside)]:
                if self._state[key] != _RUNNING:      # a worker drops what it is reading
                    self._forget(key)
            for path in [p for p in self._headers if p == folder or p.startswith(inside)]:
                del self._headers[path]
            self._unexpanded = {p for p in self._unexpanded
                                if not (p == folder or p.startswith(inside))}

    def close(self):
        """Stop reading ahead. Work in flight finishes; nothing new starts."""
        with self._lock:
            self._closed = True
            self._stack.clear()
            self._state.clear()
            self._headers.clear()
            self._unexpanded.clear()
            self._work.notify_all()
            waiting = list(self._ready.values())
            self._ready.clear()
        for event in waiting:
            event.set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # --- internals (names ending in _locked expect the lock held) ------------

    def _collect(self, kind, path, reader):
        """(result, read_ahead): a worker's result if there is one, else read now."""
        key = (kind, path)
        if self.threads:
            with self._lock:
                state = self._state.get(key, _MISSING)
                waiting = None
                if state == _QUEUED:
                    self._forget(key)                # not started: read it here instead
                elif state == _RUNNING:
                    waiting = self._ready.setdefault(key, threading.Event())
                elif state is not _MISSING:
                    self._forget(key)
                    return state, True
            if waiting is not None:
                waiting.wait()
                with self._lock:
                    state = self._state.get(key, _MISSING)
                    if state not in (_MISSING, _QUEUED, _RUNNING):
                        self._forget(key)
                        return state, True
        return reader(path), False

    def _forget(self, key):
        """Remove a key queued or read ahead (lock held)."""
        if self._state.pop(key, _MISSING) is not _MISSING and key[0] == 'list':
            self._outstanding -= 1
        self._ready.pop(key, None)

    def _is_discarded(self, path):
        return any(path == d or path.startswith(d.rstrip('\\/') + os.sep)
                   for d in self._discarded)

    def _push(self, kind, path):
        key = (kind, path)
        if self._closed or key in self._state:
            return False
        self._state[key] = _QUEUED
        self._stack.append(key)
        if kind == 'list':
            self._outstanding += 1
        self._start_workers()
        self._work.notify()
        return True

    def _queue_children_locked(self, path, listing):
        if self._closed or self._is_discarded(path):
            return
        for name in reversed(listing.dirs):
            if name in listing.links or self.skip_dir(name):
                continue
            if self._outstanding >= self.budget:
                self._unexpanded.add(path)
                return
            self._push('list', os.path.join(path, name))

    def _header_jobs(self, listing):
        try:
            return [(kind, file_path) for kind, file_path in self.header_jobs(listing)
                    if kind in self.readers]
        except Exception:                         # noqa: BLE001 - only read-ahead is lost
            return []

    def _queue_headers_locked(self, folder, jobs):
        if self._closed or self._is_discarded(folder):
            return
        queued = self._headers.setdefault(folder, [])
        for kind, file_path in jobs:
            if self._push(kind, file_path):
                queued.append((kind, file_path))

    def _start_workers(self):
        while len(self._workers) < self.threads:
            worker = threading.Thread(target=self._run, name='archive-readahead', daemon=True)
            self._workers.append(worker)
            worker.start()

    def _run(self):
        while True:
            with self._lock:
                while not self._stack and not self._closed:
                    self._work.wait()
                if self._closed:
                    return
                key = self._stack.pop()
                if self._state.get(key) != _QUEUED:
                    continue                     # collected or discarded meanwhile
                self._state[key] = _RUNNING
            kind, path = key
            jobs = []
            try:
                result = list_folder(path) if kind == 'list' else self.readers[kind](path)
                if kind == 'list' and result.error is None:
                    jobs = self._header_jobs(result)
            except Exception:                     # noqa: BLE001 - the walk reads it itself
                result = _MISSING
            with self._lock:
                event = self._ready.pop(key, None)
                if self._state.get(key) != _RUNNING:
                    pass                          # discarded or closed while it ran
                elif result is _MISSING or self._closed or self._is_discarded(path):
                    self._forget(key)
                else:
                    if kind == 'list' and result.error is None:
                        # Queued before the listing is published, so the walk can
                        # never collect it and reach a child that is not queued
                        # yet, read that itself, and have it read again here.
                        # Files go on last so they are taken first.
                        try:
                            self._queue_children_locked(path, result)
                            self._queue_headers_locked(path, jobs)
                        except Exception:         # noqa: BLE001 - only read-ahead is lost
                            pass
                    self._state[key] = result
            # Always wake a walk waiting on this: if nothing was stored, it
            # finds nothing and reads the file itself.
            if event is not None:
                event.set()
