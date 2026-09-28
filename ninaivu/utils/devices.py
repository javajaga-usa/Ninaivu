"""Phones and cameras, which Windows shows as folders and are not folders.

Plug an iPhone into a Windows machine and Explorer shows
``This PC\\Apple iPhone\\Internal Storage\\DCIM``. It looks exactly like a
path. It is not one: there is no drive letter, no UNC name, and nothing in
``os`` can open it. The device speaks MTP — a protocol for asking a device for
one file at a time — and Explorer is a client for it, not a view of a disk.

So this module is a second, small file layer that speaks the only dialect
Windows offers from Python without a compiler: the shell namespace, through
COM. It can list what is on a device and copy files off it, and that is all
it needs to do, because everything downstream of the copy is ordinary files
in an ordinary folder.

**Why copy rather than read in place.** The archive engine hashes every file
it moves and verifies the copy before it counts it done. That needs bytes it
can re-read, and MTP is the wrong medium for that: it is slow, it does not
seek well, and a phone will drop the connection if it sleeps. Copying to a
staging folder first turns an unreliable device into a reliable folder, and
means an interrupted import costs the transfer rather than the archive.

**What this deliberately does not do.** It does not delete anything from the
device, ever. It does not write to the device. A phone is not a backup
target and Ninaivu will not treat it as one.

Windows only, and unavailable without ``comtypes``. Everywhere else, and on a
machine without it, :func:`available` is False and the caller says so.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)

__all__ = [
    "available", "unavailable_reason", "list_devices", "list_folder",
    "looks_like_device_path", "split_device_path", "copy_out", "DeviceEntry",
]

#: "This PC" in the shell's numbering. The one namespace every device hangs off.
SSF_DRIVES = 17

#: Detail columns are localised strings, so they are read by index rather than
#: by name. These indices are stable across Windows versions for the shell's
#: standard columns.
COL_NAME, COL_SIZE, COL_TYPE, COL_MODIFIED = 0, 1, 2, 3

#: CopyHere flags: no progress dialog, no confirmation, no undo record.
#: 4 = no progress box, 16 = yes to all, 512 = do not confirm creating a
#: directory, 1024 = no error UI. A copy that opens a dialog on the server's
#: desktop is a copy nobody will ever click.
_COPY_FLAGS = 4 | 16 | 512 | 1024

#: How long to wait for one file to appear and stop growing after CopyHere.
#: The shell copy is asynchronous and reports nothing, so the destination is
#: watched instead.
_FILE_TIMEOUT = 300.0
_SETTLE = 0.35
#: A copy that has not grown for this long is checked on: is the device there?
_STALL = 15.0

#: How long an import waits for a device that has stopped answering before it
#: gives up and says so. An iPhone hides its storage the moment it locks, so
#: this is mostly the time it takes somebody to notice and unlock it.
_DEVICE_WAIT = 600.0
_DEVICE_POLL = 3.0

_com_lock = threading.Lock()


class DeviceEntry(dict):
    """One thing on a device: a folder or a file, named the way the shell names it."""


class DeviceGone(OSError):
    """The device stopped answering part-way through and did not come back."""


def unavailable_reason() -> str | None:
    """Why device access will not work here, or None when it will."""
    if os.name != "nt":
        return ("Reading a phone or camera directly is a Windows feature — it "
                "goes through the Windows shell, which only exists there.")
    try:
        import comtypes  # noqa: F401  # PLC0415
    except ImportError:
        return ("Reading a phone or camera needs the `comtypes` package, "
                "which is not installed. `pip install comtypes`, or run "
                "start.py again to install the optional extras.")
    return None


def available() -> bool:
    return unavailable_reason() is None


def _shell():
    """The shell automation object, on a thread that has initialised COM.

    Every thread that touches COM has to initialise it, and a Flask request
    runs on whichever pool thread is free — so this is done per call rather
    than once at import, which is the difference between working and failing
    on the second request.
    """
    import comtypes.client                                  # noqa: PLC0415

    try:
        import comtypes                                     # noqa: PLC0415

        comtypes.CoInitialize()
    except Exception:                                       # noqa: BLE001
        pass
    return comtypes.client.CreateObject("Shell.Application")


# ---------------------------------------------------------------------------
# Recognising and splitting a device path
# ---------------------------------------------------------------------------

#: What Explorer's address bar shows above a device.
_ROOTS = ("this pc", "computer", "my computer")


def looks_like_device_path(text: str) -> bool:
    """Is this the shell's spelling of something on a device?"""
    raw = str(text or "").strip().strip('"').replace("/", "\\")
    if not raw:
        return False
    if raw.startswith("::{"):
        return True
    head = raw.lstrip("\\").split("\\", 1)[0].lower()
    return head in _ROOTS


