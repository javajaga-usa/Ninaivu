"""What changed in the library while Ninaivu was not running, from macOS itself.

A start-up scan that wants to be sure it has seen every new, changed or
removed photograph has to walk the whole library: some 208,000 files on a
large one, most of a minute on a spinning drive, for what is usually a
handful of new pictures in one or two day folders.

macOS already knows which folders those are. Every volume it can write to
keeps a journal of changes (FSEvents, the same record Time Machine and
Spotlight read), numbered by event ids that only ever go up. If the scan
writes down the event id it finished at, the next start-up can ask the
journal "which folders under the library changed after this id?" and read
only those (:func:`changes_since`).

The one rule here is that a change is never missed. The journal can be
unsure of itself: events dropped under load, ids that wrapped round, a
volume mounted or unmounted beneath the library, a volume that keeps no
journal at all (some exFAT and network drives), a journal that was
reset or comes from another computer. In every one of those cases, and when
the answer takes too long or is too big to be worth having,
:class:`History` says ``complete=False`` with a short reason for the log, and
the caller walks everything the way it always did.

Folders are reported, not files: the stream is created without the per-file
flag, so the journal answers with one entry per folder something happened
in, far fewer of them. Callers must rescan each reported folder *with
everything beneath it*: FSEvents may coalesce changes deep in a tree into one
entry for a folder above them (``MustScanSubDirs``), and a folder that was
moved in arrives as one entry for itself.

Nothing is loaded when this module is imported. On Linux and Windows
:func:`available` is ``False`` and the other functions raise
:class:`Unsupported` (:func:`volume_id` just answers ``None``), so callers
need no platform checks of their own.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Sequence

log = logging.getLogger(__name__)

_CORESERVICES = "/System/Library/Frameworks/CoreServices.framework/CoreServices"
_COREFOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"

_kCFStringEncodingUTF8 = 0x08000100

#: FSEventStreamCreate flags: deliver at once, folder-level events only.
_kFSEventStreamCreateFlagNoDefer = 0x2

# Event flags (FSEvents.h).
MUST_SCAN_SUBDIRS = 0x01
USER_DROPPED = 0x02
KERNEL_DROPPED = 0x04
EVENT_IDS_WRAPPED = 0x08
HISTORY_DONE = 0x10
ROOT_CHANGED = 0x20
MOUNT = 0x40
UNMOUNT = 0x80

#: Flags after which the journal's answer cannot be trusted, with why.
_UNRELIABLE = (
    (USER_DROPPED, "events dropped"),
    (KERNEL_DROPPED, "events dropped by the kernel"),
    (EVENT_IDS_WRAPPED, "event ids wrapped"),
    (ROOT_CHANGED, "library folder moved"),
    (MOUNT, "volume mounted under the library"),
    (UNMOUNT, "volume unmounted under the library"),
)
_UNRELIABLE_MASK = USER_DROPPED | KERNEL_DROPPED | EVENT_IDS_WRAPPED | ROOT_CHANGED | MOUNT | UNMOUNT


class Unsupported(RuntimeError):
    """Raised where there is no FSEvents journal to ask (anything but macOS,
    or a Mac whose CoreServices could not be loaded)."""


@dataclass(frozen=True)
class History:
    """The journal's answer. ``folders`` are absolute paths as FSEvents spells
    them (``/private/tmp/...`` rather than ``/tmp/...``, no trailing slash);
    each is to be rescanned with everything beneath it. When ``complete`` is
    false they are not the whole story and the caller walks everything."""

    folders: frozenset[str]
    complete: bool
    last_id: int
    reason: str = ""


# --- loading ----------------------------------------------------------------

_load_lock = threading.Lock()
_api: Any = None          # the loaded functions, once loaded
_load_failed = False


class _Api:
    """CoreServices, CoreFoundation and libdispatch, with their signatures."""

    def __init__(self) -> None:
        cs = ctypes.CDLL(_CORESERVICES)
        cf = ctypes.CDLL(_COREFOUNDATION)
        sysl = ctypes.CDLL(_LIBSYSTEM)
        vp = ctypes.c_void_p

        self.FSEventsGetCurrentEventId = cs.FSEventsGetCurrentEventId
        self.FSEventsGetCurrentEventId.argtypes = []
        self.FSEventsGetCurrentEventId.restype = ctypes.c_uint64

        self.FSEventsCopyUUIDForDevice = cs.FSEventsCopyUUIDForDevice
        self.FSEventsCopyUUIDForDevice.argtypes = [ctypes.c_int32]      # dev_t
        self.FSEventsCopyUUIDForDevice.restype = vp

        self.FSEventStreamCreate = cs.FSEventStreamCreate
        self.FSEventStreamCreate.argtypes = [vp, _CALLBACK, vp, vp, ctypes.c_uint64,
                                             ctypes.c_double, ctypes.c_uint32]
        self.FSEventStreamCreate.restype = vp
        self.FSEventStreamSetDispatchQueue = cs.FSEventStreamSetDispatchQueue
        self.FSEventStreamSetDispatchQueue.argtypes = [vp, vp]
        self.FSEventStreamSetDispatchQueue.restype = None
        self.FSEventStreamStart = cs.FSEventStreamStart
        self.FSEventStreamStart.argtypes = [vp]
        self.FSEventStreamStart.restype = ctypes.c_ubyte                # Boolean
        for name in ("FSEventStreamStop", "FSEventStreamInvalidate", "FSEventStreamRelease"):
            fn = getattr(cs, name)
            fn.argtypes = [vp]
            fn.restype = None
            setattr(self, name, fn)

        self.CFRelease = cf.CFRelease
        self.CFRelease.argtypes = [vp]
        self.CFRelease.restype = None
        self.CFStringCreateWithCString = cf.CFStringCreateWithCString
        self.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        self.CFStringCreateWithCString.restype = vp
        self.CFStringGetCString = cf.CFStringGetCString
        self.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.CFStringGetCString.restype = ctypes.c_ubyte
        self.CFUUIDCreateString = cf.CFUUIDCreateString
        self.CFUUIDCreateString.argtypes = [vp, vp]
        self.CFUUIDCreateString.restype = vp
        self.CFArrayCreate = cf.CFArrayCreate
        self.CFArrayCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.c_long, vp]
        self.CFArrayCreate.restype = vp
        # The callbacks struct itself: its address is what CFArrayCreate wants.
        self.kCFTypeArrayCallBacks = ctypes.addressof(vp.in_dll(cf, "kCFTypeArrayCallBacks"))

        self.dispatch_queue_create = sysl.dispatch_queue_create
        self.dispatch_queue_create.argtypes = [ctypes.c_char_p, vp]
        self.dispatch_queue_create.restype = vp
        self.dispatch_release = sysl.dispatch_release
        self.dispatch_release.argtypes = [vp]
        self.dispatch_release.restype = None


# void cb(ConstFSEventStreamRef, void *info, size_t n, void *paths (char **),
#         const FSEventStreamEventFlags *flags, const FSEventStreamEventId *ids)
_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                             ctypes.POINTER(ctypes.c_uint64))


def _load() -> Any:
    global _api, _load_failed
    if _api is not None or _load_failed:
        return _api
    with _load_lock:
        if _api is None and not _load_failed:
            if sys.platform != "darwin":
                _load_failed = True
            else:
                try:
                    _api = _Api()
                except (OSError, AttributeError, ValueError) as e:
                    log.info("FSEvents not available: %s", e)
                    _load_failed = True
    return _api


def _need() -> Any:
    api = _load()
    if api is None:
        raise Unsupported("FSEvents is only available on macOS")
    return api


def available() -> bool:
    """Whether the FSEvents journal can be asked here. Never raises."""
    try:
        return _load() is not None
    except Exception:                                    # pragma: no cover - belt and braces
        return False


def current_event_id() -> int:
    """The newest event id the system has handed out, across all volumes."""
    return int(_need().FSEventsGetCurrentEventId())


def _cfstring_to_str(api: Any, ref: int) -> str | None:
    buf = ctypes.create_string_buffer(256)
    if not api.CFStringGetCString(ref, buf, len(buf), _kCFStringEncodingUTF8):
        return None
    return buf.value.decode("utf-8")


def volume_id(path: str | os.PathLike) -> str | None:
    """The UUID of the event journal of the volume ``path`` is on, or ``None``
    when that volume keeps none, or on anything going wrong. A journal that was
    reset, or a drive written to by another Mac, gets a new UUID: the event ids
    stored against the old one mean nothing any more."""
    try:
        api = _load()
        if api is None:
            return None
        dev = ctypes.c_int32(os.stat(path).st_dev & 0xFFFFFFFF).value
        uuid = api.FSEventsCopyUUIDForDevice(dev)
        if not uuid:
            return None
        try:
            s = api.CFUUIDCreateString(None, uuid)
            if not s:
                return None
            try:
                return _cfstring_to_str(api, s) or None
            finally:
                api.CFRelease(s)
        finally:
            api.CFRelease(uuid)
    except Exception as e:
        log.debug("no FSEvents volume id for %s: %s", path, e)
        return None


# --- replaying the journal ----------------------------------------------------

def _clean(path: str) -> str:
    return path.rstrip("/") or "/"


class _Collector:
    """Sorts the events of one replay into folders, or a reason not to trust
    them. Plain Python with nothing of macOS in it, so it is tested anywhere."""

    def __init__(self, paths: Sequence[str], since: int, max_folders: int) -> None:
        roots: set[str] = set()
        for p in paths:
            p = os.fspath(p)
            roots.add(_clean(os.path.abspath(p)))
            roots.add(_clean(os.path.realpath(p)))
        self.roots = tuple(sorted(roots))
        self.max_folders = max_folders
        self.folders: set[str] = set()
        self.last_id = since
        self.reason = ""
        self.history_done = False
        self.lock = threading.Lock()
        self.finished = threading.Event()

    def _under(self, path: str) -> bool:
        return any(path == r or path.startswith(r + "/") or r == "/" for r in self.roots)

    def _covers_a_root(self, path: str) -> bool:
        """Whether ``path`` is a library folder or above one."""
        return any(r == path or path == "/" or r.startswith(path + "/") for r in self.roots)

    def _fail(self, reason: str) -> None:
        if not self.reason:
            self.reason = reason
        self.finished.set()

    def add(self, path: str, flags: int, event_id: int) -> None:
        """One event. Sets :attr:`finished` once nothing more is needed."""
        with self.lock:
            if self.finished.is_set():
                return
            if flags & HISTORY_DONE:
                self.history_done = True
                self.finished.set()
                return
            if event_id > self.last_id:
                self.last_id = event_id
            if flags & _UNRELIABLE_MASK:
                for bit, why in _UNRELIABLE:
                    if flags & bit:
                        self._fail(why)
                        return
            path = _clean(path)
            if flags & MUST_SCAN_SUBDIRS and self._covers_a_root(path):
                # Changes somewhere at or above the library, coalesced: the
                # whole library is to be read, which is the walk anyway.
                self._fail("whole library to rescan")
                return
            if not self._under(path):
                real = _clean(os.path.realpath(path))
                if not self._under(real):
                    return
            self.folders.add(path)
            if len(self.folders) > self.max_folders:
                self._fail("too many changes")

    def history(self, *, timed_out: bool = False) -> History:
        with self.lock:
            reason = self.reason
            if not reason and timed_out and not self.history_done:
                reason = "timed out"
            if not reason and not self.history_done:
                reason = "replay did not finish"
            return History(frozenset(self.folders), not reason, self.last_id, reason)


#: The replays running now, by stream. One callback serves them all and lives
#: as long as the module, so a late call from a stream being torn down finds
#: nothing rather than a freed function.
_running: dict[int, _Collector] = {}
_running_lock = threading.Lock()


def _on_events(stream, info, n, paths, flags, ids) -> None:  # noqa: ANN001 - C signature
    collector = None
    try:
        with _running_lock:
            collector = _running.get(stream or 0)
        if collector is None:
            return
        names = ctypes.cast(paths, ctypes.POINTER(ctypes.c_char_p))
        for i in range(n):
            raw = names[i] or b""
            collector.add(os.fsdecode(raw), int(flags[i]), int(ids[i]))
            if collector.finished.is_set():
                return
    except Exception as e:                               # never let one escape into C
        log.exception("FSEvents callback failed")
        if collector is not None:
            with collector.lock:
                collector._fail(f"callback failed: {e}")


_callback = _CALLBACK(_on_events)


def changes_since(paths: Sequence[str], since: int, *, timeout: float = 60.0,
                  max_folders: int = 5000) -> History:
    """The folders under ``paths`` where something changed after event id
    ``since`` (exclusive), from the volume's journal. Each folder is to be
    rescanned with everything beneath it. ``complete=False`` means the caller
    must walk everything: there was no usable id, the volume keeps no
    journal, events were lost, it took longer than ``timeout`` seconds, or
    more than ``max_folders`` folders changed."""
    api = _need()
    paths = [os.fspath(p) for p in paths]
    if since <= 0:
        return History(frozenset(), False, since, "no event id stored")
    if not paths:
        return History(frozenset(), False, since, "no folders given")
    for p in paths:
        if not os.path.isdir(p):
            return History(frozenset(), False, since, "library folder missing")
        if volume_id(p) is None:
            return History(frozenset(), False, since, "volume keeps no event history")
    now = int(api.FSEventsGetCurrentEventId())
    if since > now:
        # An id from the future: the journal was reset, or it is another
        # computer's. Nothing it says can be lined up with what we stored.
        return History(frozenset(), False, since, "event id ahead of the journal")

    collector = _Collector(paths, since, max_folders)
    cfstrings: list[int] = []
    array = stream = queue = None
    scheduled = False
    try:
        for p in paths:
            s = api.CFStringCreateWithCString(None, os.fsencode(p), _kCFStringEncodingUTF8)
            if not s:
                return History(frozenset(), False, since, "could not name a folder")
            cfstrings.append(s)
        values = (ctypes.c_void_p * len(cfstrings))(*cfstrings)
        array = api.CFArrayCreate(None, values, len(cfstrings), api.kCFTypeArrayCallBacks)
        if not array:
            return History(frozenset(), False, since, "could not make the folder list")
        stream = api.FSEventStreamCreate(None, _callback, None, array, ctypes.c_uint64(since),
                                         0.0, _kFSEventStreamCreateFlagNoDefer)
        if not stream:
            return History(frozenset(), False, since, "could not open the event stream")
        with _running_lock:
            _running[stream] = collector
        queue = api.dispatch_queue_create(b"ninaivu.fsevents", None)
        if not queue:
            return History(frozenset(), False, since, "could not make a queue")
        api.FSEventStreamSetDispatchQueue(stream, queue)
        scheduled = True
        if not api.FSEventStreamStart(stream):
            return History(frozenset(), False, since, "event stream did not start")
        finished = collector.finished.wait(timeout)
        api.FSEventStreamStop(stream)
        result = collector.history(timed_out=not finished)
    finally:
        if stream:
            # Invalidate only undoes a queue it was given; release either way.
            if scheduled:
                api.FSEventStreamInvalidate(stream)
            api.FSEventStreamRelease(stream)
        with _running_lock:
            for key in [k for k, c in _running.items() if c is collector]:
                del _running[key]
        if queue:
            api.dispatch_release(queue)
        if array:
            api.CFRelease(array)
        for s in cfstrings:
            api.CFRelease(s)
    if result.complete:
        log.info("FSEvents: %d changed folders since %d", len(result.folders), since)
    else:
        log.info("FSEvents: walking everything (%s)", result.reason)
    return result
