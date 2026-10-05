"""Ninaivu — the family media library.

Ninaivu runs as **two applications in one process**, on two ports:

``80`` — the *home* app
    What the household sees. Opens on a profile picker: tap your face to come
    in (with a PIN only if the admin set one), or browse as a guest. Nothing
    administrative is even routed here — an admin endpoint on this port is a
    404, not a 403, so the family app has no attack surface for it at all.
    80 is the default because it's the standard HTTP port, so an mDNS name
    (``http://ninaivu.local``) needs no port typed after it; it steps up to
    the next free port if 80 is already taken.

``3000`` — the *admin console*
    A separate, purpose-built management interface behind an admin login.
    People and profiles, folder visibility, library and scan controls, an
    activity log, the **Archive** tab — the consolidation engine that sweeps
    old drives, cards and backup folders into one hash-verified
    ``YYYY/MM/DD`` archive, which the library can then be pointed at — and the
    **Cloud** tab, a one-way backup of the library into a single folder in a
    Google Drive account, which never reads anything back.

Both share one config, one index and one session table, so a change made in
the console shows up on the home app immediately.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Before anything can start a child process: on Windows, a Ninaivu with no
# console (pythonw, a detached restart) would flash one for each of them.
from . import nowindow as _nowindow
_nowindow.install()

from flask import Flask, g, jsonify, request

from .server.config import Config
from . import extensions

__version__ = "0.1.2"
APP_NAME = "Ninaivu"
TAGLINE = "Your family's media, at home."
#: Who holds the copyright, shown in About and the console. The licence stays
#: MIT (see LICENSE): naming the holder does not take any freedom away.
COPYRIGHT_HOLDER = "Jagadeesh Rajendran"
COPYRIGHT = f"© 2026 {COPYRIGHT_HOLDER}"
LICENCE = "MIT"


def about() -> dict[str, str]:
    """Name, version, copyright and licence, for any page or API that shows them."""
    return {"name": APP_NAME, "version": __version__,
            "copyright": COPYRIGHT, "licence": LICENCE}

#: Which face an app is wearing.
FACE_HOME = "home"
FACE_ADMIN = "admin"

__all__ = [
    "create_app", "create_home_app", "create_admin_app", "build_services",
    "Config", "APP_NAME", "TAGLINE", "FACE_HOME", "FACE_ADMIN", "__version__",
    "COPYRIGHT_HOLDER", "COPYRIGHT", "LICENCE", "about",
]


class Services:
    """Everything both apps share: config, scanner and AI engine."""

    def __init__(self, cfg: Config) -> None:
        from . import ai as ai_mod
        from . import archive
        from .archive.guardian import ArchiveGuardian
        from .archive.scanner import is_scanning
        from .media.scanner import Scanner
        from .server import auth
        from .storage import db
        from .utils import logs, PowerPolicy

        cfg.ensure_dirs()
        # Before anything else that might have something to say.
        logs.configure(cfg.state_dir, debug=cfg.debug)
        self.cfg = cfg
        # Which kind of computer this is, and so what "auto" means below.
        from .server import tiers                                   # noqa: PLC0415
        self.tier = tiers.current(cfg)
        tiers.apply(cfg, self.tier["tier"])
        logging.getLogger(__name__).info("hardware tier: %s (%s)", self.tier["tier"], self.tier["why"])
        conn = db.init_db(cfg.db_path)
        auth.init_auth_schema(conn)

        # The archive engine keeps its own database — the record of every file
        # it has copied and verified. Point it at Ninaivu's state directory so
        # that record lives with everything else Ninaivu owns, and survives
        # reinstalling the application.
        archive.configure(cfg.state_dir, cfg.min_media_bytes)
        archive.database.init_db()

        from .storage import BackupKeeper                 # noqa: PLC0415
        self.backups = BackupKeeper(cfg)
        from .server.updates import UpdateChecker             # noqa: PLC0415
        self.updates = UpdateChecker(cfg, __version__)

        # Reads what Windows already records about failing drives, which until
        # now somebody had to find in the event log by hand. See
        # ninaivu/storage/disk_health.py.
        from .storage.disk_health import DiskWatch        # noqa: PLC0415
        self.disks = DiskWatch(cfg, lambda: db.connect(cfg.db_path),
                               notify=self._tell_somebody)

        # The one part of Ninaivu that speaks first. Everything else here is
        # about keeping photographs; this sends one back. Off unless the
        # household asks for it — see ninaivu/utils/digest.py.
        from .utils.digest import DigestKeeper             # noqa: PLC0415
        self.digest = DigestKeeper(
            cfg,
            connect=lambda: db.connect(cfg.db_path),
            roots=lambda: cfg.roots or ([cfg.active_root] if cfg.active_root else []),
        )

        # Every AI model in one folder — the search and editing models, the
        # face detector and recogniser, the orientation model. They used to be
        # split between `.ai-models` and the state directory, which meant
        # moving Ninaivu to another machine left half of them behind and the
        # household re-downloaded gigabytes to get face grouping back.
        from .media import model_catalog, orientnet, straighten
        model_catalog.configure(cfg.ai_models_dir or None)
        models = model_catalog.models_root()
        self._gather_models(models)
        orientnet.configure(models)
        straighten.init_schema(conn)

        # Downloaded models are found through absolute paths in settings.json,
        # which point at where Ninaivu *was* after it moves to another folder or
        # machine. Repaired here, on every start, so a model that is on disk is
        # a model that works. Never fatal: the gallery does not need it.
        try:
            if repaired := model_catalog.ensure_settings():
                logging.getLogger(__name__).info(
                    "pointed %s back at where their models are", ", ".join(repaired))
        except Exception:                                   # noqa: BLE001
            logging.getLogger(__name__).exception("could not check model settings")

        # The record of what has gone to the cloud lives in the same database
        # as the index, so a backup of Ninaivu's state directory carries both.
        # Losing one without the other is what causes a library to be uploaded
        # twice, so they are deliberately not separable.
        from .cloud import service as cloud_service, store as cloud_store
        cloud_store.init_schema(conn)
        from .media import phone_backup
        phone_backup.init_schema(conn)
        self._app_configs = []
        from .server.workload import Workload
        #: The household's claim on the machine: which background work waits
        #: while people use the app, and when it may run flat out.
        self.workload = Workload(cfg)
        self.cloud = cloud_service.CloudService(
            cfg, connect_db=lambda: db.connect(cfg.db_path), idle=self._cloud_idle,
            hold=lambda: self.workload.hold("upload"))

        self.scanner = Scanner(cfg)
        # A restore writes into the library: the indexer stands down while it
        # does, and indexes what came back afterwards.
        self.cloud.scanner = self.scanner
        # And what a scan finds goes into a running backup's queue then, not
        # when everything owed before it has gone.
        self.scanner.add_listener(self._scan_found)
        # Proves the cloud backup restores, a few files a week. See
        # ninaivu/cloud/restore_test.py.
        from .cloud.restore_test import RestoreTester          # noqa: PLC0415
        self.restore_tests = RestoreTester(
            cfg, self.cloud, connect_db=lambda: db.connect(cfg.db_path),
            notify=self._tell_somebody, hold=lambda: self.workload.hold("upload"))
        # The index itself, in Drive, so a lost computer loses nothing but
        # time. See ninaivu/cloud/index_copy.py.
        from .cloud.index_copy import IndexCopy                 # noqa: PLC0415
        self.index_copy = IndexCopy(
            cfg, self.cloud, connect_db=lambda: db.connect(cfg.db_path),
            hold=lambda: self.workload.hold("upload"))
        # "Is everything safe?", asked of every one of the above at once.
        from .server.safety import Safety                        # noqa: PLC0415
        self.safety = Safety(self)
        self.scanner.workload = self.workload
        self.straightener = straighten.Straightener(cfg, self.scanner)
        from .utils.resources import budget
        self.power = PowerPolicy(profile=budget()['mode'])
        self.guardian = ArchiveGuardian(is_scanning)
        self.engine = None
        self._ai_mod = ai_mod
        self._started = False

    def _scan_found(self, payload: dict) -> None:
        # "indexed" as soon as a folder's new files are in, "done" once they
        # have been checked for screenshots, which holds pictures back till then.
        if (payload.get("phase") in ("indexed", "done")
                and (payload.get("added") or payload.get("updated"))):
            self.cloud.library_changed()
        # Once the scan has settled: look for sideways photographs among what
        # it indexed, if the household has left that switch on (the default).
        # The survey holds the indexer aside while it runs, so it is started
        # after "done" rather than during, and never while a scan is running.
        straightener = getattr(self, "straightener", None)
        if (straightener is not None and payload.get("phase") == "done"
                and (payload.get("added") or payload.get("updated"))):
            cfg = self.cfg
            roots = cfg.libraries or ([cfg.active_root] if cfg.active_root else [])
            try:
                straightener.after_scan([str(r) for r in roots])
            except Exception:                                      # noqa: BLE001
                logging.getLogger(__name__).debug("the straightening survey did not start",
                                                  exc_info=True)

    def _tell_somebody(self, event: str, summary: str, detail: str) -> None:
        """A notification, if the household has set somewhere to send one."""
        from .utils import notify                                  # noqa: PLC0415

        try:
            notify.from_config(self.cfg).send(event, summary, detail)
        except Exception:                                          # noqa: BLE001
            logging.getLogger(__name__).debug("could not report %s", event,
                                              exc_info=True)

    def _cloud_idle(self) -> bool:
        """May the upload take its larger, idle-time chunks right now?

        Only when the household is not using the app (see workload.py) and no
        other background job wants the disk — except at night in overnight
        mode, which is the time set aside for exactly this.
        """
        from .server.activity import other_work_active
        from .server.workload import OVERNIGHT
        workload = self.workload
        if not workload.boost("upload"):
            return False
        if workload.upload_full_speed():
            return True
        if workload.mode == OVERNIGHT and workload.is_night():
            return True
        return not any(other_work_active(self, config)
                       for config in (self._app_configs or [None]))

    #: The folder the face and orientation models used to live in, inside the
    #: state directory. Anything still there is moved into the shared model
    #: folder once, on start-up.
    OLD_MODELS_FOLDER = "models"

    def _gather_models(self, models: Path) -> None:
        """Bring models left in the state directory in with the rest.

        A household that has been running Ninaivu for a while has the face
        detector, the recogniser and the orientation model in
        ``<state>/models`` and everything else in ``.ai-models``. Left split,
        the promise this change makes — copy three things and Ninaivu works on
        the new machine — would be false for exactly the people who have been
        using it longest.

        Moved rather than copied, and never fatal: a model that cannot be
        moved is re-downloaded, which is slow but not wrong.
        """
        old = Path(self.cfg.state_dir) / self.OLD_MODELS_FOLDER
        log = logging.getLogger(__name__)
        try:
            if not old.is_dir() or old.resolve() == Path(models).resolve():
                return
            found = [path for path in old.iterdir() if path.is_file()]
        except OSError:
            return
        if not found:
            return
        moved = []
        for path in found:
            target = Path(models) / path.name
            try:
                if target.exists():
                    path.unlink()           # already here; the copy is spare
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
                moved.append(path.name)
            except OSError as exc:
                log.warning("could not move %s in with the other models: %s",
                            path.name, exc)
        if moved:
            log.info("moved %s into %s, so every model is in one folder",
                     ", ".join(sorted(moved)), models)
        try:
            old.rmdir()                     # only when nothing is left in it
        except OSError:
            pass

    def _prepare_new_files_folder(self) -> None:
        """Set up the new-files folder now if a library folder is read-only.

        A library folder that is connected but cannot be written — an NTFS
        drive on a Mac — sends new files to the new-files folder
        (ninaivu/storage/new_files.py). Made at start-up, not at the first save,
        so it is in the library folders and on the Performance page from the
        beginning, and the first scan includes it. A drive that is not plugged
        in is not read-only, only away, and a writable library needs nothing.
        """
        from .storage import new_files

        for root in list(self.cfg.roots):
            if Path(root).is_dir() and not new_files.writable(root):
                try:
                    new_files.destination(self.cfg, root)
                except OSError as exc:
                    logging.getLogger(__name__).warning(
                        "%s is read-only and no folder for new files could be made: %s",
                        root, exc)

    def start(self, rescan: bool = False) -> None:
        """Load the AI engine off the request path, then start the first scan.

        ``rescan`` forces a full re-index. It is sequenced inside boot because
        the engine may take minutes to become ready (a first-run model
        download): a rescan that starts earlier indexes the whole library
        without embeddings, and later incremental scans never repair that.
        """
        if self._started:
            return
        self._started = True
        self._prepare_new_files_folder()
        self.power.start()
        self.guardian.start()
        self.backups.start()
        self.updates.start()
        self.digest.start()
        self.disks.start()
        self.restore_tests.start()
        self.index_copy.start()

        def boot() -> None:
            engine = self._ai_mod.build_engine(self.cfg)
            bound = self._ai_mod.bind(engine, self.cfg)
            self.engine = bound
            self.scanner.ai = bound
            self._sweep_the_bin()
            # Before the library scan: a consolidation takes the disk, and a
            # scan asked for while it runs is queued rather than started and
            # then stood down a moment later. After the model, because the
            # archive asks it about borderline folders exactly as Start does.
            self._resume_archive()
            self._scan_the_libraries(rescan)
            if self.cfg.hide_screens:
                # Now, not at the end of a scan's indexing, which on a large
                # library is hours of the gallery still showing what was
                # already known to be a screenshot. Only items that have a
                # name or a vector are judged, so it does not wait on the scan.
                try:
                    self.scanner.apply_screen_rule()
                except Exception:                           # noqa: BLE001
                    logging.getLogger(__name__).exception(
                        "could not hide screenshots and documents")
            self._resume_jobs()
            self._warm_the_pages_the_gallery_asks_for()

        threading.Thread(target=boot, name="ninaivu-boot", daemon=True).start()

    def _warm_the_pages_the_gallery_asks_for(self) -> None:
        """Read the columns the first page load will need, off its path.

        Every query the gallery opens with is fast once the pages it touches
        are in the operating system's cache and slow before that, and the
        cost is the *size* of the column rather than the number of rows. The
        sidebar counts read 358 MB of vectors to count them until an index
        fixed that; the tag cloud still reads 12 MB of JSON, which on a cold
        cache was four and a half seconds of somebody looking at a page that
        had not drawn yet.

        Doing it here rather than caching the answers is deliberate: a cache
        has to be invalidated, and a wrong tag cloud is a worse failure than
        a slow one. This makes the first real request find what it needs
        already in memory, and is wrong about nothing.

        Never fatal, and last in the boot sequence — this is a courtesy, and
        it must not delay anything that matters.
        """
        from .storage import db                              # noqa: PLC0415

        log = logging.getLogger(__name__)
        roots = self.cfg.roots or (
            [self.cfg.active_root] if self.cfg.active_root else [])
        if not roots:
            return
        started = time.time()
        try:
            conn = db.connect(self.cfg.db_path)
            db.library_stats(conn, roots)
            db.facets(conn, roots)
        except Exception:                                    # noqa: BLE001
            log.debug("could not warm the gallery's first page", exc_info=True)
            return
        log.info("read the gallery's opening queries in %.1fs, so the first "
                 "page does not have to", time.time() - started)
        self._warm_search(conn)

    def _warm_search(self, conn) -> None:
        """Load the photographs' vectors for searching by description.

        The first search after a start read all of them from the index: 197,015
        vectors, 4.5 s of somebody waiting on a search that takes 10 ms once
        they are in memory, where they stay. They are loaded here instead, off
        every request's path. Nothing is held that the first search would not
        have held anyway.
        """
        from .storage import db                              # noqa: PLC0415

        if not self.cfg.ai_enabled:
            return
        started = time.time()
        try:
            ids, _, _, _ = db.embedding_store(conn)
        except Exception:                                    # noqa: BLE001
            logging.getLogger(__name__).debug("could not load the search vectors",
                                              exc_info=True)
            return
        if ids:
            logging.getLogger(__name__).info(
                "loaded %s photographs' search vectors in %.1fs, so the first "
                "search does not have to", f"{len(ids):,}", time.time() - started)

    def _resume_jobs(self) -> None:
        """Carry on the long jobs somebody started and nobody stopped.

        Each writes itself down when it starts and crosses itself off when it
        finishes or is stopped (storage/resume.py); a shutdown does neither,
        so what is still written down is exactly what a restart interrupted.
        The library scan and the archive have their own records and are
        picked up before this.
        """
        from .api import admin_api, ai_models_api                # noqa: PLC0415
        from .cloud import service as cloud_service              # noqa: PLC0415
        from .media import model_catalog, straighten             # noqa: PLC0415
        from .storage import db, resume                          # noqa: PLC0415

        log = logging.getLogger(__name__)
        conn = db.connect(self.cfg.db_path)
        try:
            wanted = resume.wanted(conn)
        except Exception:                                        # noqa: BLE001
            log.exception("could not read which jobs to carry on")
            wanted = {}

        def attempt(what: str, action, interrupted: bool = True) -> None:
            try:
                action()
                if interrupted:
                    log.info("%s was running when Ninaivu stopped; it has been "
                             "carried on", what)
                else:
                    log.info("%s started, as set to at start-up", what)
            except Exception:                                    # noqa: BLE001
                log.exception("could not carry on %s", what)

        # A cloud upload somebody started, or every start when they asked for
        # that. Starting an upload of a whole library just because the server
        # restarted is not a decision software gets to make on somebody's
        # broadband — carrying on one they started is not that.
        if self.cfg.cloud_enabled and (self.cfg.cloud_autostart
                                       or cloud_service.RESUME_NAME in wanted):
            attempt("the cloud backup", self.cloud.start,
                    interrupted=cloud_service.RESUME_NAME in wanted)
        elif cloud_service.RESUME_NAME in wanted:
            resume.done(conn, cloud_service.RESUME_NAME)   # switched off since

        job = wanted.get(straighten.RESUME_NAME)
        if job and job.get("job") == "survey":
            roots = self.cfg.libraries or ([self.cfg.active_root]
                                           if self.cfg.active_root else [])
            attempt("the straightening survey", lambda: self.straightener.survey(
                roots, limit=job.get("limit") or None, rescan=False,
                auto_apply=bool(job.get("auto_apply"))))
        elif job and job.get("job") == "apply":
            attempt("straightening", lambda: self.straightener.apply(
                [int(i) for i in job.get("ids") or []] or None,
                float(job.get("min_confidence") or 0.0),
                batch=int(job["batch"]) if job.get("batch") else None))

        if admin_api.SCRUBBER_RESUME in wanted:
            after = int(wanted[admin_api.SCRUBBER_RESUME].get("after_id") or 0)
            attempt("the storage check",
                    lambda: admin_api.start_scrubber_job(self.cfg.db_path, after,
                                                         scanner=self.scanner,
                                                         notify=self._tell_somebody))

        for name, args in wanted.items():
            if not name.startswith(ai_models_api.MODEL_RESUME_PREFIX):
                continue
            model_id = str(args.get("model_id") or name.split(":", 1)[1])
            if model_id not in model_catalog.MODELS or model_catalog.installed(model_id):
                resume.done(conn, name)
                continue
            # Whether it was a plain download or a "download again": a
            # forced fetch interrupted half way must carry on being forced,
            # or it would step over the very file it was replacing.
            again = bool(args.get("force"))
            attempt(f"the {model_id} model download",
                    lambda m=model_id, f=again: ai_models_api.start_model_download(
                        self.cfg.db_path, m, force=f))

    #: How long start-up keeps trying to pick up an interrupted archive job
    #: whose folders are not there yet — a network share still reconnecting, a
    #: USB disk still mounting after a reboot.
    ARCHIVE_RESUME_ATTEMPTS = 20
    ARCHIVE_RESUME_WAIT = 30.0

    def _resume_archive(self) -> None:
        """Carry on with a consolidation or audit Ninaivu stopped in the middle of.

        Stopping Ninaivu pauses the job so it ends cleanly, and nothing used to
        start it again: after a restart the archive sat idle until somebody
        noticed and pressed Start. It was started on purpose and has not
        finished, so it carries on, the same way Start would carry it on, and
        comes back paused if somebody had paused it.
        """
        from .archive import scanner as archive_scanner      # noqa: PLC0415
        log = logging.getLogger(__name__)

        def attempt() -> bool:
            """One try. True when there is nothing more to try."""
            from .api.archive_api import YIELD_LABELS, _yield_the_disk  # noqa: PLC0415
            engine = self._ai_mod.get_engine()
            vision = engine if callable(getattr(engine, "embed_images", None)) else None
            job, problems = archive_scanner.resume_interrupted(vision=vision)
            if job is None:
                return True
            if problems:
                log.info("the interrupted archive job %s cannot start yet: %s",
                         job["id"], "; ".join(problems))
                return False
            _yield_the_disk(self.scanner,
                            YIELD_LABELS.get(job["mode"], "an archive job is running"),
                            self.power)
            log.info("archive job %s was running when Ninaivu stopped; it has "
                     "been picked up again", job["id"])
            return True

        def keep_trying() -> None:
            for _ in range(self.ARCHIVE_RESUME_ATTEMPTS):
                time.sleep(self.ARCHIVE_RESUME_WAIT)
                try:
                    if attempt():
                        return
                except Exception:                            # noqa: BLE001
                    log.exception("could not resume the archive job")
                    return
            log.warning("gave up resuming the interrupted archive job; press "
                        "Start on the Archive page once its folders are back")

        try:
            if attempt():
                return
        except Exception:                                    # noqa: BLE001
            log.exception("could not resume the archive job")
            return
        threading.Thread(target=keep_trying, name="archive-resume",
                         daemon=True).start()

    def _sweep_the_bin(self) -> None:
        """Erase what the household said it was done with.

        Off unless somebody sets a number of days. It runs at start-up rather
        than on a timer because that is the moment nothing else is happening,
        and a bin a fortnight past its policy is not an emergency.
        """
        days = float(getattr(self.cfg, "bin_erase_after_days", 0) or 0)
        if days <= 0:
            return
        try:
            from .storage import db, recycle             # noqa: PLC0415
            from .media import media

            conn = db.connect(self.cfg.db_path)
            result = recycle.sweep(conn, days)
            for base in result.get("thumbs", []):
                try:
                    media.remove_thumbnails(self.cfg.thumbs_dir, base,
                                            self.cfg.thumb_sizes,
                                            self.cfg.thumb_format)
                except OSError:
                    pass
        except Exception:                                # noqa: BLE001
            logging.getLogger(__name__).exception(
                "could not sweep the recycle bin")

    def _scan_the_libraries(self, rescan: bool = False) -> list[str]:
        """Walk the library folders that need it, and watch all of them.

        Every library folder, not only the one the console has selected: a
        household with a second drive had it neither walked at start-up nor
        watched afterwards, so its new photographs waited for Rescan. Returns
        the folders a scan was started for.
        """
        due = [root for root in self.cfg.libraries
               if rescan or self._scan_worth_it(root)]
        if due:
            self.scanner.start(due, full=rescan)
        else:
            # Nothing to walk — the library was scanned moments before a
            # restart — but still something to watch. Skipping the walk is
            # only safe because a watcher notices whatever arrives next.
            self.scanner.watch()
        return due

    def _scan_worth_it(self, root: str) -> bool:
        """Is a walk of the library worth doing right now?

        Starting Ninaivu used to start a scan, always. It skips unchanged files,
        so it costs nothing in thumbnails — but it still walks every folder,
        which on an external drive is minutes of spinning and a progress strip
        in front of the family every time the machine reboots.

        A restart is the case worth skipping: the watcher was live until the
        moment the process stopped, a scan finished shortly before that, and
        the seconds in between are not enough for the library to have changed
        underneath us. Anything longer — an overnight shutdown, a laptop that
        was away for a week, a scan that died half way — is exactly when a walk
        earns its keep, so it happens.
        """
        from .storage import db                             # noqa: PLC0415

        if not self.cfg.watch:
            return True                     # nothing was watching; look properly
        window = float(getattr(self.cfg, "boot_scan_after", 900) or 0)
        if window <= 0:
            return True
        try:
            last = db.last_scan(db.connect(self.cfg.db_path), str(root))
        except Exception:                                # noqa: BLE001
            return True
        # The latest scan, not the latest finished one, and only if it got to
        # the end. Stopping the server closes the scan it interrupts as `idle`,
        # and reading that as "finished a moment ago" is how a restart in the
        # middle of indexing used to leave it stopped until somebody pressed
        # Rescan. An earlier scan that did finish says nothing about the files
        # the interrupted one never reached.
        if not last or not last.get("ended_at") or last.get("status") != "done":
            return True
        # Analysis still owed is work a scan does, whatever the last scan
        # says about itself. A scan stopped mid-analysis used to record itself
        # as done, so this also covers a library left that way.
        if self.cfg.ai_enabled and self._analysis_owed(root):
            return True
        quiet = time.time() - float(last["ended_at"])
        if quiet > window:
            if not self._stayed_connected(root, last):
                return True
            logging.getLogger(__name__).info(
                "skipping the start-up scan of %s: this computer cannot write to it, "
                "and it has stayed connected since a scan finished %d minutes ago",
                root, int(quiet // 60))
            return False
        logging.getLogger(__name__).info(
            "skipping the start-up scan: one finished %d seconds ago", int(quiet))
        return False

    #: Where macOS mounts a drive, one entry for each; mounting or unmounting
    #: one moves this folder's own modification time.
    VOLUMES = Path("/Volumes")

    def _stayed_connected(self, root: str, last: dict) -> bool:
        """Whether a read-only library cannot have changed since *last* began.

        A drive this computer cannot write to (NTFS on a Mac) changes only while
        it is somewhere else: unplugged, changed, plugged back in. Each restart
        walked it anyway, four minutes of a USB drive to find nothing new. If
        /Volumes has not changed since the last scan of it began, the drive has
        been here, unwritable, all along. Anything else mounted or unmounted in
        between moves it too, which only costs a scan that was not needed.
        """
        if sys.platform != "darwin":
            return False
        from .storage import new_files                       # noqa: PLC0415

        try:
            if self.VOLUMES not in Path(root).parents or new_files.writable(root):
                return False
            started = float(last.get("started_at") or 0)
            return started > 0 and self.VOLUMES.stat().st_mtime < started
        except OSError:
            return False

    def _analysis_owed(self, root: str) -> bool:
        """Are there items in this folder the image model has not looked at yet?"""
        from .media.scanner import AI_VERSION                # noqa: PLC0415
        from .storage import db                              # noqa: PLC0415

        try:
            # As the scanner's tagging asks: a sound file's picture is a cover
            # or a drawn tile, never tagged. Counted here, the 12,861 sound files
            # of a library made every restart walk a whole external drive.
            row = db.connect(self.cfg.db_path).execute(
                "SELECT 1 FROM assets WHERE root=? AND trashed=0 "
                "AND thumb IS NOT NULL AND ai_version < ? AND kind != 'audio' LIMIT 1",
                (str(root), AI_VERSION)).fetchone()
        except Exception:                                    # noqa: BLE001
            return False
        return row is not None

    def stop(self, timeout: float = 20.0) -> list[str]:
        """Bring everything to a stop that can be picked up again.

        Every long-running job in Ninaivu already survives the process being
        killed — the scan checkpoints per file, the archive keeps its progress
        in SQLite and renames whole files into place, the cloud upload holds a
        resumable session. So this is not about preventing loss. It is about
        not *leaving work on the floor*: a job asked to stop writes down where
        it got to and deletes the temporary file it was part-way through,
        where the same job killed mid-write leaves both to be worked out
        again on the next run.

        Each part is asked separately and a part that will not stop is
        reported rather than waited on forever, because a stop that hangs is
        worse than a stop that says what it could not close.
        """
        problems: list[str] = []

        def attempt(what: str, action) -> None:
            try:
                action()
            except Exception as exc:                        # noqa: BLE001
                problems.append(f"{what}: {exc}")

        # The scan first, and joined: it is the one holding the database open
        # for writing, so letting it finish the file it is on is what makes
        # the index consistent at the moment the process ends.
        attempt("the library scan", lambda: self.scanner.stop(join=True))
        attempt("the straightening pass",
                lambda: self.straightener.stop(join=True))
        # Paused, not stopped. Pausing keeps the resumable upload session and
        # records the file as still waiting; stopping would count an orderly
        # shutdown as a failed attempt, and five quiet nights of that turns a
        # large video into a file that has "failed" too often to retry.
        attempt("the cloud backup", self.cloud.pause)
        attempt("the restore from Google Drive", self.cloud.restore_stop)
        attempt("the archive run", self._pause_archive)
        attempt("the archive guardian", self.guardian.stop)
        attempt("the index backup", self.backups.stop)
        attempt("the update check", self.updates.stop)
        attempt("the weekly photograph", self.digest.stop)
        attempt("the drive watch", self.disks.stop)
        attempt("the test restore", self.restore_tests.stop)
        attempt("the copy of the index", self.index_copy.stop)
        attempt("the power policy", self.power.stop)
        return problems

    @staticmethod
    def _pause_archive() -> None:
        from .archive import scanner as archive_scanner

        if archive_scanner.is_scanning():
            archive_scanner.pause_scan()


def build_services(cfg: Config | None = None, **overrides: Any) -> Services:
    return Services(cfg or Config.load(**overrides))


# ---------------------------------------------------------------------------

def _remote_access(services: Services):
    """The household's remote-access provider, resolved once a minute at most:
    it may look for Tailscale's command and read a saved name."""
    from .server import remote                            # noqa: PLC0415
    cached = getattr(services, "_remote", None)
    now = time.monotonic()
    if cached is None or now - cached[0] > 60:
        cached = (now, remote.resolve(services.cfg))
        services._remote = cached
    return cached[1]


