"""Pendrives, external hard drives and phones on a cable: which are plugged
in now, and copying the library onto a drive. From Ninaivu Lite 1.5.

    connected()      what a person carried in: USB sticks, memory cards,
                     external hard drives, phones; never the computer's own
                     disks
    Watcher          which of them nobody has been asked about yet
    Exporter         the library's photos and videos onto one, in a thread

Standard library only, apart from a phone on Windows, which is found through
the shell as the Import page already finds it (``utils.devices``). On Windows
the drive letters come from kernel32 and each fixed drive is asked which bus
it hangs off, so a USB hard drive (which Windows calls "fixed", like the disk
inside) is still known as one that was carried in. Windows' "insert a disk"
box is turned off while asking, because an empty card reader would raise it.
On macOS a drive is a folder in /Volumes; on Linux, one under /media or
/run/media, and a phone the desktop has opened is under /run/user/<uid>/gvfs.

Nothing is written to a drive except by the Exporter, which only adds: it
never deletes or overwrites a file that is already there.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from ..words import filled, said
from .filenames import portable_name
from .files import copystat_unlocked, create_new, remove_own, same_bytes, sha256_file, sync_folder

log = logging.getLogger(__name__)

#: The folder on the drive that an export goes into.
EXPORT_FOLDER = "Ninaivu"
#: How long one reading of the drives is good for: the console asks every few
#: seconds, and asking Windows about every letter is not free.
CACHE_SECONDS = 2.0
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Drive:
    id: str          # stable while the same drive stays plugged in
    path: str        # its root: E:\ or /Volumes/NAME, or This PC\Phone for a phone
    label: str       # its name, or the path when it has none
    total: int
    free: int
    #: "drive", or "phone" for a phone on a USB cable
    kind: str = "drive"
    #: a phone Windows shows only in Explorer (MTP): no folder on a disk, but
    #: the Import page reads it as a source all the same
    shell: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def is_within(child: str, parent: str) -> bool:
    def norm(path: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def say(template: str, **params: Any) -> dict[str, Any]:
    """A message as English and as a sentence the console can translate."""
    values = {name: str(value) for name, value in params.items()}
    return {"text": filled(template, values), "key": template, "params": values}


def _drive_id(path: str, label: str, serial: str | int, total: int) -> str:
    raw = f"{path}|{label}|{serial}|{total}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _usage(path: str) -> tuple[int, int]:
    try:
        u = shutil.disk_usage(path)
        return u.total, u.free
    except OSError:
        return 0, 0


# --- Windows -------------------------------------------------------------------------

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
#: STORAGE_BUS_TYPE values of a drive that was plugged in: 1394, USB, SD, MMC.
CARRIED_BUSES = {4, 7, 12, 13}
IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
SEM_FAILCRITICALERRORS = 0x0001


def _windows_bus(letter: str) -> int | None:
    """Which bus the drive hangs off (STORAGE_DEVICE_DESCRIPTOR.BusType), or None.

    Opened with no access rights, which needs no administrator and does not
    wake a sleeping disk."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    handle = kernel32.CreateFileW(f"\\\\.\\{letter}:", 0, 3, None, 3, 0, None)
    if not handle or handle == invalid:
        return None
    try:
        query = (ctypes.c_uint32 * 3)(0, 0, 0)  # StorageDeviceProperty, PropertyStandardQuery
        out = (ctypes.c_ubyte * 1024)()
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(handle, IOCTL_STORAGE_QUERY_PROPERTY,
                                      ctypes.byref(query), ctypes.sizeof(query),
                                      ctypes.byref(out), ctypes.sizeof(out),
                                      ctypes.byref(returned), None)
        if not ok or returned.value < 32:
            return None
        return int.from_bytes(bytes(out[28:32]), "little")
    finally:
        kernel32.CloseHandle(handle)