def split_device_path(text: str) -> tuple[str, list[str]]:
    """``("Apple iPhone", ["Internal Storage", "DCIM"])`` — device, then the way in."""
    raw = str(text or "").strip().strip('"').replace("/", "\\")
    parts = [p for p in raw.split("\\") if p]
    if parts and parts[0].lower() in _ROOTS:
        parts = parts[1:]
    if not parts:
        return "", []
    return parts[0], parts[1:]


# ---------------------------------------------------------------------------
# Looking
# ---------------------------------------------------------------------------

def _details(folder, item, column: int) -> str:
    try:
        return str(folder.GetDetailsOf(item, column) or "").strip()
    except Exception:                                       # noqa: BLE001
        return ""


def _detect_columns(folder) -> tuple[int, int, int, int]:
    """Identify column indices for (name, size, type, modified).

    Windows shell columns vary depending on whether the namespace is a disk
    volume or an MTP/WPD device (where Type is often col 1 and Size is col 2).
    """
    col_name, col_size, col_type, col_mod = COL_NAME, COL_SIZE, COL_TYPE, COL_MODIFIED
    try:
        for i in range(15):
            title = str(folder.GetDetailsOf(None, i) or "").strip().lower()
            if not title:
                continue
            if any(k in title for k in ("size", "größe", "taille", "tamano", "tamanho")):
                col_size = i
            elif any(k in title for k in ("type", "typ", "tipo")):
                col_type = i
            elif any(k in title for k in ("date modified", "modified", "änderungsdatum", "modifié")):
                col_mod = i
    except Exception:                                       # noqa: BLE001
        pass
    return col_name, col_size, col_type, col_mod


_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}


def _exact_size(item) -> int | None:
    """The size in bytes, where the shell will say.

    The Size column is text rounded to a unit ("3,492 KB"), which cannot tell a
    finished copy from one cut off in its last kilobyte. ``System.Size`` can.
    """
    try:
        size = int(item.ExtendedProperty("System.Size"))
    except Exception:                                       # noqa: BLE001
        return None
    return size if size > 0 else None


def _parse_size(text: str) -> int:
    """The shell reports sizes as localised text like "3.492 KB". Best effort.

    Used for showing a total and nothing else — the real size is taken from
    the copied file, which is the only number worth trusting anyway.
    """
    match = re.match(r"([\d.,\s]+)\s*([A-Za-z]+)", str(text or ""))
    if not match:
        return 0
    number = match.group(1).replace("\u00a0", "").replace(" ", "")
    # Either separator may be the decimal point depending on locale; the last
    # one that appears with 1-2 digits after it wins.
    number = re.sub(r"[.,](?=\d{3}\b)", "", number)
    number = number.replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return 0
    return int(value * _SIZE_UNITS.get(match.group(2).lower(), 1))