def _base_app(services: Services, face: str, template: str) -> Flask:
    from .server import auth
    from .server import workload as workload_mod
    from .storage import db

    cfg = services.cfg
    # Gallery and console can each own video conversions; check both.
    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
        static_url_path="/static",
    )
    services._app_configs.append(app.config)
    app.config.update(
        MV_CONFIG=cfg,
        MV_SERVICES=services,
        MV_SCANNER=services.scanner,
        MV_STRAIGHTEN=services.straightener,
        MV_POWER=services.power,
        MV_GUARDIAN=services.guardian,
        MV_BACKUPS=services.backups,
        APP_NAME=APP_NAME,
        NINAIVU_FACE=face,
        NINAIVU_TEMPLATE=template,
        JSON_SORT_KEYS=False,
        # Both of the next two were set unconditionally, which is a debug
        # posture: every render stats the template, every asset is fetched
        # again. The bug they were fixing was real — a cached script running
        # against fresh markup throws and takes every control wired after it
        # down — but the fix for that is the version stamp on the asset URLs
        # below, not turning caching off for the household for ever.
        SEND_FILE_MAX_AGE_DEFAULT=0 if cfg.debug else 31536000,
        # Templates are compiled once and cached by default, while the CSS and
        # JavaScript beside them are served with no-cache and update on the
        # next reload. That split is worse than either choice on its own: after
        # editing a page the browser runs the new script against the old
        # markup, and a handler bound to an element that does not exist yet
        # throws and takes every control wired after it down with it. The stat
        # per render costs nothing next to reading and hashing photographs.
        TEMPLATES_AUTO_RELOAD=cfg.debug,
        # Applies to every request body, /api/upload included -- 8 MiB was
        # fine for JSON API calls but silently rejected any real photo and
        # every video, since nothing on this route ever raised it.
        # Configurable so an admin can size it to what their household shoots.
        MAX_CONTENT_LENGTH=cfg.max_upload_mb * 1024 * 1024,
    )

    # Behind a reverse proxy every request arrives from 127.0.0.1 over plain
    # HTTP, whatever the household actually connected to. Both configs Ninaivu
    # ships set X-Forwarded-For and X-Forwarded-Proto faithfully; this is what
    # makes the application read them, and it is deliberately opt-in — see
    # Config.trusted_proxies for why trusting them unasked would be worse than
    # not trusting them at all.
    hops = int(getattr(cfg, "trusted_proxies", 0) or 0)
    if hops > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops,
                                x_host=hops, x_prefix=0)

    # The engine is loaded asynchronously; the before_request below re-reads
    # it from the services object on every request, so both apps see it the
    # moment it is ready.
    app.config["MV_ENGINE"] = None

    @app.context_processor
    def _asset_versions():
        """`asset(path)` — a static URL that changes when the file does.

        This is what makes long caching safe: the browser may keep
        /static/js/app.js for a year, because editing it produces a different
        URL rather than the same one with different contents.
        """
        import hashlib as _hashlib                   # noqa: PLC0415

        cache: dict[str, str] = app.config.setdefault("MV_ASSET_V", {})

        def asset(path: str) -> str:
            if cfg.debug:
                return f"{path}?v={time.time():.0f}"
            if path not in cache:
                whole = Path(app.static_folder or "") / path.removeprefix("/static/")
                try:
                    digest = _hashlib.sha256(whole.read_bytes()).hexdigest()[:12]
                except OSError:
                    digest = __version__
                cache[path] = digest
            return f"{path}?v={cache[path]}"

        # Only the Windows build carries the Tamil font file; every other
        # build would ask for it and get a 404 on every page. Looked at per
        # page rather than once, so a font dropped in later is picked up.
        tamil_font = (Path(app.static_folder or "") / "fonts" / "NotoSansTamil.ttf").is_file()

        # `map_tiles` rides along here because the page needs it before any
        # JavaScript runs and there is no inline script to put it in — the
        # policy above forbids one, deliberately.
        return {"asset": asset, "map_tiles": bool(getattr(cfg, "map_tiles", False)),
                "tamil_font": tamil_font, "about": about()}

    @app.before_request
    def _refuse_unknown_hosts():
        """Answer only to names a household uses for its own server.

        The defence against DNS rebinding: see ninaivu/server/hosts.py.
        """
        from .server import hosts                            # noqa: PLC0415
        name = hosts.checked_host(request.headers, request.host, cfg)
        if hosts.allowed(name, cfg):
            return None
        return jsonify({
            "error": (f"Ninaivu does not answer to the name {name!r}. If that is a name "
                      "you set up for it (a tunnel or a reverse proxy), add it to "
                      "allowed_hosts on All settings, or set remote_hostname."),
            "status": 421}), 421

    @app.before_request
    def _refuse_cross_origin_writes():
        """No state-changing request from a page Ninaivu did not serve.

        The session cookies are SameSite=Lax, which stops a page on another
        *site* — but every port on this machine is the same site, so a page
        served by anything else here (another app, an AI server) could post a
        form to the console and the browser would attach the admin's cookie.
        Endpoints that take no body accepted such a form as readily as the
        console's own fetch. The browser says where a request came from; a
        write that came from anywhere but this origin is refused.

        Requests carrying neither header are not from a browser page (scripts,
        stop.bat, the test client) and have no ambient cookie to abuse.
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None:
            if site in ("same-origin", "none"):
                return None
            return jsonify({"error": "Cross-origin request refused.",
                            "status": 403}), 403
        origin = request.headers.get("Origin")
        if not origin:
            return None
        from urllib.parse import urlsplit
        # Compared by host and port only: behind a TLS-terminating proxy the
        # scheme the app sees is not the one the browser used.
        allowed = {request.host.lower()}
        # The forwarded name only when a proxy is known to be there: without
        # one, any client can write that header.
        if int(getattr(cfg, "trusted_proxies", 0) or 0) > 0:
            allowed.update(h.strip().lower() for h in
                           request.headers.get("X-Forwarded-Host", "").split(",") if h.strip())
        if origin != "null" and urlsplit(origin).netloc.lower() in allowed:
            return None
        return jsonify({"error": "Cross-origin request refused.", "status": 403}), 403

    @app.before_request
    def _identify():
        app.config["MV_ENGINE"] = services.engine
        workload = getattr(services, "workload", None)
        if face != FACE_ADMIN and workload is not None:
            # Somebody using the family app. The console is not counted: an
            # administrator watching a scan is not a household to make way for.
            workload.noticed(request.path,
                             ranged=bool(request.headers.get("Range")))
        if workload is not None and request.path.startswith(workload_mod._MEDIA) \
                and workload_mod.from_outside(
                    request.remote_addr, request.headers,
                    int(getattr(cfg, "trusted_proxies", 0) or 0),
                    outside_networks=_remote_access(services).outside_networks):
            # Family app or console: either way, its answers go up the house's
            # internet connection, and the backup makes way (workload.py).
            workload.noticed_outside(request.path)
        g.face = face
        conn = db.connect(cfg.db_path)
        g.user = auth.load_user(conn, face)

        # Locked for inactivity: still signed in, so what the page is doing
        # carries on, but nothing else is answered until it is unlocked. See
        # the screen lock in server/auth.py.
        g.locked = False
        lock_after = max(0, int(getattr(cfg, "lock_after_minutes", 0) or 0)) * 60
        if g.user.id and lock_after:
            lock = auth.lock_state(conn, g.get("session_token", ""), lock_after)
            if lock and lock["locked"]:
                g.locked = True
                if not auth.open_while_locked(request.path, request.method):
                    return jsonify({
                        "error": "Ninaivu is locked. Unlock it to carry on.",
                        "locked": True,
                        "status": 423,
                    }), 423

        if face == FACE_ADMIN:
            # The console is staff-only: anonymous callers get the login page
            # and nothing else.
            path = request.path
            open_paths = (
                path == "/"
                or path.startswith("/static/")
                or path.startswith("/api/auth/")
                or path == "/healthz"
                or path == "/readyz"
                or path == "/ninaivu-ca.crt"
                or path == "/cert"
                or path == "/manifest.webmanifest"
                or path == "/sw.js"
                # Stopping the server, asked for by a script on this machine.
                # It has no session to offer and no password to type; it
                # proves itself with the token from the run file instead, and
                # the route refuses anything not coming from loopback and
                # answers 404 rather than 403 to everything else. See
                # admin_api.shutdown.
                or path == "/api/admin/shutdown"
            )
            if not g.user.is_admin and not open_paths:
                return jsonify({
                    "error": "Administrator sign-in required.",
                    "status": 401,
                }), 401
            return None

        # Home app: closed library means sign in before anything loads.
        if g.user.id == 0 and not cfg.open_browsing:
            path = request.path
            allowed = (
                path == "/"
                or path.startswith("/static/")
                or path.startswith("/api/auth/")
                or path.startswith("/api/share/")
                or path.startswith("/share/")
                or path == "/sw.js"
                or path == "/healthz"
                or path == "/readyz"
                or path == "/ninaivu-ca.crt"
                or path == "/cert"
                or path == "/manifest.webmanifest"
            )
            if not allowed:
                return jsonify({
                    "error": "This library is private. Please sign in.",
                    "status": 401,
                }), 401
        return None

    @app.after_request
    def _headers(response):  # noqa: ANN001
        # A fingerprinted URL may be kept forever. Nothing else may.
        #
        # The version stamp is what makes long caching safe: editing the file
        # produces a different URL rather than the same one with different
        # contents. So a `?v=` that matches what this process computed is the
        # one case where a year is honest, and it is the only one.
        #
        # Everything else has to revalidate, because the template can only
        # stamp what the template names. Two kinds of asset it never names:
        # the modules the entry point `import`s, and the files the scripts
        # fetch for themselves — `/static/css/leaflet.css` and
        # `/static/data/world.geojson`, both hardcoded in app.js. Those are
        # requested bare, so they can never match a fingerprint, and a
        # year-long default froze them in the browser with no way to shift
        # them: the map drew into a container whose stylesheet was a year old
        # and showed a grey rectangle. Guarding only `/static/js/` here is
        # what let that through.
        #
        # `no-cache` means "revalidate", not "do not store", so an unchanged
        # file still costs a 304 and not a download.
        if request.path.startswith("/static/"):
            asked = request.args.get("v")
            current = app.config.get("MV_ASSET_V", {}).get(request.path)
            if asked and current and asked == current:
                response.headers["Cache-Control"] = \
                    "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Nothing in Ninaivu uses these; saying so means a script that got in
        # some other way cannot ask for them either.
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()")
        # The tile servers are named here only when the household has asked
        # for tiles. Left in unconditionally, the policy would permit the one
        # request Ninaivu exists to avoid on every installation that never
        # turns the map on — and a Content-Security-Policy is worth more as a
        # statement of what may happen than as a list of what might.
        tiles = (" https://*.tile.openstreetmap.org https://tile.openstreetmap.org"
                 if getattr(cfg, "map_tiles", False) else "")
        response.headers.setdefault(
            "Content-Security-Policy",
            f"default-src 'self'; img-src 'self' data: blob:{tiles}; "
            "media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
            f"script-src 'self'; connect-src 'self'{tiles}; font-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        # What a request may see depends on who is signed in, so responses
        # must never be reused across sessions by a shared cache.
        if request.path.startswith("/api/"):
            response.headers.setdefault("Vary", "Cookie")
            from .server import date_policy
            if face == FACE_HOME and date_policy.restricted():
                response.headers["Cache-Control"] = "private, no-store"
        _gzip_text(response)
        return response

    @app.before_request
    def _unsuffix_etags():
        # A browser given the gzipped file sends its ETag back with the "-gz"
        # suffix _gzip_text added; the file's own conditional check has to
        # see the bare one, or every revisit is a download instead of a 304.
        given = request.environ.get("HTTP_IF_NONE_MATCH")
        if given and "-gz" in given:
            request.environ["HTTP_IF_NONE_MATCH"] = given.replace('-gz"', '"')
        return None

    return app


#: A text answer smaller than this goes as it is: a status line gains nothing
#: from gzip that pays for its header and the time.
_GZIP_MIN_BYTES = 2048
#: Nothing larger is compressed on the fly: a large file sent as text by
#: mistake would otherwise be read whole into memory.
_GZIP_MAX_BYTES = 8 * 1024 * 1024
#: What is worth compressing: pictures and videos are compressed already.
_GZIP_TYPES = ("text/", "application/json", "application/javascript",
               "image/svg+xml", "application/manifest+json")
#: Static files are compressed once per version, at the best level, and kept.
_gzip_static: dict[str, tuple[str, bytes]] = {}
_gzip_static_lock = threading.Lock()
_GZIP_STATIC_LIMIT = 96


def _gzip_text(response) -> None:  # noqa: ANN001
    """Compress a text answer for a client that says it can take one.

    The gallery's layout for 25,000 items is 1.2 MB of JSON and 140 KB
    gzipped, for about 13 ms of work. On the household's Wi-Fi either arrives
    quickly; on a phone reaching Ninaivu over the tailnet from outside the
    house, it is the difference between half a second a page and a twentieth
    of one. API answers are compressed as they go, at level 5 because the
    levels above it cost twice the time for a few per cent. The pages,
    scripts, styles and the Tamil strings — a few megabytes on a phone's
    first visit — are compressed once per version at level 9 and kept
    (from Ninaivu Lite).
    """
    import gzip

    mimetype = response.mimetype or ""
    if (response.status_code != 200
            or (response.is_streamed and not response.direct_passthrough)
            or "Content-Encoding" in response.headers
            or not mimetype.startswith(_GZIP_TYPES)
            or request.accept_encodings["gzip"] <= 0):
        return
    length = response.content_length
    if length is not None and not _GZIP_MIN_BYTES <= length <= _GZIP_MAX_BYTES:
        return
    response.direct_passthrough = False
    data = response.get_data()
    if not _GZIP_MIN_BYTES <= len(data) <= _GZIP_MAX_BYTES:
        return
    static = request.path.startswith("/static/")
    etag = response.get_etag()[0] or ""
    if static and etag:
        with _gzip_static_lock:
            hit = _gzip_static.get(request.path)
        if hit and hit[0] == etag:
            packed = hit[1]
        else:
            packed = gzip.compress(data, compresslevel=9, mtime=0)
            with _gzip_static_lock:
                if len(_gzip_static) >= _GZIP_STATIC_LIMIT:
                    _gzip_static.pop(next(iter(_gzip_static)))
                _gzip_static[request.path] = (etag, packed)
    else:
        packed = gzip.compress(data, compresslevel=9 if static else 5, mtime=0)
    if len(packed) >= len(data):
        return
    response.set_data(packed)
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    if etag:
        response.set_etag(f"{etag}-gz")


def create_home_app(services: Services) -> Flask:
    """Port 80 by default: the gallery, for family members and guests."""
    from .api.accounts_api import accounts, home_accounts
    from .api import bp

    app = _base_app(services, FACE_HOME, "index.html")
    app.register_blueprint(bp)
    app.register_blueprint(accounts)
    app.register_blueprint(home_accounts)
    extensions.install(app, services.cfg, FACE_HOME)
    return app


def create_admin_app(services: Services) -> Flask:
    """Port 3000: the admin console."""
    from .api.accounts_api import accounts, admin_accounts
    from .api.admin_api import admin_bp
    from .api import admin_only, bp
    from .api.archive_api import archive_bp
    from .api.cloud_api import cloud_bp
    from .api.ai_models_api import ai_models_bp
    from .api.components_api import components_bp
    from .api.migration_api import migration_bp
    from .api.server_api import server_bp

    app = _base_app(services, FACE_ADMIN, "admin.html")
    # The console reuses the media API (for the preview and per-item
    # visibility) plus every library-level and management endpoint.
    app.register_blueprint(bp)
    app.register_blueprint(admin_only)
    app.register_blueprint(accounts)
    app.register_blueprint(admin_accounts)
    app.register_blueprint(admin_bp)
    # Consolidating drives writes gigabytes and can enumerate every disk on
    # the machine: console only, never the family port.
    app.register_blueprint(archive_bp)
    # Cloud backup hands out a Google consent URL and can copy the household's
    # photographs off the premises. Console only, for the same reason.
    app.register_blueprint(cloud_bp)
    # The AI models tab downloads model files onto this machine.
    app.register_blueprint(ai_models_bp)
    # Migration rewrites every path in the index and renames every thumbnail.
    # Console only, and admin-only within it, for reasons that need no stating.
    app.register_blueprint(migration_bp)
    # The Extras tab installs ffmpeg and Ninaivu's optional packages on this
    # machine, which is why it is here and not on the family port.
    app.register_blueprint(components_bp)
    # The Server page restarts and stops the whole of Ninaivu.
    app.register_blueprint(server_bp)
    extensions.install(app, services.cfg, FACE_ADMIN)
    return app


def create_app(cfg: Config | None = None, **overrides: Any) -> Flask:
    """Backwards-compatible single-app factory (the home app, all features).

    Used by the test suite and by anyone who wants one port instead of two.
    """
    services = build_services(cfg, **overrides)
    from .api.accounts_api import accounts, admin_accounts, home_accounts
    from .api.admin_api import admin_bp
    from .api import admin_only, bp
    from .api.archive_api import archive_bp
    from .api.cloud_api import cloud_bp
    from .api.ai_models_api import ai_models_bp
    from .api.components_api import components_bp
    from .api.server_api import server_bp

    app = _base_app(services, FACE_HOME, "index.html")
    app.register_blueprint(bp)
    app.register_blueprint(admin_only)
    app.register_blueprint(accounts)
    app.register_blueprint(home_accounts)
    app.register_blueprint(admin_accounts)
    app.register_blueprint(admin_bp)
    app.register_blueprint(archive_bp)
    app.register_blueprint(cloud_bp)
    app.register_blueprint(ai_models_bp)
    app.register_blueprint(components_bp)
    app.register_blueprint(server_bp)
    extensions.install(app, services.cfg, FACE_HOME)
    services.start()
    return app
