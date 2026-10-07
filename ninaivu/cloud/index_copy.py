"""A copy of the index in Google Drive, so a lost computer loses nothing but time.

The cloud backup has always held the photographs and nothing else. The rest —
who is in which photograph, the albums, what each person may see, corrected
dates, where each file lived — is in the index, and the index lived on this
computer and in local backup bundles beside it. A house fire, a stolen laptop
or a dead system disk left a household with every photograph back and none of
the work on top of them, and a rebuild from Drive that could only guess the
folders two levels deep.

So a copy of the index goes to Drive as well: into a folder of its own inside
Ninaivu's folder, in the same bundle format the local backups use, so the same
restore tool puts it back. It is *not* the local bundle, because the local
bundle holds things that must never sit in Drive:

* **the cloud encryption key** — beside the files it encrypts it would make
  the encryption worthless;
* **the Google sign-in**, the **certificate authority's private key**, and
  any **password, token or webhook** in the settings;
* **sign-in sessions**, which are keys to the running server;
* the **sign-in record and counters** (the audit table and ``auth_limits``,
  whose keys can hold a whole share link) and the saved details of what is
  in the bin.

It also leaves out what Ninaivu rebuilds by itself — the image-search vectors,
roughly half the index — and marks every photograph for re-analysis, so a
restored library searches again after one scan. What is left is what cannot
be rebuilt: faces and who they are, albums, visibility, dates, captions,
ratings, and the record of every uploaded file with its full path and
checksum, which is what lets a new computer restore the library exactly.

It is encrypted with the cloud key, and only ever sent encrypted: with cloud
encryption off there is no copy in Drive at all, since even slimmed it holds
every file name, face, place and the scrambled passwords. It is kept in two slots
updated in turn, never more: Ninaivu deletes nothing in Drive, and a copy added
every day would pile up for ever, while updating one slot alone would mean a
bad update replaced the only good copy.
"""

from __future__ import annotations

from .. import __version__

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..utils.files import sha256_file
from . import crypto, limits, store
from .drive import FOLDER_MIME, DriveClient

log = logging.getLogger(__name__)

__all__ = ["IndexCopy", "make_bundle", "find", "fetch", "FOLDER", "SLOTS", "KIND",
           "keep_local_secrets"]

#: The folder inside Ninaivu's Drive folder the copies live in.
FOLDER = "Ninaivu index"
#: The two slots, updated in turn. An encrypted copy adds ``.ninaivu``.
SLOTS = ("ninaivu-index-a", "ninaivu-index-b")
BUNDLE = ".tar.gz"

#: Settings whose values are secrets, and are blanked in the copy.
_SECRET = re.compile(r"password|secret|token|api_key|apikey|webhook|credential",
                     re.IGNORECASE)

#: Where the last upload is recorded: a file beside the index rather than a
#: row in it, so writing the record down is not itself a change to the index
#: that the next day's look would take for one.
RECORD = "cloud-index-copy.json"

#: Bytes per download request when reading a copy back.
PIECE = 16 * 1024 * 1024

#: The ``kind`` in a bundle's manifest that says it is this copy, not a full
#: local backup: a restore of it must keep what it does not carry.
KIND = "cloud-index-copy"


# ---------------------------------------------------------------------------
# Making the copy
# ---------------------------------------------------------------------------