def list_devices() -> list[DeviceEntry]:
    """Every phone, camera or player Windows can currently see."""
    reason = unavailable_reason()
    if reason:
        raise RuntimeError(reason)
    with _com_lock:
        shell = _shell()
        computer = shell.NameSpace(SSF_DRIVES)
        if computer is None:
            return []
        found: list[DeviceEntry] = []
        for item in computer.Items():
            try:
                if not item.IsFolder:
                    continue
                path = str(item.Path or "")
                # A drive has a letter. A device does not — that is the whole
                # distinction, and it is more reliable than matching names.
                if re.match(r"^[A-Za-z]:\\?$", path):
                    continue
                if not path.startswith("::{") and ":" in path[:3]:
                    continue
                found.append(DeviceEntry(
                    name=str(item.Name or ""),
                    path=f"This PC\\{item.Name}",
                    shell_path=path,
                    kind=_details(computer, item, COL_TYPE),
                ))
            except Exception as exc:                        # noqa: BLE001
                log.debug("devices: skipped an entry — %s", exc)
    return found


def _descend(shell, device: str, parts: list[str]):
    """The shell folder for a device path, or None if any step is missing."""
    computer = shell.NameSpace(SSF_DRIVES)
    if computer is None:
        return None
    current = None
    for item in computer.Items():
        if str(item.Name or "").lower() == device.lower():
            current = item.GetFolder
            break
    if current is None:
        return None
    for part in parts:
        nxt = None
        for item in current.Items():
            if str(item.Name or "").lower() == part.lower() and item.IsFolder:
                nxt = item.GetFolder
                break
        if nxt is None:
            return None
        current = nxt
    return current


def folder_exists(text: str) -> bool:
    """Check whether a folder on a device exists without enumerating its files."""
    reason = unavailable_reason()
    if reason:
        return False
    device, parts = split_device_path(text)
    if not device:
        try:
            return bool(list_devices())
        except Exception:
            return False
    with _com_lock:
        shell = _shell()
        folder = _descend(shell, device, parts)
        return folder is not None


def _device_answers(text: str) -> bool:
    """Whether the device holding *text* is still showing its storage.

    Asked when something on it could not be found, to tell a folder or file
    that has gone from a device that has. A locked iPhone is still listed
    under This PC, but with nothing inside it, so the storage level — the
    first step below the device — is what is looked for.
    """
    device, parts = split_device_path(text)
    if not device:
        return True
    try:
        with _com_lock:
            folder = _descend(_shell(), device, parts[:1])
            if folder is None:
                return False
            return bool(parts) or any(True for _ in folder.Items())
    except Exception:                                       # noqa: BLE001
        return False