def _windows_drives() -> list[Drive]:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    # No "There is no disk in the drive" box for an empty card reader.
    old_mode = ctypes.c_uint(0)
    try:
        set_mode = kernel32.SetThreadErrorMode
        set_mode(SEM_FAILCRITICALERRORS, ctypes.byref(old_mode))
        restore = lambda: set_mode(old_mode.value, None)  # noqa: E731
    except AttributeError:
        previous = kernel32.SetErrorMode(SEM_FAILCRITICALERRORS)
        restore = lambda: kernel32.SetErrorMode(previous)  # noqa: E731
    system = (os.environ.get("SystemDrive") or "C:")[:1].upper()
    found = []
    try:
        mask = kernel32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            letter = chr(ord("A") + i)
            if letter in "AB" or letter == system:
                continue
            root = f"{letter}:\\"
            kind = kernel32.GetDriveTypeW(root)
            if kind == DRIVE_FIXED:
                try:
                    carried = _windows_bus(letter) in CARRIED_BUSES
                except Exception:  # noqa: BLE001 — unknown is "the computer's own"
                    carried = False
                if not carried:
                    continue
            elif kind != DRIVE_REMOVABLE:
                continue
            name = ctypes.create_unicode_buffer(261)
            serial = ctypes.c_uint32(0)
            # Fails for an empty card reader: nothing to ask about.
            if not kernel32.GetVolumeInformationW(root, name, 261, ctypes.byref(serial),
                                                  None, None, None, 0):
                continue
            total, free = _usage(root)
            label = name.value.strip()
            found.append(Drive(_drive_id(root, label, serial.value, total), root,
                               label or root, total, free))
    finally:
        restore()
    return found


def _windows_phones() -> list[Drive]:
    """Phones and cameras Windows shows in Explorer, as the Import page
    reads them; [] without the optional ``comtypes``."""
    from . import devices                                    # noqa: PLC0415

    if not devices.available():
        return []
    return [Drive(_drive_id(d["path"], d["name"], "phone", 0), d["path"], d["name"],
                  0, 0, kind="phone", shell=True)
            for d in devices.list_devices()]


# --- macOS and Linux -----------------------------------------------------------------


def _mount_drives(parents: list[str]) -> list[Drive]:
    found = []
    seen = set()
    for parent in parents:
        try:
            names = sorted(os.listdir(parent))
        except OSError:
            continue
        for name in names:
            path = os.path.join(parent, name)
            if name.startswith(".") or not os.path.isdir(path):
                continue
            real = os.path.realpath(path)
            # macOS lists its own disk in /Volumes too, as a link to /.
            if real == "/" or real in seen or not os.path.ismount(real):
                continue
            seen.add(real)
            try:
                serial = os.stat(real).st_dev
            except OSError:
                continue
            total, free = _usage(real)
            found.append(Drive(_drive_id(real, name, serial, total), real, name, total, free))
    return found


def _gvfs_phones(parent: str | None = None) -> list[Drive]:
    """Linux: a phone on a cable, opened by the desktop (GNOME's gvfs) under
    /run/user/<uid>/gvfs as mtp:host=… or gphoto2:host=…: plain folders."""
    if parent is None:
        if not hasattr(os, "getuid"):
            return []
        parent = f"/run/user/{os.getuid()}/gvfs"
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return []
    found = []
    for name in names:
        if not name.startswith(("mtp:", "gphoto2:")):
            continue
        path = os.path.join(parent, name)
        label = name.split("=", 1)[-1].replace("_", " ") or name
        found.append(Drive(_drive_id(path, label, "phone", 0), path, label, 0, 0, kind="phone"))
    return found


def _posix_parents() -> list[str]:
    if sys.platform == "darwin":
        return ["/Volumes"]
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    parents = [os.path.join("/media", user), os.path.join("/run/media", user)] if user else []
    parents.append("/media")
    return parents


def connected() -> list[Drive]:
    """The drives plugged in now; [] when they cannot be read."""
    try:
        if sys.platform == "win32":
            found = _windows_drives()
            try:
                found += _windows_phones()
            except Exception:  # noqa: BLE001 — the drives still count
                log.debug("drives: could not list phones", exc_info=True)
            return found
        found = _mount_drives(_posix_parents())
        if sys.platform.startswith("linux"):
            found += _gvfs_phones()
        return found
    except Exception:  # noqa: BLE001 — a prompt is a nicety; never break the caller
        log.exception("could not list the drives")
        return []


# --- who has been asked ----------------------------------------------------------------


