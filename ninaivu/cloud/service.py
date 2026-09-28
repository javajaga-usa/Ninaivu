"""The part of the cloud sync the rest of Ninaivu talks to.

:mod:`.store` remembers, :mod:`.drive` speaks the protocol and :mod:`.engine`
does the sending. This holds the three together and gives the console one
object to ask: where are the credentials kept, what is the queue, start,
pause, connect, disconnect.

Two decisions are made here rather than in the layers below, because they are
about *this application* rather than about Drive.

**What gets queued.** The index is the source of truth for what the library
contains, so a sync run is "walk the index, offer each file to the record,
keep the ones it has never seen". Items in the recycle bin are not offered.
Hidden items are not offered either, unless the setting says otherwise — and
the engine checks again at the moment of sending, because visibility can
change while a queue drains and the later answer is the right one.

**Where the tokens live.** In one file in the state directory, owner-only,
never in the database and never in a response. The console is shown
:meth:`Credentials.public`, which cannot carry a token even by accident.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from . import index_copy, keyring, limits, restore, store
from .drive import Credentials, DriveClient, consent_url, new_state
from .engine import SyncEngine
from .upload_cache import UploadCache

#: How a running upload is remembered across a restart (storage/resume.py).
RESUME_NAME = "cloud"

log = logging.getLogger(__name__)

__all__ = ["CloudService"]

#: How long a half-finished sign-in stays valid. Long enough to read Google's
#: consent screen properly, short enough that an abandoned attempt does not
#: leave a usable slot open all week.
STATE_TTL = 15 * 60


class CloudService:
    """One per running Ninaivu. Owns the credentials file and the engine."""

    def __init__(self, cfg, connect_db, *, idle: Callable[[], bool] | None = None,
                 hold: Callable[[], str | None] | None = None):
        self.cfg = cfg
        self._idle = idle
        #: Why the upload should wait for the household right now, if it should
        #: (ninaivu/server/workload.py).
        self._hold = hold
        self._connect_db = connect_db
        self._lock = threading.Lock()
        self.creds = Credentials.load(self.creds_path)
        if not self.creds.folder_name:
            self.creds.folder_name = cfg.cloud_folder_name or "Ninaivu"
        self._pending_state: tuple[str, float] | None = None
        self._engine: SyncEngine | None = None
        self._last_finished: dict[str, Any] = {}
        #: The library scanner, when there is one (set by Services).
        self.scanner = None
        self._restore: restore.RestoreJob | None = None
        self._restore_resumes_upload = False

    # -- where things are kept -------------------------------------------

    @property
    def creds_path(self) -> Path:
        return Path(self.cfg.state_dir) / "google.json"

    def _save(self, creds: Credentials | None = None) -> None:
        if creds is not None:
            self.creds = creds
        self.creds.save(self.creds_path)

    # -- the client and the engine ---------------------------------------

    def client(self) -> DriveClient:
        return DriveClient(creds=self.creds, on_change=lambda c: self._save(c))

    def engine(self) -> SyncEngine:
        with self._lock:
            if self._engine is None:
                self._engine = SyncEngine(
                    connect=self.client,
                    open_db=lambda: self._connect_db(),
                    visibility_of=self._visibility_of,
                    awaiting_check=self._awaiting_check,
                    requeue=self._queue_for_the_engine,
                    rate_kbps=int(self.cfg.cloud_rate_kbps or 0),
                    window=self.window(),
                    encryption=self._encryption,
                    cache=UploadCache(Path(self.cfg.state_dir) / "cloud-upload-cache"),
                    publish_params=self._publish_params,
                    on_finished=self._upload_finished,
                    idle=self._idle,
                    hold=self._hold,
                    on_trouble=self._tell_somebody,
                    send_hidden=self.sends_hidden,
                    parallel=int(getattr(self.cfg, "cloud_parallel", 1) or 1),
                )
            else:
                self._push_settings(self._engine)
            return self._engine

    def _queue_for_the_engine(self) -> int:
        """Queue the library, telling the engine's state how far it has got."""
        engine = self._engine
        if engine is None:
            return self.queue_library()
        return self.queue_library(
            on_queued=lambda total: engine.state.update(queued_so_far=total))

    def _tell_somebody(self, event: str, summary: str, detail: str) -> None:
        """Send the line, if the household has set somewhere to send it.

        Off unless configured, like everything in `notify`. The events have
        existed since that module was written and nothing ever raised them,
        so a backup that stopped stayed stopped until somebody happened to
        open the Cloud page.
        """
        from ..utils import notify                              # noqa: PLC0415

        try:
            notify.from_config(self.cfg).send(event, summary, detail)
        except Exception:                                       # noqa: BLE001
            log.debug("could not report %s", event, exc_info=True)

    def _upload_finished(self) -> None:
        """Everything is up: a restart has no upload left to carry on."""
        from ..storage import resume                        # noqa: PLC0415
        resume.done(self._connect_db(), RESUME_NAME)

    def _encryption(self) -> tuple[bytes, bytes] | None:
        """The key to encrypt with, or None to upload files as they are.

        Encryption switched on with no usable key is refused rather than
        quietly ignored: uploading in the clear because a key file went
        missing is exactly the failure this setting exists to prevent.
        """
        if not self.cfg.cloud_encrypt:
            return None
        record = keyring.load(self.cfg.state_dir)
        if record is None:
            raise RuntimeError("encryption is on but the encryption key is missing or damaged; "
                               "nothing is uploaded until it is restored")
        return keyring.key_material(record)

    def _publish_params(self, client: DriveClient) -> None:
        """Put ninaivu-encryption.json (salt and settings, no key) in the Drive folder, once."""
        import json
        import tempfile
        record = keyring.load(self.cfg.state_dir)
        if record is None or record.get("params_remote_id"):
            return
        with tempfile.TemporaryDirectory(prefix="ninaivu_params_") as tmp:
            params = Path(tmp) / "ninaivu-encryption.json"
            params.write_text(json.dumps(keyring.public_params(record), indent=2), encoding="utf-8")
            remote_id = client.upload_file(params, params.name, client.ninaivu_root())
        keyring.note_params_uploaded(self.cfg.state_dir, remote_id)

    def encryption_status(self, summary: dict[str, Any] | None = None) -> dict[str, Any]:
        record = keyring.load(self.cfg.state_dir)
        return {
            "enabled": bool(self.cfg.cloud_encrypt),
            "key_exists": record is not None,
            "key_id": record["key_id"] if record else "",
            "created_at": record["created_at"] if record else 0,
            "params_in_drive": bool(record and record.get("params_remote_id")),
            "encrypted_uploads": int((summary or {}).get("encrypted", 0)),
            "unencrypted_uploads": int((summary or {}).get("unencrypted", 0)),
            "min_passphrase": keyring.MIN_PASSPHRASE,
        }

    def window(self) -> limits.Window:
        return limits.Window(self.cfg.cloud_window_start,
                             self.cfg.cloud_window_end)

    def _push_settings(self, engine: SyncEngine) -> None:
        """Copy the current settings onto an engine.

        All three are read live — on every file or every chunk — so this is
        what makes a cap or a window edited during a running upload take
        effect on that upload rather than on the next one.
        """
        engine.rate_kbps = int(self.cfg.cloud_rate_kbps or 0)
        engine.window = self.window()
        # From the next run: the files of a run all go through one set of threads.
        engine.parallel = max(1, int(getattr(self.cfg, "cloud_parallel", 1) or 1))

    def apply_settings(self) -> None:
        """Tell a running upload about a settings change. No-op if idle."""
        with self._lock:
            if self._engine is not None:
                self._push_settings(self._engine)

    def sends_hidden(self) -> bool:
        """Whether hidden things are backed up right now.

        "encrypted" means only while the backup is encrypted with a key that is
        there: a call recording does not go to Google as it is because a key
        file went missing.
        """
        mode = str(getattr(self.cfg, "cloud_hidden", "never") or "never")
        if mode == "always":
            return True
        if mode == "encrypted":
            return bool(self.cfg.cloud_encrypt) and keyring.load(self.cfg.state_dir) is not None
        return False

    def _visibility_of(self, root: str, rel: str) -> int | None:
        try:
            row = self._connect_db().execute(
                "SELECT visibility FROM assets WHERE root=? AND rel_path=?",
                (root, rel)).fetchone()
        except Exception:                                   # noqa: BLE001
            # Not knowing is not permission. An unreadable index means "do not
            # send", because the alternative is uploading something hidden.
            log.exception("could not read visibility for %s", rel)
            return 2
        return int(row["visibility"]) if row else None

    def _checks_screens(self, conn) -> bool:
        """Is the screenshot and document check going to reach every picture?

        Only with hiding switched on, analysis on, and a search model that has
        a measured threshold: the loaded one, or while it is still loading,
        the one the library was analysed with. Without all three no picture
        will ever be checked, and holding them for a check would hold them
        for good.
        """
        from .. import ai                                    # noqa: PLC0415
        from ..media import screens                          # noqa: PLC0415
        from ..storage import db                             # noqa: PLC0415

        if not (getattr(self.cfg, "hide_screens", False)
                and getattr(self.cfg, "ai_enabled", False)):
            return False
        model_id = (getattr(ai.get_engine(), "model_id", "")
                    or db.get_meta(conn, "ai_model_id", "") or "")
        return model_id in screens.THRESHOLDS

    def _awaiting_check(self, root: str, rel: str) -> bool:
        from ..media import screens                          # noqa: PLC0415
        # Held only so as not to send something about to be hidden; when
        # hidden things go too, there is nothing to wait for.
        if self.sends_hidden():
            return False
        conn = self._connect_db()
        if not self._checks_screens(conn):
            return False
        row = conn.execute(
            "SELECT kind, thumb, screen_version FROM assets WHERE root=? AND rel_path=?",
            (root, rel)).fetchone()
        return bool(row and row["kind"] == "picture" and row["thumb"]
                    and int(row["screen_version"] or 0) < screens.SCREEN_VERSION)

    # -- the queue --------------------------------------------------------

    def queue_library(self, on_queued: Callable[[int], None] | None = None) -> int:
        """Offer everything in the index to the record. Returns how many were
        added. Anything already uploaded is stepped over, not re-sent.

        Everything except what is hidden, which goes only when the household
        said it may (cloud_hidden), and pictures still waiting to be checked
        for being screenshots or documents, which go once they have been and
        are not hidden.
        """
        from ..media import screens                          # noqa: PLC0415
        conn = self._connect_db()
        store.init_schema(conn)
        hidden_too = self.sends_hidden()
        sql = ("SELECT root, rel_path, filename, size, mtime, kind FROM assets "
               "WHERE trashed=0" + ("" if hidden_too else " AND visibility < 2"))
        params: list[Any] = []
        if not hidden_too and self._checks_screens(conn):
            sql += (" AND NOT (kind='picture' AND thumb IS NOT NULL "
                    "AND screen_version < ?)")
            params.append(screens.SCREEN_VERSION)
        rows = (dict(r) for r in conn.execute(sql, params))
        return store.queue_missing(conn, rows, on_queued=on_queued)

    # -- doing it ---------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Begin uploading. Returns at once; the work happens on the engine.

        The library used to be queued here, on the request's thread, which on
        a large library meant the console waited minutes for an answer and
        nothing was uploaded until the whole index had been looked at. The
        engine asks for the queue itself, on its own thread, the moment it has
        nothing left to send — which at the start of a run is immediately.
        """
        if not self.creds.connected:
            return {"started": False, "reason": "no Google account is connected"}
        engine = self.engine()
        started = engine.start()
        return {"started": started, "already_running": not started}

    def pause(self) -> None:
        if self._engine:
            self._engine.stop()

    def library_changed(self) -> None:
        """A scan added or changed files: a running backup queues them next."""
        if self._engine is not None:
            self._engine.library_changed()

    def retry_failures(self) -> int:
        return store.reset_failures(self._connect_db())

    # -- signing in -------------------------------------------------------

    def set_client(self, client_id: str, client_secret: str) -> None:
        """Save the household's own OAuth client. See :mod:`.drive` for why
        Ninaivu cannot ship one."""
        self.creds.client_id = client_id.strip()
        self.creds.client_secret = client_secret.strip()
        self._save()

    def set_folder(self, name: str) -> bool:
        """Rename the one folder Ninaivu owns in Drive.

        The remembered id is dropped with it. A new name means a new folder,
        and keeping the old id would leave everything landing in the old one
        while the console claimed otherwise. Files already up there stay where
        they are — Ninaivu does not move things around in somebody's Drive.
        """
        name = (name or "").strip().strip("/") or "Ninaivu"
        if name == self.creds.folder_name:
            return False
        self.creds.folder_name = name
        self.creds.folder_id = ""
        self.cfg.cloud_folder_name = name
        self._save()
        return True

    def begin_connect(self, redirect_uri: str) -> str:
        if not self.creds.configured:
            raise ValueError("Add your Google client ID and secret first.")
        state = new_state()
        self._pending_state = (state, time.time())
        return consent_url(self.creds.client_id, redirect_uri, state)

    def finish_connect(self, code: str, state: str, redirect_uri: str) -> None:
        pending = self._pending_state
        self._pending_state = None
        if not pending or not state or state != pending[0]:
            # Without this check any page in the browser could hand Ninaivu a
            # code for an account nobody in this house asked to connect.
            raise ValueError("That sign-in did not come from this console.")
        if time.time() - pending[1] > STATE_TTL:
            raise ValueError("That sign-in took too long. Please start again.")
        client = self.client()
        client.exchange_code(code, redirect_uri)
        self._save(client.creds)

    def disconnect(self, *, forget_uploads: bool = False) -> None:
        """Sign the account out. The record of what has gone up survives.

        That is the point: reconnecting — the same account or a different one —
        must not cause a library's worth of photographs to be uploaded a second
        time. Clearing the record is a separate, deliberate act, and even then
        it only clears Ninaivu's memory: nothing is deleted from Drive.
        """
        try:
            self.client().revoke()
        except Exception:                                   # noqa: BLE001
            log.debug("could not tell Google about the disconnect")
        self.pause()
        keep_id, keep_name = "", self.creds.folder_name
        self.creds = Credentials(folder_name=keep_name, folder_id=keep_id)
        self._save()
        if forget_uploads:
            conn = self._connect_db()
            conn.execute("DELETE FROM cloud_uploads")
            conn.commit()

    #: How long a whole-queue count is reused before it is taken again. The
    #: count itself is 6.8 ms on a 156,000-row queue, but the activity strip
    #: asks every couple of seconds from every open tab, so the cost that
    #: matters is per-ask, not per-count.
    QUEUE_FRESH_FOR = 8.0

    def queue_progress(self) -> tuple[int, int]:
        """(done, total) for the whole backup, cheaply.

        For the one line that says how far through the library this is. The
        answer moves slowly — a file at a time — so a few seconds old is not
        stale in any way somebody could notice.
        """
        now = time.time()
        seen = getattr(self, "_queue_seen", None)
        if seen and now - seen[0] < self.QUEUE_FRESH_FOR:
            return seen[1], seen[2]
        try:
            conn = self._connect_db()
            store.init_schema(conn)
            row = conn.execute(
                "SELECT COALESCE(SUM(state=?), 0), COUNT(*) FROM cloud_uploads",
                (store.DONE,)).fetchone()
            done, total = int(row[0] or 0), int(row[1] or 0)
        except Exception:                                    # noqa: BLE001
            # A reading nobody can take is not worth failing a status line for.
            return (seen[1], seen[2]) if seen else (0, 0)
        self._queue_seen = (now, done, total)
        return done, total

    # -- getting it back ---------------------------------------------------

    #: Why the indexer is standing down during a restore.
    RESTORE_REASON = "restoring from Google Drive"

    def restore_items(self, scope: dict[str, Any], *,
                      recovery: dict[str, Any] | None = None,
                      passphrase: str | None = None) -> list[restore.RestoreItem]:
        """The files a restore of *scope* would bring back.

        ``source`` is ``record`` (what this Ninaivu recorded uploading — the
        normal case) or ``drive`` (a new machine with no record of its own).
        For ``drive``, the copy of the index kept in Drive is used when there
        is one — it knows every file's full path and checksum — and the Drive
        folder is walked only when there is not. ``roots``, ``folder`` and
        ``asset_ids`` narrow it.
        """
        self.index_found = None
        if scope.get("source") == "drive":
            if not self.creds.connected:
                raise ValueError("Connect the Google account first.")
            client = self.client()
            copies = index_copy.find(client)
            if copies:
                newest = copies[0]
                key = (self._key_from(recovery, passphrase, index=True)
                       if str(newest.get("name", "")).endswith(".ninaivu") else None)
                state, bundle = self._index_copy_state(client, newest, key)
                with closing(sqlite3.connect(str(state / "index.db"))) as conn:
                    conn.row_factory = sqlite3.Row
                    items = restore.from_record(conn, folder=str(scope.get("folder") or ""))
                self.index_found = {"bundle": str(bundle),
                                    "modified": newest.get("modifiedTime", ""),
                                    "encrypted": key is not None,
                                    "roots": sorted({i.root for i in items if i.root})}
                return items
            return restore.from_drive(client, folder=str(scope.get("folder") or ""))
        return restore.from_record(
            self._connect_db(), roots=scope.get("roots") or None,
            folder=str(scope.get("folder") or ""),
            asset_ids=scope.get("asset_ids") or None)

    def _index_copy_state(self, client, entry: dict[str, Any], key: bytes | None):
        """Download (once) and unpack the newest index copy from Drive."""
        tag = "".join(c for c in f"{entry.get('id')}-{entry.get('modifiedTime', '')}"
                      if c.isalnum() or c in "-_")[:120]
        work = Path(self.cfg.state_dir) / "from-drive" / tag
        state = work / "unpacked" / "state"
        bundles = sorted(work.glob("*.tar.gz"))
        if state.is_dir() and bundles:
            return state, bundles[0]
        shutil.rmtree(work, ignore_errors=True)
        state, bundle = index_copy.fetch(client, entry, key, work)
        # Named like a backup bundle, and kept: it is what brings albums,
        # faces and names back with tools/backup_restore.py.
        kept = work / f"ninaivu_backup_{time.strftime('%Y%m%d_%H%M%S')}_from_drive.tar.gz"
        bundle.replace(kept)
        return state, kept

    def _key_from(self, recovery: dict[str, Any] | None, passphrase: str | None,
                  *, index: bool = False) -> bytes:
        """A key from a recovery file, the passphrase, or this machine."""
        if recovery is not None:
            return keyring.key_from(recovery=recovery)
        if passphrase:
            record = keyring.load(self.cfg.state_dir)
            params = keyring.public_params(record) if record else None
            if params is None:
                params = restore.find_params(self.client())
            if params is None:
                raise ValueError("The key settings (ninaivu-encryption.json) are "
                                 "not in the Drive folder; use the recovery file.")
            return keyring.key_from(passphrase=passphrase, params=params)
        record = keyring.load(self.cfg.state_dir)
        if record is not None:
            return keyring.key_material(record)[0]
        if index:
            raise index_copy.NeedsKey(
                "The copy of the index in Drive is encrypted, and this computer "
                "does not have the key. Choose the recovery file, or type the "
                "passphrase, then check again.")
        raise ValueError("These backups are encrypted. Give the recovery file, "
                         "or the passphrase the key was made with.")

    def restore_key(self, items: list[restore.RestoreItem], *,
                    recovery: dict[str, Any] | None = None,
                    passphrase: str | None = None) -> bytes | None:
        """The key for an encrypted restore, from wherever it can be had.

        This machine's own key first; then a recovery file; then the
        passphrase with the settings Ninaivu left in the Drive folder. None when
        nothing in *items* is encrypted. ValueError when something is and no
        key can be found — before a byte is downloaded, rather than as one
        failure per file.
        """
        if not any(item.encrypted for item in items):
            return None
        return self._key_from(recovery, passphrase)

    def restore_start(self, items: list[restore.RestoreItem], *,
                      destination: str | None, key: bytes | None) -> dict[str, Any]:
        """Begin bringing *items* back. Returns at once."""
        if self._restore is not None and self._restore.running:
            raise ValueError("A restore is already running.")
        if not self.creds.connected:
            raise ValueError("Connect the Google account first.")
        if not items:
            raise ValueError("There is nothing to restore in what was chosen.")
        if destination is None and any(not item.root for item in items):
            raise ValueError("Choose a folder to restore into: these files were "
                             "found in Drive, not in this Ninaivu's record.")
        roots = {item.root for item in items if item.root}
        if destination is not None and len(roots) > 1:
            # Several library folders into one: each keeps a folder of its own
            # name, or two libraries' 2019 folders would be poured together.
            for item in items:
                if item.root:
                    item.rel_path = f"{Path(item.root).name or 'library'}/{item.rel_path}"
        found = getattr(self, "index_found", None)
        # The upload and the restore would share one connection and one Drive
        # quota, and the indexer would chase the restore's writes file by
        # file. Both wait; both are put back afterwards.
        engine = self._engine
        self._restore_resumes_upload = bool(engine and engine.running)
        if self._restore_resumes_upload:
            engine.stop()
        if self.scanner is not None:
            self.scanner.defer(self.RESTORE_REASON)
        job = restore.RestoreJob(connect=self.client, items=items,
                                 destination=destination, key=key,
                                 on_done=self._restore_done)
        job.index_found = found
        self._restore = job
        job.start()
        return self.restore_status()

    def _restore_done(self, job: restore.RestoreJob) -> None:
        if self.scanner is not None:
            self.scanner.resume(self.RESTORE_REASON)
            # A restore into a folder that is not a library is the household's
            # to add; one into a library is indexed straight away.
            libraries = {str(Path(r)) for r in self.cfg.libraries}
            wanted = [r for r in job.touched_roots if str(Path(r)) in libraries]
            if job.destination is not None and str(job.destination) in libraries:
                wanted.append(str(job.destination))
            if wanted:
                self.scanner.start(sorted(set(wanted)))
        if self._restore_resumes_upload:
            self._restore_resumes_upload = False
            self.start()
        found = getattr(job, "index_found", None)
        if found:
            old = " ".join(f'--from "{r}"' for r in found["roots"][:1])
            to = f'--to "{job.destination}"' if job.destination else ""
            job.state.update(message=(
                f"{job.state.snapshot()['message']} The copy of the index came from "
                f"Drive too, and is kept at {found['bundle']}. To bring back albums, "
                f"faces, names and who may see what: stop Ninaivu, run "
                f"python tools/backup_restore.py restore \"{found['bundle']}\", then "
                f"python tools/reroot_library.py {old} {to}, and start Ninaivu.").strip())
        snap = job.state.snapshot()
        if snap["failed"]:
            self._tell_somebody(
                "cloud_failed", "Some files could not be restored from Google Drive",
                f"{snap['failed']:,} of {snap['total']:,} files failed their "
                f"checks or could not be downloaded. The Cloud page lists them.")

    def restore_stop(self) -> None:
        if self._restore is not None:
            self._restore.stop()

    def restore_status(self) -> dict[str, Any]:
        job = self._restore
        return job.state.snapshot() if job is not None else {"running": False}

    # -- what the console shows -------------------------------------------

    def status(self) -> dict[str, Any]:
        conn = self._connect_db()
        store.init_schema(conn)
        engine = self._engine
        state = engine.state.snapshot() if engine else {}
        summary = store.summary(conn)
        speed = float(state.get("speed_bps", 0.0)) if state else 0.0
        waiting = int(summary.get("bytes_waiting", 0))
        summary["eta_seconds"] = (
            round(waiting / speed, 1) if speed > 0 and waiting > 0 else 0.0)
        window = self.window()
        return {
            "enabled": bool(self.cfg.cloud_enabled),
            "autostart": bool(self.cfg.cloud_autostart),
            "rate_kbps": int(self.cfg.cloud_rate_kbps or 0),
            "window_start": self.cfg.cloud_window_start or "",
            "window_end": self.cfg.cloud_window_end or "",
            "hidden": str(getattr(self.cfg, "cloud_hidden", "never") or "never"),
            "parallel": max(1, int(getattr(self.cfg, "cloud_parallel", 1) or 1)),
            "full_speed": bool(getattr(self.cfg, "cloud_full_speed", False)),
            "hidden_now": self.sends_hidden(),
            "window_label": window.label(),
            "window_open": window.is_open(),
            "window_opens_at": window.opens_at(),
            "folder_name": self.creds.folder_name or "Ninaivu",
            "account": self.creds.public(),
            "running": bool(engine and engine.running),
            "state": state,
            "queue": summary,
            "encryption": self.encryption_status(summary),
            "recent": store.recent(conn, limit=12),
            "failures": store.recent(conn, limit=12, state=store.FAILED),
            "skipped": store.recent(conn, limit=12, state=store.SKIPPED),
        }