def _wait_for_device(text: str, should_stop: Callable[[], bool] | None = None,
                     waiting: Callable[[str], None] | None = None) -> bool:
    """Wait for a device that stopped answering. True once it answers again.

    An iPhone hides its storage the moment it locks, and a knocked cable drops
    the device for a second. Either way every file after that point used to
    fail at once — 453 of them in one minute on the import this was written
    for, each logged as a failed copy, in a run that then said it was done —
    when all that was needed was for somebody to unlock the phone.
    """
    deadline = time.monotonic() + _DEVICE_WAIT
    told = False
    while True:
        if should_stop and should_stop():
            return False
        if _device_answers(text):
            if told:
                log.info("devices: %s is answering again; carrying on", text)
            return True
        if time.monotonic() >= deadline:
            return False
        if not told:
            told = True
            log.warning("devices: %s stopped answering — it may have locked "
                        "itself or been unplugged. Waiting up to %d minutes; "
                        "unlock it to carry on.", text, int(_DEVICE_WAIT // 60))
            if waiting:
                waiting(text)
        pause_until = min(deadline, time.monotonic() + _DEVICE_POLL)
        while time.monotonic() < pause_until:
            if should_stop and should_stop():
                return False
            time.sleep(0.1)


def list_folder(text: str) -> list[DeviceEntry]:
    """What is inside one folder on a device."""
    reason = unavailable_reason()
    if reason:
        raise RuntimeError(reason)
    device, parts = split_device_path(text)
    if not device:
        return [DeviceEntry(d) for d in list_devices()]
    with _com_lock:
        shell = _shell()
        folder = _descend(shell, device, parts)
        if folder is None:
            raise FileNotFoundError(
                f"“{text}” is not on the device any more. If the phone has "
                f"locked itself, unlock it and try again — Windows hides the "
                f"storage until you do.")
        out: list[DeviceEntry] = []
        base = "\\".join(["This PC", device, *parts])
        col_name, col_size, col_type, col_mod = _detect_columns(folder)
        for item in folder.Items():
            try:
                raw_size = _details(folder, item, col_size)
                size_val = _parse_size(raw_size)
                if size_val == 0 and not item.IsFolder:
                    for alt_col in (COL_SIZE, 1, 2):
                        alt_val = _parse_size(_details(folder, item, alt_col))
                        if alt_val > 0:
                            size_val = alt_val
                            break
                out.append(DeviceEntry(
                    name=str(item.Name or ""),
                    path=f"{base}\\{item.Name}",
                    is_folder=bool(item.IsFolder),
                    size=size_val,
                    exact_size=None if item.IsFolder else _exact_size(item),
                    modified=_details(folder, item, col_mod),
                ))
            except Exception as exc:                        # noqa: BLE001
                log.debug("devices: skipped an entry — %s", exc)
    return out


def walk(text: str, *, should_stop: Callable[[], bool] | None = None,
         patient: bool = False,
         waiting: Callable[[str], None] | None = None) -> Iterator[DeviceEntry]:
    """Every file under a device folder, depth first.

    A phone's camera roll is one enormous folder in the usual case, but an
    iPhone splits it into ``100APPLE``, ``101APPLE`` and so on, so this has to
    descend.

    A folder that cannot be found is stepped over. With *patient*, that is
    only once the device is known to still be there: a folder "missing"
    because the phone locked is waited for and read again, and if the phone
    does not come back :class:`DeviceGone` is raised rather than every folder
    not yet read being quietly left out.
    """
    stack = [text]
    while stack:
        if should_stop and should_stop():
            return
        current = stack.pop()
        try:
            entries = list_folder(current)
        except FileNotFoundError:
            if not patient or _device_answers(current):
                continue
            if not _wait_for_device(current, should_stop, waiting):
                if should_stop and should_stop():
                    return
                raise DeviceGone(f"{split_device_path(current)[0]} stopped "
                                 f"answering while {current} was being read") from None
            stack.append(current)
            continue
        for entry in entries:
            if should_stop and should_stop():
                return
            if entry.get("is_folder"):
                stack.append(entry["path"])
            else:
                yield entry


# ---------------------------------------------------------------------------
# Copying off
# ---------------------------------------------------------------------------

def copy_out(text: str, destination: Path | str, *,
             progress: Callable[[int, int, str], None] | None = None,
             should_stop: Callable[[], bool] | None = None,
             waiting: Callable[[str], None] | None = None,
             already_have: Callable[[Path, int | None], bool] | None = None,
             limit: int | None = None,
             preserve_structure: bool = False) -> dict[str, Any]:
    """Copy every file under a device folder into a real folder.

    Returns counts, and ``device_lost`` when the device stopped answering and
    did not come back within :data:`_DEVICE_WAIT` — the files after that point
    were not copied, and the caller must not call the import finished.
    *waiting* is told when the copy starts waiting for the device.
    *already_have* is asked, with where a file would land and its exact size
    if known, whether a file no longer there needs fetching at all.

    Nothing on the device is altered or removed — the shell's copy verb is the
    only one used, and the device is never a destination.
    """
    reason = unavailable_reason()
    if reason:
        raise RuntimeError(reason)
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)

    copied = skipped = failed = 0
    done_bytes = 0
    device_lost = False
    norm_base = str(text or "").strip().strip('"').replace("/", "\\").rstrip("\\")
    try:
        for entry in walk(text, should_stop=should_stop, patient=True,
                          waiting=waiting):
            if should_stop and should_stop():
                break
            if limit is not None and copied >= limit:
                break
            name = entry["name"]
            entry_path = entry["path"]
            if preserve_structure:
                rel_dir = Path()
                if entry_path.lower().startswith(norm_base.lower() + "\\"):
                    sub_rel = entry_path[len(norm_base) + 1:]
                    sub_parts = [p for p in sub_rel.split("\\") if p][:-1]
                    if sub_parts:
                        rel_dir = Path(*sub_parts)
                dest_dir = target / rel_dir
                dest_dir.mkdir(parents=True, exist_ok=True)
                landing = dest_dir / name
            else:
                dest_dir = target
                landing = target / name

            expected = entry.get("exact_size")
            if landing.exists():
                have = landing.stat().st_size
                if have > 0 and (expected is None or have == expected):
                    skipped += 1                 # already brought over
                    continue
                # Cut off part-way last time: fetched again, never archived short.
            elif already_have is not None and already_have(landing, expected):
                skipped += 1                     # brought over and dealt with
                continue

            for _attempt in range(3):
                try:
                    _copy_one(entry_path, dest_dir, name, expected)
                    done_bytes += landing.stat().st_size if landing.exists() else 0
                    copied += 1
                except Exception as exc:                    # noqa: BLE001
                    _discard_partial(landing)
                    if _device_answers(entry_path):
                        log.warning("devices: could not copy %s — %s", entry_path, exc)
                        failed += 1
                    elif _wait_for_device(entry_path, should_stop, waiting):
                        continue                         # the same file, again
                    elif not (should_stop and should_stop()):
                        device_lost = True
                break
            if device_lost:
                break
            if progress:
                progress(copied, done_bytes, name)
    except DeviceGone as exc:
        log.warning("devices: %s", exc)
        device_lost = True
    if device_lost:
        log.warning("devices: stopped copying from %s after %d files: it did "
                    "not come back within %d minutes. The rest are still on it.",
                    text, copied, int(_DEVICE_WAIT // 60))
    return {"copied": copied, "skipped": skipped, "failed": failed,
            "bytes": done_bytes, "destination": str(target),
            "device_lost": device_lost}


def _discard_partial(landing: Path) -> None:
    """Remove what a copy that failed part-way left behind.

    It is Ninaivu's own staging copy, never the photograph, and left in place it
    would be taken for a finished one next time.
    """
    try:
        landing.unlink()
    except OSError:
        pass


def _copy_one(device_path: str, target: Path, name: str,
              expected: int | None = None) -> None:
    """One file, and then wait for it — the shell's copy does not tell you.

    ``CopyHere`` returns immediately and reports nothing at all: no handle, no
    completion, no error. The only way to know a file arrived is to watch for
    it: until it is *expected* bytes long where the device said how long it
    is, and otherwise until it stops growing.
    """
    device, parts = split_device_path(device_path)
    parent_parts = parts[:-1]
    with _com_lock:
        shell = _shell()
        folder = _descend(shell, device, parent_parts)
        if folder is None:
            raise FileNotFoundError(f"{name}: its folder is not on the device any more")
        item = None
        for candidate in folder.Items():
            if str(candidate.Name or "") == name:
                item = candidate
                break
        if item is None:
            raise FileNotFoundError(f"{name} is not on the device any more")
        destination = shell.NameSpace(str(target))
        if destination is None:
            raise OSError(f"{target} could not be opened as a folder")
        destination.CopyHere(item, _COPY_FLAGS)

    landing = target / name
    deadline = time.time() + _FILE_TIMEOUT
    last = -1
    moved_at = time.time()
    while time.time() < deadline:
        if landing.exists():
            size = landing.stat().st_size
            if expected is not None and size == expected:
                return
            if expected is None and size > 0 and size == last:
                return
            if size != last:
                moved_at = time.time()
            last = size
        if time.time() - moved_at > _STALL:
            if not _device_answers(device_path):
                # Locked mid-file. Waiting out the full timeout would only
                # delay waiting for the phone itself.
                raise DeviceGone(f"{name} stopped arriving: the device is not answering")
            moved_at = time.time()          # still there, just slow: ask again later
        time.sleep(_SETTLE)
    raise TimeoutError(f"{name} did not finish copying within "
                       f"{int(_FILE_TIMEOUT)}s")