class Watcher:
    """The drives plugged in, and which of them still need the question.

    A drive is asked about once each time it is plugged in: answered (or set
    aside with Not now), it is not asked about again until it is taken out
    and put back."""

    def __init__(self, lister: Callable[[], list[Drive]] = connected) -> None:
        self.lister = lister
        self.lock = threading.Lock()
        self.answered: set[str] = set()
        self.cached: list[Drive] = []
        self.read_at = float("-inf")

    def drives(self) -> list[Drive]:
        with self.lock:
            now = time.monotonic()
            if now - self.read_at >= CACHE_SECONDS:
                self.cached = self.lister()
                self.read_at = now
                present = {d.id for d in self.cached}
                self.answered &= present     # taken out: ask again next time
            return list(self.cached)

    def find(self, drive_id: str) -> Drive | None:
        return next((d for d in self.drives() if d.id == drive_id), None)

    def pending(self) -> list[Drive]:
        drives = self.drives()
        with self.lock:
            return [d for d in drives if d.id not in self.answered]

    def answer(self, drive_id: str) -> None:
        with self.lock:
            self.answered.add(drive_id)


# --- copying the library onto a drive -------------------------------------------------------


def export_root(drive_path: str) -> str:
    return os.path.join(drive_path, EXPORT_FOLDER)


def _is_media(name: str) -> bool:
    from ..server.config import IMAGE_EXTS, RAW_EXTS, VIDEO_EXTS   # noqa: PLC0415

    return os.path.splitext(name)[1].lower() in IMAGE_EXTS | RAW_EXTS | VIDEO_EXTS


def _long(path: str) -> str:
    from ..archive.safety import long_path                         # noqa: PLC0415

    return long_path(path)


def _targets(folders: list[str], root: str) -> list[tuple[str, str]]:
    """Each library folder and where it goes on the drive: by its own name,
    with a number added when two folders share one."""
    out, used = [], set()
    for folder in folders:
        base = os.path.basename(os.path.normpath(folder)) or "Library"
        base = base.rstrip(":\\/") or "Library"
        name, n = base, 2
        while name.lower() in used:
            name, n = f"{base} ({n})", n + 1
        used.add(name.lower())
        out.append((folder, os.path.join(root, name)))
    return out


