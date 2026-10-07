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

from . import approvals, index_copy, keyring, limits, restore, store
from .rules import REASON as RULE_REASON, Rules
from .drive import Credentials, DriveClient, DriveError, consent_url, new_state
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


def _same_path(path: str) -> str:
    """A folder path in the form two spellings of the same folder share."""
    import os                                               # noqa: PLC0415
    return os.path.normcase(os.path.normpath(os.path.abspath(
        os.path.expanduser(str(path)))))


def _drive_time(text: str) -> float:
    """Drive's RFC 3339 time as seconds, or 0 when there is none to read."""
    import datetime as _dt                                  # noqa: PLC0415
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _rules_key(cfg) -> tuple[Any, ...]:
    """The settings Rules.from_config reads, as values: a list changed in
    place compares equal to itself, so the lists are copied."""
    def frozen(value: Any) -> Any:
        return tuple(value) if isinstance(value, (list, tuple)) else value
    return tuple(frozen(getattr(cfg, name, None)) for name in
                 ("cloud_kinds", "cloud_max_mb", "cloud_skip_folders", "cloud_skip_words"))


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
        #: When the household was last told that large files wait for a yes.
        self._approvals_told = 0.0
        # What the engine asks about every file, kept rather than remade for
        # each one: the rules by the settings they came from, the key record
        # by the key file's stamp, and whether the queue's tables are there.
        self._rules: tuple[tuple[Any, ...], Rules] | None = None
        self._key_record: tuple[tuple[str, Any], dict[str, Any] | None] | None = None
        self._schema_ready = False

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
                    kept_back=self._kept_back,
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
        record = self._keyring_record()
        if record is None:
            # Never used a key before: the household has simply not made one
            # yet. A key that encrypted uploads and is now gone is a different
            # matter — making a new one would not read those — so that says
            # "missing" and points at the recovery file.
            try:
                conn = self._connect_db()
                store.init_schema(conn)
                encrypted_before = int(store.summary(conn).get("encrypted", 0) or 0)
            except Exception:                                # noqa: BLE001
                encrypted_before = 1
            if not keyring.path(self.cfg.state_dir).exists() and not encrypted_before:
                raise RuntimeError("make the encryption key on the Mugil page first; nothing is "
                                   "uploaded until there is one, so nothing goes up unencrypted")
            raise RuntimeError("encryption is on but the encryption key is missing or damaged; "
                               "nothing is uploaded until it is restored")
        return keyring.key_material(record)

    def _keyring_record(self) -> dict[str, Any] | None:
        """The key record, read again only when the key file has changed.

        Asked before every file that goes up; reading and checking the file
        each time was a disk read and a hash per upload for an answer that
        changes once in the life of a library.
        """
        path = keyring.path(self.cfg.state_dir)
        try:
            stat = path.stat()
            stamp: Any = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        kept = self._key_record
        if kept is not None and kept[0] == (str(path), stamp):
            return kept[1]
        record = keyring.load(self.cfg.state_dir)
        self._key_record = ((str(path), stamp), record)
        return record

    #: Which Drive folder ninaivu-encryption.json was put in, beside the key
    #: record rather than in it (the key file is keyring's to write).
    PARAMS_PLACED = "cloud-encryption-published.json"

    def _publish_params(self, client: DriveClient) -> None:
        """Put ninaivu-encryption.json (salt and settings, no key) in the Drive folder.

        Once per folder, not once for ever. It used to be sent once and the
        key record marked; after the household chose a different Drive folder
        (:meth:`set_folder`) the new folder, where every upload now went, had
        no settings file in it — and a new machine restoring from that folder
        with the passphrase found nothing to derive the key with. So the
        folder it went into is kept too, and when the folder now in use is
        another one, it is looked for there and sent if it is missing.

        Called before every encrypted file, so the usual case costs nothing:
        the folder id the client already holds is compared with the one
        remembered, without asking Drive.
        """
        import json
        import tempfile
        record = self._keyring_record()
        if record is None:
            return
        current = client.creds.folder_id
        placed = self._params_placed()
        if (record.get("params_remote_id") and current
                and placed.get("folder_id") == current
                and placed.get("remote_id") == record.get("params_remote_id")):
            return
        folder = client.ninaivu_root()
        if (record.get("params_remote_id") and placed.get("folder_id") == folder
                and placed.get("remote_id") == record.get("params_remote_id")):
            return
        # A folder not known to hold it — a new one, or one from before this
        # was remembered. It may already be there (put there by the old code,
        # or by this machine before a reinstall); sending a second copy would
        # be harmless but untidy, so look first. The plain-named file is only
        # taken as this key's when it names this key: one left by an earlier
        # key used to be adopted as it was, and the passphrase then had
        # nothing to make this key from. A later key gets a file of its own.
        files = [entry for entry in client.list_folder(folder)
                 if entry.get("mimeType") != "application/vnd.google-apps.folder"]
        own_name = keyring.params_name(record)
        existing = next((e for e in files if e.get("name") == own_name), None)
        plain = next((e for e in files if e.get("name") == restore.PARAMS_NAME), None)
        if existing is None and plain is not None and \
                self._params_key_id(client, plain) == record["key_id"]:
            existing = plain
        if existing is not None:
            remote_id = str(existing["id"])
        else:
            name = restore.PARAMS_NAME if plain is None else own_name
            with tempfile.TemporaryDirectory(prefix="ninaivu_params_") as tmp:
                params = Path(tmp) / name
                params.write_text(json.dumps(keyring.public_params(record), indent=2),
                                  encoding="utf-8")
                remote_id = client.upload_file(params, params.name, folder)
        keyring.note_params_uploaded(self.cfg.state_dir, remote_id)
        self._note_params_placed(folder, remote_id)

    @staticmethod
    def _params_key_id(client: DriveClient, entry: dict[str, Any]) -> str:
        """Which key a settings file in Drive is for; "" when it cannot be read."""
        import json
        try:
            size = int(entry.get("size") or 0) or 65536
            found = json.loads(client.download_range(str(entry["id"]), 0,
                                                     min(size, 65536) - 1).decode("utf-8"))
        except (DriveError, OSError, UnicodeDecodeError, ValueError):
            return ""
        return str(found.get("key_id") or "") if isinstance(found, dict) else ""

    def _params_placed(self) -> dict[str, Any]:
        import json
        try:
            found = json.loads((Path(self.cfg.state_dir) / self.PARAMS_PLACED)
                               .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def _note_params_placed(self, folder_id: str, remote_id: str) -> None:
        import json
        target = Path(self.cfg.state_dir) / self.PARAMS_PLACED
        pending = target.with_suffix(".tmp")
        pending.write_text(json.dumps({"folder_id": folder_id, "remote_id": remote_id}),
                           encoding="utf-8")
        pending.replace(target)

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
            return bool(self.cfg.cloud_encrypt) and self._keyring_record() is not None
        return False

    # -- what is left out, and what waits for a yes ------------------------

    def rules(self) -> Rules:
        """What the household leaves out of the backup, as it is set now.

        Remade only when the settings it reads have changed: the engine asks
        for every file, and cleaning two lists of folders and words each
        time was most of what the question cost.
        """
        key = _rules_key(self.cfg)
        kept = self._rules
        if kept is None or kept[0] != key:
            kept = (key, Rules.from_config(self.cfg))
            self._rules = kept
        return kept[1]

    def _kept_back(self, row: dict[str, Any]) -> str | None:
        """Why this queued file may not go now, or None. A backup rule first;
        then a file over the approval size that nobody has said yes to."""
        reason = self.rules().why(row.get("rel_path", ""), int(row.get("size") or 0),
                                  str(row.get("kind") or ""))
        if reason:
            return reason
        conn = self._connect_db()
        if not self._schema_ready:
            # Once, not per file: executescript commits whatever the caller
            # has open before it runs, and the tables do not come and go.
            store.init_schema(conn)
            self._schema_ready = True
        reason = approvals.why(conn, self.cfg, row.get("root", ""), row.get("rel_path", ""),
                               int(row.get("size") or 0))
        if reason and reason.startswith(approvals.REASON):
            self._tell_about_approvals()
        return reason

    def _tell_about_approvals(self) -> None:
        """Say that files are waiting for approval -- at most twice a day."""
        now = time.time()
        if now - self._approvals_told < 12 * 3600:
            return
        self._approvals_told = now
        self._tell_somebody(
            "cloud_approval", "Large files are waiting for your approval",
            "Files over the size that needs approval are held back from the cloud "
            "backup until an administrator approves them on the Mugil page.")

    def apply_rules(self) -> dict[str, int]:
        """Set aside what the rules now hold, and release what they no longer do."""
        conn = self._connect_db()
        store.init_schema(conn)
        result = self.rules().apply(conn)
        self.apply_approvals()
        return result

    def apply_approvals(self) -> dict[str, int]:
        """Hold what is queued over the approval size; release what no longer is."""
        conn = self._connect_db()
        store.init_schema(conn)
        return approvals.apply(conn, self.cfg)

    def _rules_status(self, conn) -> dict[str, Any]:
        rules = self.rules()
        held = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM cloud_uploads "
            "WHERE state='skipped' AND error LIKE ?", (RULE_REASON + "%",)).fetchone()
        return {**rules.public(), "on": rules.any,
                "held": int(held[0]), "held_bytes": int(held[1])}

    def _approvals_status(self, conn) -> dict[str, Any]:
        return {"limit_mb": int(getattr(self.cfg, "cloud_approval_mb", 1024) or 0),
                **approvals.totals(conn)}

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
        sql = ("SELECT id, root, rel_path, filename, size, mtime, kind FROM assets "
               "WHERE trashed=0" + ("" if hidden_too else " AND visibility < 2"))
        params: list[Any] = []
        if not hidden_too and self._checks_screens(conn):
            sql += (" AND NOT (kind='picture' AND thumb IS NOT NULL "
                    "AND screen_version < ?)")
            params.append(screens.SCREEN_VERSION)
        # And not what a rule keeps back. Not offered, it stays set aside
        # rather than being queued again only to be set aside again.
        held, held_args = self.rules().held()
        sql += f" AND NOT {held}"
        params += held_args
        queued = store.queue_missing(conn, _in_pages(conn, sql, params), on_queued=on_queued)
        # A large file queued (or put back in the queue) is set aside for an
        # administrator's approval straight away, so it is listed as waiting
        # rather than sitting in the queue until the uploader reaches it.
        approvals.apply(conn, self.cfg)
        return queued

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
        self.apply_approvals()
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
                newer = self._newer_in_drive(client, items, newest,
                                             str(scope.get("folder") or ""))
                self.index_found = {"bundle": str(bundle),
                                    "modified": newest.get("modifiedTime", ""),
                                    "encrypted": key is not None,
                                    "roots": sorted({i.root for i in items if i.root}),
                                    "newer_in_drive": len(newer)}
                return items + newer
            return restore.from_drive(client, folder=str(scope.get("folder") or ""))
        return restore.from_record(
            self._connect_db(), roots=scope.get("roots") or None,
            folder=str(scope.get("folder") or ""),
            asset_ids=scope.get("asset_ids") or None)

    #: How long before the copy of the index went up a file in Drive may have
    #: gone up and still be one that copy does not know. The copy is a
    #: snapshot taken a while before its upload finished.
    NEWER_SLACK = 24 * 3600

    def _newer_in_drive(self, client, items: list[restore.RestoreItem],
                        copy: dict[str, Any], folder: str) -> list[restore.RestoreItem]:
        """Files in Drive the copy of the index does not know: sent after it was made.

        The copy is made at most daily, and not while it is held or failing,
        so a restore from it alone left out everything uploaded since, and
        said "Restored N files" as if that were all. The Drive folder is
        walked and every file whose Drive id the copy does not name, and
        that went up since about when the copy was made, is added — with
        Drive's own MD5 to check it by, and the two folder levels it was
        uploaded into for its place (the copy's full path is not known for
        it). Older unknown files are earlier versions of files the copy has.
        """
        known = {item.remote_id for item in items}
        made = _drive_time(str(copy.get("modifiedTime") or ""))
        newer = []
        for item in restore.from_drive(client, folder=folder):
            if item.remote_id in known or item.rel_path.split("/", 1)[0] == index_copy.FOLDER:
                continue
            when = _drive_time(item.drive_modified)
            if made and when and when < made - self.NEWER_SLACK:
                continue
            newer.append(item)
        return newer

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
        # faces and names back with `ninaivu restore`.
        kept = work / f"ninaivu_backup_{time.strftime('%Y%m%d_%H%M%S')}_from_drive.tar.gz"
        bundle.replace(kept)
        return state, kept

    def _key_from(self, recovery: dict[str, Any] | None, passphrase: str | None,
                  *, index: bool = False) -> bytes:
        """A key from a recovery file, the passphrase, or this machine."""
        if recovery is not None:
            return keyring.key_from(recovery=recovery)
        if passphrase:
            # This machine's key settings first, then every key's settings in
            # the Drive folder: a key made later, or one brought back from a
            # recovery file, has its own, and the passphrase may be for any.
            record = keyring.load(self.cfg.state_dir)
            tried: set[str] = set()
            problem: Exception | None = None

            def attempt(params: dict[str, Any]) -> bytes | None:
                nonlocal problem
                if str(params.get("key_id") or "") in tried:
                    return None
                tried.add(str(params.get("key_id") or ""))
                try:
                    return keyring.key_from(passphrase=passphrase, params=params)
                except (ValueError, KeyError, TypeError) as exc:
                    problem = problem or exc
                    return None

            if record is not None:
                key = attempt(keyring.public_params(record))
                if key is not None:
                    return key
            elsewhere = (restore.all_params(self.client())
                         if record is None or self.creds.connected else [])
            for params in elsewhere:
                key = attempt(params)
                if key is not None:
                    return key
            if problem is not None:
                raise ValueError(str(problem))
            raise ValueError("The key settings (ninaivu-encryption.json) are "
                             "not in the Drive folder; use the recovery file.")
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

    def roots_outside_libraries(self, items: list[restore.RestoreItem]) -> list[str]:
        """The folders *items* would be put back into that are not libraries here.

        A restore "to the original folders" from the copy of the index in
        Drive writes to whatever absolute paths that index names — the paths
        of another computer, possibly years old, possibly a system folder on
        this one. Only the library folders configured on this machine are
        trusted as places to write without being asked; anything else needs
        a destination chosen for it. Compared the way the platform compares
        paths: case-insensitively on Windows.
        """
        libraries = {_same_path(root) for root in (getattr(self.cfg, "roots", None) or [])}
        return sorted({item.root for item in items
                       if item.root and _same_path(item.root) not in libraries})

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
        found = getattr(self, "index_found", None)
        if destination is None and found:
            # From the copy of the index in Drive: see roots_outside_libraries.
            # The console refuses this before it gets here; this is the same
            # rule for anything else that calls in.
            outside = self.roots_outside_libraries(items)
            if outside:
                raise ValueError(
                    "Choose a folder to restore into: the copy of the index names "
                    f"folders that are not libraries on this computer ({', '.join(outside[:3])}).")
        roots = {item.root for item in items if item.root}
        if destination is not None and len(roots) > 1:
            # Several library folders into one: each keeps a folder of its own
            # name, or two libraries' 2019 folders would be poured together.
            for item in items:
                if item.root:
                    item.rel_path = f"{Path(item.root).name or 'library'}/{item.rel_path}"
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
            # The upload was told to stop, not waited for: it may still be
            # finishing the file it had started, and Start refuses while it
            # is. A short restore then left the backup off with nothing on the
            # screen saying so. This is the restore's own thread, so it waits.
            engine = self._engine
            if engine is not None:
                engine.stop(join=True, timeout=RESUME_WAIT_SECONDS)
            result = self.start()
            if result.get("started") or (result.get("already_running")
                                         and not engine._stop.is_set()):
                self._restore_resumes_upload = False
            else:
                log.warning("the backup could not be resumed after the restore: %s",
                            result.get("reason") or "it is still stopping")
        found = getattr(job, "index_found", None)
        if found:
            old = " ".join(f'--from "{r}"' for r in found["roots"][:1])
            to = f'--to "{job.destination}"' if job.destination else ""
            job.state.update(message=(
                f"{job.state.snapshot()['message']} The copy of the index came from "
                f"Drive too, and is kept at {found['bundle']}. To bring back albums, "
                f"faces, names and who may see what: stop Ninaivu, run "
                f"ninaivu restore \"{found['bundle']}\", then "
                f"ninaivu reroot {old} {to}, and start Ninaivu.").strip())
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
            "rules": self._rules_status(conn),
            "approvals": self._approvals_status(conn),
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


#: How long a finished restore waits for the paused upload to let go.
RESUME_WAIT_SECONDS = 300.0

#: Rows of the index read at a time while the backup queue is brought up to date.
QUEUE_PAGE = 2000


def _in_pages(conn, sql: str, params: list[Any]):
    """The rows *sql* finds, read a page at a time and each page read whole.

    The queue is written to as it is read. With one statement left open over
    the whole index, its snapshot was older than anything another part of
    Ninaivu committed meanwhile (the indexer every 64 files, the uploads after
    every file), and the first write after that failed at once with
    "database is locked" — so during a big import the backup spent its time
    retrying the queue instead of sending anything.
    """
    after = 0
    while True:
        page = conn.execute(f"{sql} AND id > ? ORDER BY id LIMIT ?",
                            [*params, after, QUEUE_PAGE]).fetchall()
        if not page:
            return
        after = int(page[-1]["id"])
        for row in page:
            yield dict(row)
        if len(page) < QUEUE_PAGE:
            return