def _slim(index: Path) -> None:
    """Take out what must not travel, and what Ninaivu rebuilds by itself."""
    with closing(sqlite3.connect(str(index))) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" in tables:
            conn.execute("DELETE FROM sessions")
        if "shares" in tables:
            # A share link's token *is* the link: anyone holding the copy could
            # open every album the household ever shared. After a restore the
            # links are made again from the album.
            conn.execute("DELETE FROM shares")
        if "embeddings" in tables:
            conn.execute("DELETE FROM embeddings")
            if "assets" in tables:
                # No vectors, so every photograph is analysed again after a
                # restore — which is what brings searching by description back.
                conn.execute("UPDATE assets SET ai_version=0")
        if "audit" in tables:
            # Who signed in when, and every name somebody mistyped at the
            # sign-in page. A record for this machine's administrator, not for
            # whoever ends up holding a copy of the index.
            conn.execute("DELETE FROM audit")
        if "auth_limits" in tables:
            # The sign-in counters are keyed by what was tried, and a wrong
            # share link is tried with the whole link: "*|share:<the link>".
            conn.execute("DELETE FROM auth_limits")
        if "recycled" in tables and "metadata" in {
                r[1] for r in conn.execute("PRAGMA table_info(recycled)")}:
            # The faces, places and captions of what is in the bin, kept so a
            # restore from the bin brings them back. A restore of the index
            # puts the bin's files back without them: rebuilt on the next look.
            conn.execute("UPDATE recycled SET metadata=NULL")
        if "cloud_uploads" in tables:
            conn.execute("UPDATE cloud_uploads SET resume_url=''")
        if "pending_uploads" in tables:
            # Their bytes stay on this computer (they are not in the library
            # yet), and a row without its file would fail the bundle's check.
            conn.execute("DELETE FROM pending_uploads WHERE status='pending'")
        conn.commit()
        conn.execute("VACUUM")