class Exporter:
    """One export at a time, on its own thread; the console polls progress()."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.cancel = threading.Event()
        self.state: dict[str, Any] = self._blank()

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {"running": False, "phase": "", "drive": "", "destination": "", "total": 0,
                "done": 0, "copied": 0, "skipped": 0, "errors": 0, "bytes_total": 0,
                "bytes_done": 0, "message": None, "finished_at": None}

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def progress(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state, running=self.running)

    def _set(self, **values: Any) -> None:
        with self.lock:
            self.state.update(values)

    def start(self, drive: Drive, folders: list[str], skip: list[str]) -> None:
        """Copy *folders* onto *drive*, stepping around *skip* (Ninaivu's own
        data folder, when it sits inside a library)."""
        if self.running:
            raise RuntimeError("busy")
        self.cancel.clear()
        root = export_root(drive.path)
        self.state = {**self._blank(), "running": True, "phase": "counting",
                      "drive": drive.label, "destination": root}
        self.thread = threading.Thread(target=self._run, args=(list(folders), root, list(skip)),
                                       name="drive-export", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.cancel.set()

    def wait(self, timeout: float = 60) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    def _walk(self, folders: list[str], root: str, skip: list[str]):
        """(source, target, size) for every photo and video in the library."""
        avoid = [root, *skip]
        for folder, target in _targets(folders, root):
            for here, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith(".")
                           and not any(is_within(os.path.join(here, d), a) for a in avoid if a)]
                rel = os.path.relpath(here, folder)
                for name in files:
                    if name.startswith(".") or not _is_media(name):
                        continue
                    src = os.path.join(here, name)
                    try:
                        size = os.stat(_long(src)).st_size
                    except OSError:
                        continue
                    # A pendrive is FAT or exFAT whatever the library is on:
                    # a name with ":" or "?" in it cannot be made there (and on
                    # NTFS ":" writes a hidden stream). Each part is made one
                    # that can; the library keeps its own names.
                    parts = [] if rel == os.curdir else [
                        portable_name(p, "folder") for p in rel.split(os.sep)]
                    dest = os.path.join(target, *parts, portable_name(name, "photo"))
                    yield src, os.path.normpath(dest), size

    def _run(self, folders: list[str], root: str, skip: list[str]) -> None:
        try:
            plan = []
            for item in self._walk(folders, root, skip):
                if self.cancel.is_set():
                    raise InterruptedError
                plan.append(item)
            # What is already there (same name, size and time, or the same
            # bytes) is not copied again, so an export to the same drive next
            # month only adds what is new.
            todo = []
            for src, dest, size in plan:
                place = _place(dest, size, src)
                if place:
                    todo.append((src, place, size))
            need = sum(size for _, _, size in todo)
            self._set(phase="copying", total=len(plan), skipped=len(plan) - len(todo),
                      done=len(plan) - len(todo), bytes_total=need)
            free = _usage(os.path.dirname(root) or root)[1]
            if need and free and need > free:
                self._set(phase="failed", message=say(
                    said("Not enough room on the drive: {need} needed, {free} free."),
                    need=_size(need), free=_size(free)))
                return
            for src, dest, size in todo:
                if self.cancel.is_set():
                    raise InterruptedError
                try:
                    _copy(src, dest, self.cancel)
                    with self.lock:
                        self.state["copied"] += 1
                except InterruptedError:
                    raise
                except OSError as exc:
                    log.warning("export: could not copy %s: %s", src, exc)
                    with self.lock:
                        self.state["errors"] += 1
                with self.lock:
                    self.state["done"] += 1
                    self.state["bytes_done"] += size
            s = self.progress()
            counts = {"copied": f"{s['copied']:,}", "skipped": f"{s['skipped']:,}",
                      "errors": f"{s['errors']:,}"}
            if s["errors"]:
                key = said("Copied {copied} photos and videos to the drive. {errors} could not be copied; see the log.")
            elif s["skipped"]:
                key = said("Copied {copied} photos and videos to the drive. {skipped} were there already.")
            else:
                key = said("Copied {copied} photos and videos to the drive.")
            self._set(phase="done", message=say(key, **counts))
        except InterruptedError:
            self._set(phase="stopped", message=say(
                said("Stopped. What was copied stays on the drive.")))
        except OSError as exc:
            log.warning("export to %s failed: %s", root, exc)
            self._set(phase="failed", message=say(
                said("Could not write to the drive. Is it still plugged in, and not read-only?")))
        finally:
            self._set(running=False, finished_at=time.time())


def _place(dest: str, size: int, src: str | None = None) -> str | None:
    """Where a file of *size* goes: *dest*, or "name (2).jpg" when a different
    file already has that name; None when it is there already.

    "There already" is the same size and modification time, or failing the
    time, the same bytes. Name and size alone kept a file that a pendrive
    pulled out too early left full-size but never written, for good.
    """
    stem, ext = os.path.splitext(dest)
    try:
        mine = os.stat(_long(src)) if src else None
    except OSError:
        mine = None
    for n in range(1, 1000):
        candidate = dest if n == 1 else f"{stem} ({n}){ext}"
        try:
            there = os.stat(_long(candidate))
        except OSError:
            return candidate
        if there.st_size != size:
            continue
        if mine is None:
            return None
        # FAT keeps times to two seconds.
        if abs(there.st_mtime - mine.st_mtime) <= 2 \
                or same_bytes(_long(src), _long(candidate)):
            return None
    return None


def _copy(src: str, dest: str, cancel: threading.Event) -> None:
    """Copy through a .partial file, so a drive pulled out mid-copy leaves no
    half photo under the real name; then keep the original's dates.

    The copy is synced before it takes its name and read back afterwards: a
    pendrive's writes sit in memory until they are flushed, and one pulled out
    early kept full-size files that held nothing.
    """
    os.makedirs(_long(os.path.dirname(dest)), exist_ok=True)
    tmp = dest + ".partial"
    digest = hashlib.sha256()
    try:
        with open(_long(src), "rb") as fin, create_new(_long(tmp)) as fout:
            while True:
                if cancel.is_set():
                    raise InterruptedError
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                digest.update(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        copystat_unlocked(_long(src), _long(tmp))
        os.replace(_long(tmp), _long(dest))
        sync_folder(_long(os.path.dirname(dest)))
        if sha256_file(_long(dest)) != digest.hexdigest():
            os.remove(_long(dest))
            raise OSError(f"the copy of {src} did not read back the same")
    except BaseException:
        try:
            remove_own(_long(tmp))
        except OSError:
            pass
        raise


def _size(n: int) -> str:
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{n} bytes"
