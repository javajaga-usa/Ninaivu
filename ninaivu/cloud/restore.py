"""Getting photographs back out of Google Drive.

Uploading was the half that existed. This is the half a backup is for: the
library disk has failed, or a folder was deleted, and the originals are in
Drive. It restores a chosen part of the library, or all of it, and checks every
file on the way.

**Where the list of files comes from.** Normally from Ninaivu's own record of
what went up (:mod:`.store`): each finished upload remembers its Drive id, the
path it came from, its size, a SHA-256 of the bytes that were sent, whether it
was encrypted, and the file's timestamp. That record lives in the index, which
survives a failed *library* disk and comes back with an index backup. On a new
machine with neither, the list comes from walking Ninaivu's folder in Drive
instead; that keeps the two folder levels the upload kept, and checks each file
against the MD5 Drive reports for it.

**What is checked, per file, before it is put anywhere:**

* the downloaded bytes against the SHA-256 recorded when they were sent (or
  Drive's own MD5, when there is no record);
* for an encrypted file, that it names the key being used — a wrong recovery
  file or passphrase is caught before any work, not as a pile of failures —
  and then AES-GCM's own authentication, which fails on a single altered byte;
* the result's size against the size of the original.

Only a file that passes all of them is renamed into place, whole. A download
is staged beside where it is going, so the rename is on one disk, and is
resumed from what is already staged if the restore is stopped part-way.

**Nothing is overwritten.** A file already at the target is taken as already
restored only when it holds the same bytes as the backup: checked against the
backup's checksum, or, for an encrypted backup whose checksum is of the
ciphertext, against the backup itself once downloaded and decrypted. A *different* file there, a damaged copy of the same
photograph included, is left alone and the restored one goes beside it as
``name (restored).ext``. This never deletes anything, locally or in Drive.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from ..utils import source_version as source_version_mod
from ..utils.files import same_bytes, stays_inside
from . import crypto, keyring
from .drive import FOLDER_MIME, DriveClient, DriveError, NeedsReconnect
from .tempfiles import create_new, is_plain_file, open_to_append

log = logging.getLogger(__name__)

__all__ = ["RestoreItem", "RestoreJob", "RestoreState", "from_record",
           "from_drive", "find_params", "all_params", "summarise", "PARAMS_NAME",
           "ENCRYPTED_SUFFIX"]

#: Bytes per ranged download request.
PIECE = 16 * 1024 * 1024

#: The file in Drive that holds the key's salt and settings (not the key).
PARAMS_NAME = "ninaivu-encryption.json"

ENCRYPTED_SUFFIX = ".ninaivu"

#: Where downloads are staged, inside the destination.
STAGING = ".ninaivu-restore"

#: How many times one file is tried against a flaky connection.
TRIES = 4


@dataclass
class RestoreItem:
    """One file to bring back."""

    remote_id: str
    #: Where it goes, relative to the destination. POSIX separators.
    rel_path: str
    #: The library folder it came from, when the record says.
    root: str = ""
    #: The original's size in bytes; 0 when not known.
    size: int = 0
    #: SHA-256 of the bytes in Drive, from the record; "" when not known.
    sha256: str = ""
    #: MD5 of the bytes in Drive, as Drive reports it; "" when not known.
    md5: str = ""
    encrypted: bool = False
    #: The original's modification time, to put back; 0 when not known.
    mtime: float = 0.0
    #: Bytes as stored in Drive (ciphertext for an encrypted file), if known.
    stored_size: int = 0
    #: When Drive last changed the file (its upload), as Drive says; "" when
    #: the list came from the record.
    drive_modified: str = ""
    #: The upload's stamp of the original (size and a digest of both ends;
    #: see utils.source_version), from the record; "" when not known.
    source_version: str = ""


# ---------------------------------------------------------------------------
# Making the list
# ---------------------------------------------------------------------------

def _clean_rel(rel: str) -> str | None:
    """A relative path that stays inside where it is put, or None.

    A colon is an ordinary character in a Mac or Linux name ("Screenshot
    2019-07-01 at 10.30.00" is often written with colons), and refusing it left
    those files out of every restore without a word. Only Windows gives it a
    meaning (a drive, or a hidden stream of another file), so only there is it
    replaced.
    """
    parts = [p for p in PurePosixPath(str(rel).replace("\\", "/")).parts
             if p not in ("", ".", "/")]
    if not parts or any(p == ".." for p in parts):
        return None
    if os.name == "nt":
        parts = [p.replace(":", "_") for p in parts]
    return "/".join(parts)


def from_record(conn, *, roots: Iterable[str] | None = None, folder: str = "",
                asset_ids: Iterable[int] | None = None) -> list[RestoreItem]:
    """Everything Ninaivu recorded as uploaded, narrowed as asked.

    ``roots`` keeps only those library folders; ``folder`` only paths inside
    that folder of the library (``2019`` or ``2019/07``); ``asset_ids`` only
    the files the index knows by those ids.
    """
    from . import store                                     # noqa: PLC0415

    store.init_schema(conn)
    sql = ("SELECT root, rel_path, size, digest, md5, remote_id, encrypted, "
           "source_mtime, source_version FROM cloud_uploads WHERE state=? AND remote_id != ''")
    params: list[Any] = [store.DONE]
    wanted_roots = [str(r) for r in roots or [] if r]
    if wanted_roots:
        sql += f" AND root IN ({','.join('?' * len(wanted_roots))})"
        params += wanted_roots
    prefix = (_clean_rel(folder) or "") if folder else ""
    if prefix:
        sql += " AND (rel_path = ? OR rel_path LIKE ? ESCAPE '\\')"
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params += [prefix, escaped + "/%"]
    ids = [int(i) for i in asset_ids or []]
    if ids:
        sql += (" AND EXISTS (SELECT 1 FROM assets a WHERE a.root = cloud_uploads.root "
                "AND a.rel_path = cloud_uploads.rel_path AND a.id IN "
                f"({','.join('?' * len(ids))}))")
        params += ids
    sql += " ORDER BY root, rel_path"
    # What the household deleted stays deleted: Drive keeps every upload for
    # good, and a restore of everything must not bring those back (recycle.Gone).
    from ..storage.recycle import Gone                      # noqa: PLC0415
    gone = Gone(conn)
    items = []
    for row in conn.execute(sql, params):
        if gone and gone.holds(row["root"], row["rel_path"], int(row["size"] or 0)):
            continue
        rel = _clean_rel(row["rel_path"])
        if rel is None:
            continue
        items.append(RestoreItem(
            remote_id=row["remote_id"], rel_path=rel, root=row["root"],
            size=int(row["size"] or 0), sha256=row["digest"] or "",
            # Drive's own checksum, recorded for an upload that was resumed
            # and so has no SHA-256 of its own.
            md5=row["md5"] or "",
            encrypted=bool(row["encrypted"]),
            mtime=float(row["source_mtime"] or 0),
            source_version=row["source_version"] or ""))
    return items


def from_drive(client: DriveClient, *, folder: str = "",
               on_found: Callable[[int], None] | None = None) -> list[RestoreItem]:
    """Everything in Ninaivu's Drive folder, for a machine with no record.

    Walks the one folder Ninaivu owns and nothing outside it. ``folder`` narrows
    to a sub-folder path (``2019`` or ``2019/07``).
    """
    root_id = client.ninaivu_root()
    items: list[RestoreItem] = []
    queue: list[tuple[str, tuple[str, ...]]] = [(root_id, ())]
    prefix = tuple((_clean_rel(folder) or "").split("/")) if folder else ()
    while queue:
        folder_id, where = queue.pop(0)
        for entry in client.list_folder(folder_id):
            name = str(entry.get("name") or "")
            if not name or "/" in name or "\\" in name or name in (".", ".."):
                continue
            if entry.get("mimeType") == FOLDER_MIME:
                queue.append((str(entry["id"]), (*where, name)))
                continue
            if not where and is_params_name(name):
                continue
            path = (*where, name)
            if prefix and tuple(path[:len(prefix)]) != prefix:
                continue
            encrypted = name.endswith(ENCRYPTED_SUFFIX)
            rel = "/".join((*where, name[:-len(ENCRYPTED_SUFFIX)] if encrypted else name))
            clean = _clean_rel(rel)
            if clean is None:
                continue
            stored = int(entry.get("size") or 0)
            items.append(RestoreItem(
                remote_id=str(entry["id"]), rel_path=clean,
                md5=str(entry.get("md5Checksum") or ""), encrypted=encrypted,
                stored_size=stored, drive_modified=str(entry.get("modifiedTime") or ""),
                size=max(0, stored - crypto.V2_OVERHEAD) if encrypted else stored))
        if on_found:
            on_found(len(items))
    items.sort(key=lambda i: i.rel_path)
    return items


def is_params_name(name: str) -> bool:
    """``ninaivu-encryption.json``, or the one named for a key
    (``ninaivu-encryption-<key id>.json``)."""
    return name == PARAMS_NAME or (name.startswith(PARAMS_NAME[:-5] + "-")
                                   and name.endswith(".json"))


def all_params(client: DriveClient) -> list[dict[str, Any]]:
    """Every key's settings Ninaivu left in its Drive folder; the plain-named
    one first. More than one when a key was made, or imported, later."""
    import json                                             # noqa: PLC0415

    found: list[tuple[bool, dict[str, Any]]] = []
    for entry in client.list_folder(client.ninaivu_root()):
        name = str(entry.get("name") or "")
        if not is_params_name(name) or entry.get("mimeType") == FOLDER_MIME:
            continue
        size = int(entry.get("size") or 0) or 65536
        raw = client.download_range(str(entry["id"]), 0, min(size, 65536) - 1)
        try:
            params = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(params, dict):
            found.append((name != PARAMS_NAME, params))
    return [params for _, params in sorted(found, key=lambda pair: pair[0])]


def find_params(client: DriveClient, key_id: str = "") -> dict[str, Any] | None:
    """The key settings Ninaivu left in its Drive folder, if it left any.

    Salt and scrypt settings, not the key: with the passphrase they make the
    key again, which is how a machine with no recovery file gets it back.
    With *key_id*, only that key's.
    """
    found = all_params(client)
    if key_id:
        return next((p for p in found if str(p.get("key_id") or "") == key_id), None)
    return found[0] if found else None


def summarise(items: list[RestoreItem]) -> dict[str, Any]:
    """What a list amounts to, for the wizard's review step."""
    roots: dict[str, int] = {}
    folders: dict[str, int] = {}
    for item in items:
        roots[item.root] = roots.get(item.root, 0) + 1
        top = item.rel_path.split("/", 1)[0] if "/" in item.rel_path else ""
        folders[top] = folders.get(top, 0) + 1
    return {
        "files": len(items),
        "bytes": sum(i.size or i.stored_size for i in items),
        "encrypted": sum(1 for i in items if i.encrypted),
        "roots": [{"root": r, "files": n} for r, n in sorted(roots.items())],
        "folders": [{"folder": f, "files": n}
                    for f, n in sorted(folders.items(), key=lambda kv: -kv[1])[:40]],
    }


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

@dataclass
class RestoreState:
    """What the console is shown about a restore."""

    running: bool = False
    stopping: bool = False
    finished: bool = False
    started_at: float = 0.0
    ended_at: float = 0.0
    destination: str = ""
    total: int = 0
    total_bytes: int = 0
    restored: int = 0
    already_there: int = 0
    beside: int = 0
    failed: int = 0
    bytes_done: int = 0
    current: str = ""
    message: str = ""
    problems: list[dict[str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def bump(self, **fields: int) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, getattr(self, key) + value)

    def problem(self, rel: str, why: str) -> None:
        with self._lock:
            self.failed += 1
            if len(self.problems) < 200:
                self.problems.append({"file": rel, "why": why})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
            data["problems"] = list(self.problems)
        done = data["restored"] + data["already_there"] + data["beside"] + data["failed"]
        data["processed"] = done
        data["percent"] = (100 if data["finished"] else
                           int(99 * done / data["total"]) if data["total"] else 0)
        return data


class RestoreJob:
    """Brings a list of files back from Drive, one at a time, on one thread."""

    def __init__(self, *, connect: Callable[[], DriveClient],
                 items: list[RestoreItem], destination: Path | str | None,
                 key: bytes | None = None,
                 on_done: Callable[["RestoreJob"], None] | None = None):
        """``destination`` None puts each file back where it came from, which
        needs every item to know its library folder."""
        self._connect = connect
        self.items = items
        self.destination = Path(destination) if destination else None
        self.key = key
        self._on_done = on_done
        self.state = RestoreState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Library folders written into, for the index to pick up afterwards.
        self.touched_roots: set[str] = set()
        self._bases: set[Path] = set()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.state.update(
            running=True, finished=False, started_at=time.time(),
            destination=str(self.destination or "their original folders"),
            total=len(self.items),
            total_bytes=sum(i.size or i.stored_size for i in self.items),
            message="Starting…")
        self._thread = threading.Thread(target=self._run, name="ninaivu-restore",
                                        daemon=True)
        self._thread.start()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        self.state.update(stopping=True)
        if join and self._thread:
            self._thread.join(30)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # -- the loop ---------------------------------------------------------

    def _run(self) -> None:
        try:
            client = self._connect()
            for item in self.items:
                if self._stop.is_set():
                    break
                self.state.update(current=item.rel_path)
                try:
                    self._one(client, item)
                except _Stopped:
                    break
                except NeedsReconnect as exc:
                    self.state.update(message=str(exc))
                    self.state.problem(item.rel_path, str(exc))
                    break
                except DriveError as exc:
                    if not exc.account_wide:
                        log.warning("could not restore %s: %s", item.rel_path, exc)
                        self.state.problem(item.rel_path, str(exc))
                        continue
                    # Google out of reach, or the account told to slow down: not
                    # this file's fault, and every file after it would be refused
                    # the same way. Counted as a failure each, a dropped connection
                    # turned the rest of a restore into "could not be restored".
                    log.warning("restore from Google Drive paused: %s", exc)
                    self.state.update(message=(
                        f"Google Drive could not be used just now ({exc}). Start the "
                        f"restore again to carry on where it stopped."))
                    break
                except (OSError, ValueError, RuntimeError) as exc:
                    log.warning("could not restore %s: %s", item.rel_path, exc)
                    self.state.problem(item.rel_path, str(exc))
            else:
                self.state.update(finished=True)
        except Exception as exc:                            # noqa: BLE001
            log.exception("the restore stopped")
            self.state.update(message=f"The restore stopped: {exc}")
        finally:
            self._tidy()
            snap = self.state.snapshot()
            if snap["finished"]:
                message = (f"Restored {snap['restored']:,} files"
                           + (f", {snap['already_there']:,} were already there"
                              if snap["already_there"] else "")
                           + (f", {snap['beside']:,} put beside a different file"
                              if snap["beside"] else "")
                           + (f"; {snap['failed']:,} could not be restored"
                              if snap["failed"] else "") + ".")
            elif self._stop.is_set():
                message = ("Stopped. Starting the same restore again carries on "
                           "where this one stopped.")
            else:
                message = snap["message"] or "The restore stopped."
            self.state.update(running=False, stopping=False, current="",
                              ended_at=time.time(), message=message)
            log.info("restore from Google Drive: %s", message)
            if self._on_done is not None:
                try:
                    self._on_done(self)
                except Exception:                           # noqa: BLE001
                    log.exception("could not finish up after the restore")

    def _tidy(self) -> None:
        """Remove staging folders that are empty. One holding a partly
        downloaded file is kept, so starting again carries on from it."""
        for base in self._bases:
            try:
                (base / STAGING).rmdir()
            except OSError:
                pass

    # -- one file ---------------------------------------------------------

    def _base_for(self, item: RestoreItem) -> Path:
        if self.destination is not None:
            return self.destination
        if not item.root:
            raise ValueError("the record does not say which library folder this "
                             "came from; choose a folder to restore into")
        return Path(item.root)

    def _one(self, client: DriveClient, item: RestoreItem) -> None:
        base = self._base_for(item)
        target = base.joinpath(*item.rel_path.split("/"))
        # A path the list says is relative must stay relative. Checked on the
        # joined path as well as when the list was made, with links followed:
        # a folder in the destination that is a link to somewhere else would
        # otherwise carry the restored file out of the destination.
        if not stays_inside(base, target):
            raise ValueError("refusing a path outside the destination")
        staging = base / STAGING
        if not stays_inside(base, staging):
            raise ValueError("refusing a staging folder outside the destination")

        beside = False
        #: Same-size files there that could not be checked without the backup
        #: itself: compared with it once it is downloaded.
        unchecked: list[Path] = []
        if target.exists() or target.is_symlink():
            # Already restored — by an earlier run of this restore, beside a
            # different file or in its own place — is not restored twice. The
            # size alone is not enough where the backup's checksum is known: a
            # damaged photograph is usually still the size it was, and the
            # restore that exists to bring it back used to call it "already
            # there". Where the record has no checksum (an upload that was
            # resumed, before Drive's was kept), Drive is asked for its own,
            # so a same-size damaged file is still told apart.
            if not item.encrypted and not (item.sha256 or item.md5):
                self._drive_checksum(client, item)
            # An encrypted backup's checksums are of the ciphertext, so they
            # say nothing about the file on disk. That file used to be taken
            # as restored on its size alone, which is exactly how a damaged
            # photograph looks; now the backup is fetched, decrypted and
            # compared byte for byte below.
            for earlier in (target, *_earlier_beside(target)):
                if (item.size and earlier.is_file() and not earlier.is_symlink()
                        and earlier.stat().st_size == item.size):
                    verdict = _matches_backup(earlier, item)
                    if verdict is None and _same_as_sent(earlier, item):
                        verdict = True
                    if verdict:
                        self.state.bump(already_there=1, bytes_done=item.size)
                        return
                    if verdict is None:
                        unchecked.append(earlier)
            target = _beside(target)
            beside = True

        self._bases.add(base)
        staging.mkdir(parents=True, exist_ok=True)
        part = staging / f"{_safe_name(item.remote_id)}.part"
        self._download(client, item, part)

        if item.encrypted:
            if self.key is None:
                raise ValueError("this file is encrypted and no key was given")
            found = crypto.read_key_id(part)
            if found is None:
                raise ValueError("this is not a Ninaivu encrypted file")
            if found != keyring.key_id(self.key):
                raise ValueError("this file was encrypted with a different key")
            plain = staging / f"{_safe_name(item.remote_id)}.plain"
            crypto.decrypt_file_v2(part, plain, self.key)
            part.unlink(missing_ok=True)
            ready = plain
        else:
            ready = part

        size = ready.stat().st_size
        if item.size and size != item.size:
            ready.unlink(missing_ok=True)
            raise ValueError(f"it came back as {size:,} bytes, not the "
                             f"{item.size:,} that were backed up")

        for earlier in unchecked:
            if same_bytes(ready, earlier):
                ready.unlink(missing_ok=True)
                self.state.bump(already_there=1, bytes_done=size)
                return

        target.parent.mkdir(parents=True, exist_ok=True)
        # Checked again now the folders are made: a link put in their place
        # since the first check is not followed out of the destination.
        if not stays_inside(base, target):
            ready.unlink(missing_ok=True)
            raise ValueError("refusing a path outside the destination")
        if item.mtime:
            os.utime(ready, (item.mtime, item.mtime))
        _publish(ready, target)
        if self.destination is None and item.root:
            self.touched_roots.add(item.root)
        self.state.bump(beside=1 if beside else 0, restored=0 if beside else 1,
                        bytes_done=size)

    def _download(self, client: DriveClient, item: RestoreItem, part: Path) -> None:
        """Fetch the stored bytes into *part*, checking them against the record.

        Resumes from what is already in *part*; the check re-reads that much
        from disk first, so a resumed file is checked as a whole.
        """
        total = item.stored_size
        if not total or not (item.sha256 or item.md5):
            # Drive says how big the stored file is and what its MD5 is. The
            # size was always read here; the checksum was dropped, which left
            # a file with no SHA-256 in the record — an upload that had been
            # resumed — checked by nothing but its length.
            info = self._drive_checksum(client, item)
            total = total or int(info.get("size") or 0)
        sha = hashlib.sha256()
        md5 = hashlib.md5()                                  # noqa: S324 — Drive's checksum
        offset = 0
        if part.is_symlink():
            # Never carried on with, and never written through: a link at the
            # staged name would send the download out of the destination.
            part.unlink()
            raise ValueError("a link had been put where the download is staged; "
                             "it was removed, and the file was not restored")
        if part.exists():
            if not is_plain_file(part) or part.stat().st_size > total:
                part.unlink()
            else:
                with open(part, "rb") as handle:
                    while chunk := handle.read(4 * 1024 * 1024):
                        sha.update(chunk)
                        md5.update(chunk)
                        offset += len(chunk)
        with (open_to_append(part) if offset else create_new(part)) as out:
            while offset < total:
                if self._stop.is_set():
                    raise _Stopped()
                end = min(total, offset + PIECE) - 1
                piece = self._fetch(client, item.remote_id, offset, end)
                if not piece:
                    break
                out.write(piece)
                sha.update(piece)
                md5.update(piece)
                offset += len(piece)
        if offset != total:
            part.unlink(missing_ok=True)
            raise ValueError(f"only {offset:,} of {total:,} bytes arrived")
        if item.sha256 and sha.hexdigest() != item.sha256:
            part.unlink(missing_ok=True)
            raise ValueError("what came back from Drive does not match what "
                             "was sent (SHA-256 differs)")
        if not item.sha256 and item.md5 and md5.hexdigest() != item.md5:
            part.unlink(missing_ok=True)
            raise ValueError("what came back from Drive does not match "
                             "Drive's own checksum")

    def _drive_checksum(self, client: DriveClient, item: RestoreItem) -> dict[str, Any]:
        """Ask Drive about *item*'s stored file, and keep its MD5 on the item.

        Only fills a gap: a SHA-256 or MD5 already known is never replaced by
        what Drive says now, because that is the one being checked against.
        """
        info = client.file_info(item.remote_id)
        if not (item.sha256 or item.md5):
            item.md5 = str(info.get("md5Checksum") or "")
        if not item.stored_size:
            item.stored_size = int(info.get("size") or 0)
        return info

    def _fetch(self, client: DriveClient, remote_id: str, start: int, end: int) -> bytes:
        return fetch_range(client, remote_id, start, end, stop=self._stop)


def fetch_range(client: DriveClient, remote_id: str, start: int, end: int, *,
                stop: threading.Event | None = None) -> bytes:
    """One range of a file, tried again with a growing wait when Google says so.

    Shared by the restore and by reading the copy of the index back, which
    used to give up a download of hundreds of megabytes on one slow answer.
    """
    delay = 1.0
    for attempt in range(TRIES):
        try:
            return client.download_range(remote_id, start, end)
        except NeedsReconnect:
            raise
        except DriveError as exc:
            if not exc.retryable or attempt == TRIES - 1:
                raise
            if stop is not None:
                stop.wait(delay)
                if stop.is_set():
                    raise _Stopped() from exc
            else:
                time.sleep(delay)
            delay *= 2
    return b""


class _Stopped(RuntimeError):
    """Stop was pressed mid-file. The staged bytes are kept for next time."""

    def __init__(self) -> None:
        super().__init__("stopped")


def _matches_backup(path: Path, item: RestoreItem) -> bool | None:
    """Whether the file at *path* holds the bytes that were backed up.

    None when there is no way to tell here. An encrypted file's checksums are
    of what is in Drive — ciphertext — so they say nothing about a file on
    disk, and only its size can be compared.
    """
    if item.encrypted or not (item.sha256 or item.md5):
        return None
    digest = hashlib.sha256() if item.sha256 else hashlib.md5()  # noqa: S324
    try:
        with open(path, "rb") as source:
            while chunk := source.read(4 * 1024 * 1024):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == (item.sha256 or item.md5)


def _same_as_sent(path: Path, item: RestoreItem) -> bool:
    """Whether the file at *path* is the original that was sent, told from
    the record of it rather than by fetching the backup.

    For an encrypted backup, whose checksums say nothing about the file on
    disk. A file with the size and timestamp the original had, and the same
    bytes at both ends as when it was sent, is taken as that file; fetching
    and decrypting each one to be sure made a restore over a library that is
    still there download all of it.
    """
    if not (item.encrypted and item.source_version and item.mtime):
        return False
    try:
        if abs(path.stat().st_mtime - item.mtime) > 1e-3:
            return False
    except OSError:
        return False
    return source_version_mod.same_content(item.source_version, path) is True


def _safe_name(remote_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in remote_id)[:120] or "file"


def _earlier_beside(target: Path) -> list[Path]:
    """Copies an earlier restore put beside *target*."""
    # Escaped: in "IMG [1].jpg" the brackets are a glob character class, which
    # matched nothing, so each run of the same restore added another copy.
    return sorted(target.parent.glob(
        f"{glob.escape(target.stem)} (restored*){glob.escape(target.suffix)}"))


def _beside(target: Path) -> Path:
    """``name (restored).ext``, or ``name (restored 2).ext``, not yet taken."""
    for n in range(1, 10_000):
        label = "restored" if n == 1 else f"restored {n}"
        candidate = target.with_name(f"{target.stem} ({label}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"no free name beside {target}")


def _publish(ready: Path, target: Path) -> None:
    """Put the checked file in place, whole, without replacing anything."""
    try:
        # A hard link never replaces an existing name, which a rename on POSIX
        # would; the staged copy is then dropped.
        os.link(ready, target)
        ready.unlink()
    except FileExistsError:
        raise
    except OSError:
        # exFAT and FAT32 have no hard links. The target was checked a moment
        # ago; this is the one window where it is not atomic.
        if target.exists():
            raise FileExistsError(target) from None
        shutil.move(str(ready), str(target))