def _clean_settings(source: Path, target: Path) -> None:
    try:
        settings = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(settings, dict):
        return
    for key in list(settings):
        if _SECRET.search(key):
            settings[key] = ""
    target.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def keep_local_secrets(copied: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """The copy's settings, with the secrets it blanked put back from *local*.

    The copy in Drive carries the settings with every password, token and
    webhook emptied (:func:`_clean_settings`). Restoring it over this
    machine's own settings used to empty them here too: the mail password,
    the notification webhook, all gone because the index came back. A blank
    secret in the copy means "not carried", not "none", so this machine's
    value stands; a secret the copy does carry, and every other setting, is
    the copy's.
    """
    merged = dict(copied)
    for key, value in local.items():
        if _SECRET.search(str(key)) and merged.get(key, "") in ("", None) and value:
            merged[key] = value
    return merged


def make_bundle(state_dir: Path | str, out_dir: Path | str) -> Path:
    """Write the Drive copy of the index into *out_dir*. Returns its path.

    The same format as a local backup bundle (ninaivu/storage/backup.py), so
    ``tools/backup_restore.py restore`` puts it back; a restore of it keeps
    this machine's own key, sign-in and certificates, because it carries none.
    """
    from ..storage import backup                               # noqa: PLC0415

    state_dir, out_dir = Path(state_dir), Path(out_dir)
    if not (state_dir / "index.db").is_file():
        raise FileNotFoundError("the library index is missing")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ninaivu_index_", dir=out_dir) as tmp:
        staged = Path(tmp) / "state"
        staged.mkdir()
        for name in backup.DATABASES:
            source = state_dir / name
            if source.is_file() and not backup.hot_copy(source, staged / name):
                raise OSError(f"{name} could not be copied")
        _slim(staged / "index.db")
        if (state_dir / "config.json").is_file():
            _clean_settings(state_dir / "config.json", staged / "config.json")
        if (state_dir / "library-path").is_file():
            shutil.copy2(state_dir / "library-path", staged / "library-path")
        if (state_dir / "avatars").is_dir():
            shutil.copytree(state_dir / "avatars", staged / "avatars")

        manifest: dict[str, Any] = {
            # Which Ninaivu made it — read by nobody yet, but a person
            # looking at a bundle years on deserves the true answer.
            "version": __version__,
            "created_at": datetime.now().isoformat(),
            "source_state_dir": str(state_dir),
            "kind": KIND,
            "files": {},
        }
        for path in staged.rglob("*"):
            if path.is_file():
                manifest["files"][path.relative_to(staged).as_posix()] = {
                    "size": path.stat().st_size, "sha256": sha256_file(path)}
        (staged / "backup_manifest.json").write_text(json.dumps(manifest, indent=2),
                                                     encoding="utf-8")
        backup.verify_state(staged)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bundle = out_dir / f"{backup.PREFIX}{stamp}_from_drive{BUNDLE}"
        with tarfile.open(bundle, "w:gz") as tar:
            tar.add(staged, arcname="state")
    return bundle


# ---------------------------------------------------------------------------
# Keeping it in Drive
# ---------------------------------------------------------------------------

def _slot_of(name: str) -> str | None:
    return next((slot for slot in SLOTS if name.startswith(slot)), None)


def _modified(entry: dict[str, Any]) -> str:
    return str(entry.get("modifiedTime") or "")


def _choose_slot(found: dict[str, dict[str, Any]], last: dict[str, Any]) -> str:
    """The slot to write: never the one holding the newest copy.

    An empty slot first. With both full, the one this machine did not write
    last time — but only when the record of last time is borne out by what
    is in Drive (the slot it names holds the file it sent). With no record, or
    one Drive does not agree with — a new machine, a state folder restored
    from elsewhere — the older of the two by Drive's own timestamp. Always
    writing slot "a" there overwrote whichever copy happened to be in it,
    newest or not.
    """
    free = [slot for slot in SLOTS if slot not in found]
    if free:
        return free[0]
    written = str(last.get("slot") or "")
    if written in found and str(found[written].get("id")) == str(last.get("remote_id") or ""):
        return next(s for s in SLOTS if s != written)
    return min(SLOTS, key=lambda s: (_modified(found[s]), s))


class IndexCopy:
    """Uploads the index copy on a schedule, and says how it went."""

    def __init__(self, cfg, service, *, connect_db: Callable[[], sqlite3.Connection],
                 hold: Callable[[], str | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.service = service
        self._connect = connect_db
        self._hold = hold
        self._clock = clock
        self._running = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self.error = ""

    @property
    def every_hours(self) -> float:
        return max(0.0, float(getattr(self.cfg, "cloud_index_every_hours", 24) or 0))

    @property
    def running(self) -> bool:
        return self._running.locked()

    # -- schedule ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-index-copy",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in (self._thread, self._worker):
            if thread and thread.is_alive():
                thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(1800):
            try:
                if self.due():
                    self.run()
            except Exception:                                    # noqa: BLE001
                log.exception("the copy of the index could not be sent to Drive")

    def last(self) -> dict[str, Any]:
        try:
            found = json.loads((Path(self.cfg.state_dir) / RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def _stamp(self) -> list[float]:
        """When the index files last changed, as the disk says."""
        index = Path(self.cfg.state_dir) / "index.db"
        stamps = []
        for path in (index, index.with_name("index.db-wal")):
            try:
                stamps.append(path.stat().st_mtime)
            except OSError:
                stamps.append(0.0)
        return stamps

    def due(self) -> bool:
        if not self.every_hours or self.running:
            return False
        if not getattr(self.cfg, "cloud_enabled", False) or not self.service.creds.connected:
            return False
        if not getattr(self.cfg, "cloud_encrypt", False):
            # Never sent unencrypted (see _send). Said here rather than tried
            # and refused every half hour, and shown where the copy's state is.
            self.error = ("the copy of the index is only sent encrypted; switch on "
                          "encryption on the Mugil page")
            return False
        if self._hold is not None and self._hold():
            return False
        if not self.service.window().is_open():
            return False                    # the hours set for uploading
        last = self.last()
        if self._clock() - float(last.get("at") or 0) < self.every_hours * 3600:
            return False
        # Changed since the copy went — compared with the index's own stamp
        # taken just after recording it, so the record is not itself a change.
        return self._stamp() != list(last.get("stamp") or [])

    def run_in_background(self) -> bool:
        if self.running:
            return False
        self._worker = threading.Thread(target=self.run, name="ninaivu-index-copy-now",
                                        daemon=True)
        self._worker.start()
        return True

    # -- one upload ----------------------------------------------------------

    def run(self) -> dict[str, Any]:
        if not self._running.acquire(blocking=False):
            return {"ok": False, "error": "already sending a copy"}
        work = Path(self.cfg.state_dir) / "cloud-index"
        try:
            result = self._send(work)
            self.error = ""
            return result
        except Exception as exc:                                  # noqa: BLE001
            self.error = str(exc)
            log.warning("the copy of the index could not be sent to Drive: %s", exc)
            return {"ok": False, "error": str(exc)}
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self._running.release()

    def _uploaded_here(self) -> bool:
        """Whether this machine's index records anything as backed up."""
        try:
            conn = self._connect()
            return store.has_uploaded(conn)
        except Exception:                                         # noqa: BLE001
            # Not knowing is not permission to replace the copy in Drive.
            log.debug("could not read the upload record", exc_info=True)
            return False

    def _send(self, work: Path) -> dict[str, Any]:
        if not self.service.creds.connected:
            raise RuntimeError("no Google account is connected")
        # Refuses rather than sends in the clear when encryption is on and the
        # key is missing — the same rule the photographs follow.
        encryption = self.service._encryption()                  # noqa: SLF001
        if encryption is None:
            # And with encryption off, it is not sent at all. Even slimmed, the
            # index is every folder and file name in the house (the hidden ones
            # too), every named face, where each photograph was taken and where
            # home is, and the scrambled form of every password and PIN — which
            # can be guessed at, offline, by anybody holding a copy. Photographs
            # in Drive unencrypted are the household's choice; this would be a
            # map of the whole house handed over with them.
            raise RuntimeError(
                "the copy of the index is only sent encrypted; switch on "
                "encryption on the Mugil page")
        started = self._clock()

        # What is in Drive already, and which slot to write, is settled before
        # the bundle is made: a copy that is not going to be sent is not worth
        # the minutes of making.
        client: DriveClient = self.service.client()
        folder = client.ensure_folder(FOLDER, client.ninaivu_root())
        found = {}
        for entry in client.list_folder(folder):
            slot = _slot_of(str(entry.get("name") or ""))
            if slot and entry.get("mimeType") != FOLDER_MIME:
                if slot not in found or _modified(entry) > _modified(found[slot]):
                    found[slot] = entry
        last = self.last()
        ours = bool(last.get("remote_id")) and any(
            str(entry.get("id")) == str(last["remote_id"]) for entry in found.values())
        if found and not ours and not self._uploaded_here():
            # A computer that has never backed anything up, looking at copies
            # it did not make: a new machine, set up before its index was
            # restored. Its index is all but empty, and sending it would put
            # that over a copy that holds the household's faces, albums and
            # the record of every uploaded file — which the restore wizard on
            # this very machine would then read. Nothing is sent until this
            # machine has uploaded something itself, or the index has been
            # restored (which brings the record of uploads back with it).
            raise RuntimeError(
                "a copy of the index from another computer is already in Drive, and "
                "this one has not backed anything up yet; it is kept until this "
                "computer has (or its index has been restored from it)")
        slot = _choose_slot(found, last)

        bundle = make_bundle(self.cfg.state_dir, work)
        send = bundle
        if encryption is not None:
            send = bundle.with_name(bundle.name + ".ninaivu")
            crypto.encrypt_file_v2(bundle, send, *encryption)
            bundle.unlink()
        size = send.stat().st_size
        digest = hashlib.sha256()
        with open(send, "rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)

        name = slot + BUNDLE + (".ninaivu" if encryption is not None else "")
        existing = found.get(slot)
        # The same speed limit the photographs keep to.
        pace = limits.RateLimiter(int(getattr(self.cfg, "cloud_rate_kbps", 0) or 0) * 1024,
                                  stop=self._stop)

        def gate(count: int) -> None:
            if self._stop.is_set():
                raise limits.UploadPaused("Ninaivu is stopping")
            pace.take(count)

        if existing is not None:
            url = client.begin_update(str(existing["id"]), size, name=name)
            remote = client.upload_file(send, name, folder, resume_url=url, gate=gate)
        else:
            remote = client.upload_file(send, name, folder, gate=gate)
        record = {"at": self._clock(), "took": round(self._clock() - started, 1),
                  "size": size, "slot": slot, "name": name, "remote_id": remote,
                  "encrypted": encryption is not None, "sha256": digest.hexdigest()}
        record["stamp"] = self._stamp()
        target = Path(self.cfg.state_dir) / RECORD
        pending = target.with_suffix(".tmp")
        pending.write_text(json.dumps(record), encoding="utf-8")
        pending.replace(target)
        log.info("sent a copy of the index to Drive: %s, %.1f MB%s", name,
                 size / 1048576, ", encrypted" if encryption is not None else "")
        return {"ok": True, **record}

    def status(self) -> dict[str, Any]:
        last = self.last()
        return {"every_hours": self.every_hours, "running": self.running,
                "error": self.error, "last": last or None}


# ---------------------------------------------------------------------------
# Reading it back, on a computer that has nothing else
# ---------------------------------------------------------------------------

def find(client: DriveClient) -> list[dict[str, Any]]:
    """The index copies in Drive, newest first. Empty when there are none."""
    root = client.ninaivu_root()
    folder = client._find_folder(FOLDER, root)                    # noqa: SLF001
    if not folder:
        return []
    copies = [e for e in client.list_folder(folder)
              if _slot_of(str(e.get("name") or "")) and e.get("mimeType") != FOLDER_MIME]
    return sorted(copies, key=_modified, reverse=True)


def fetch(client: DriveClient, entry: dict[str, Any], key: bytes | None,
          work: Path) -> tuple[Path, Path]:
    """Download one copy, check it, decrypt it, unpack it.

    Returns ``(state folder, bundle)``: the unpacked index to read, and the
    bundle itself, which ``tools/backup_restore.py restore`` puts back.

    ValueError when it is encrypted and there is no key, or it fails a check.
    """
    from ..storage import backup                                  # noqa: PLC0415

    work.mkdir(parents=True, exist_ok=True)
    name = str(entry.get("name") or "copy")
    encrypted = name.endswith(".ninaivu")
    if encrypted and key is None:
        raise NeedsKey("The copy of the index in Drive is encrypted. Give the recovery "
                       "file, or the passphrase the key was made with.")
    raw = work / ("download.ninaivu" if encrypted else "download.tar.gz")
    total = int(entry.get("size") or 0) or int(client.file_info(str(entry["id"])).get("size") or 0)
    md5 = hashlib.md5()                                           # noqa: S324 — Drive's own checksum
    with open(raw, "wb") as out:
        offset = 0
        while offset < total:
            piece = client.download_range(str(entry["id"]), offset,
                                          min(total, offset + PIECE) - 1)
            if not piece:
                break
            out.write(piece)
            md5.update(piece)
            offset += len(piece)
    if offset != total:
        raise ValueError(f"only {offset:,} of {total:,} bytes of the index copy arrived")
    if entry.get("md5Checksum") and md5.hexdigest() != entry["md5Checksum"]:
        raise ValueError("the index copy did not arrive intact (Drive's checksum differs)")
    bundle = raw
    if encrypted:
        found = crypto.read_key_id(raw)
        from . import keyring                                     # noqa: PLC0415
        if found is None or found != keyring.key_id(key):
            raise ValueError("The index copy was encrypted with a different key.")
        bundle = work / "index-copy.tar.gz"
        crypto.decrypt_file_v2(raw, bundle, key)
        raw.unlink(missing_ok=True)
    state = backup.extract(bundle, work / "unpacked")
    backup.verify_state(state)
    return state, bundle


class NeedsKey(ValueError):
    """The copy is encrypted and no key was given."""
