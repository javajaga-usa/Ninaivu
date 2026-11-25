"""What a start-up scan owes, and whether it is worth doing (P12).

These are the questions `Services.start` asks before it walks a library: which
folders the drive says changed, whether a walk is worth it, and whether any
analysis or pass is still owed. They read only `self.cfg`, `self.scanner` and
the index, so they live here as a mixin and `Services` inherits them.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path


class ScanPlanning:
    """Mixed into `Services`: the start-up scan's decisions."""

    def _scoped_by_the_drive(self, roots: list[str]) -> dict[Path, frozenset[str]]:
        """The folders of each of *roots* the drive says changed since the
        last time all of them had been read, for the ones whose drive keeps a
        record it can trust (storage/journal.py, storage/fsevents.py). A root
        with nothing changed maps to no folders and is not read; the rest are
        left out and walked as before."""
        from .media.scanner import SCOPE_MAX_FOLDERS        # noqa: PLC0415
        from .storage import db, journal                     # noqa: PLC0415

        if not journal.enabled(self.cfg) or sys.platform != "darwin":
            return {}
        found: dict[Path, frozenset[str]] = {}
        try:
            conn = db.connect(self.cfg.db_path)
        except Exception:                                    # noqa: BLE001
            return {}
        for root in roots:
            try:
                plan = journal.plan_start_up(conn, root)
            except Exception:                                # noqa: BLE001 - walk it instead
                logging.getLogger(__name__).info(
                    "could not ask the drive what changed in %s", root, exc_info=True)
                plan = None
            if plan is None or len(plan.folders) > SCOPE_MAX_FOLDERS:
                continue
            if not plan.folders:
                journal.remember_mark(conn, root, plan.mark, whole=False)
                logging.getLogger(__name__).info(
                    "not reading %s at start-up: the drive says nothing in it "
                    "changed while Ninaivu was not running", root)
            else:
                self.scanner.journal_marks[str(root)] = (plan.folders, plan.mark)
                logging.getLogger(__name__).info(
                    "reading %d folders of %s at start-up, the ones the drive "
                    "says changed while Ninaivu was not running", len(plan.folders), root)
            found[Path(root)] = plan.folders
        return found

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
        """Are there items in this folder the image model has not looked at yet?

        Not counting the ones the last full tagging pass could not look at —
        a thumbnail gone from the cache, a batch the model refused — while
        they are still the same ones (Scanner.TAG_LEFT). Those stayed owed
        for ever, so every restart walked the whole library to try them again.
        """
        from .media.scanner import AI_VERSION, TAG_LEFT      # noqa: PLC0415
        from .storage import db                              # noqa: PLC0415

        try:
            conn = db.connect(self.cfg.db_path)
            # As the scanner's tagging asks: a sound file's picture is a cover
            # or a drawn tile, never tagged. Counted here, the 12,861 sound files
            # of a library made every restart walk a whole external drive.
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM assets WHERE root=? "
                "AND trashed=0 AND thumb IS NOT NULL AND ai_version < ? "
                f"AND {db.AI_MAY_READ} AND kind != 'audio'",
                (str(root), AI_VERSION)).fetchone()
            if not row or not row[0]:
                return False
            return db.get_meta(conn, TAG_LEFT.format(root=root)) != f"{row[0]}:{row[1]}"
        except Exception:                                    # noqa: BLE001
            return False

    def _roots_owing_analysis(self) -> list[str]:
        """The library folders whose analysis is owed, for the daily catch-up."""
        return [root for root in self.cfg.libraries
                if (self.cfg.ai_enabled and self._analysis_owed(root))
                or self._passes_owed(root)]

    def _work_left(self, root: str) -> bool:
        """Whether the last scan of *root* stopped before its end, or its
        analysis is still owed: what a folder nobody needs to read again can
        still owe."""
        from .storage import db                              # noqa: PLC0415

        try:
            last = db.last_scan(db.connect(self.cfg.db_path), str(root))
        except Exception:                                    # noqa: BLE001
            last = None
        if not last or last.get("status") != "done":
            return True
        return bool((self.cfg.ai_enabled and self._analysis_owed(root))
                    or self._passes_owed(root))

    def _passes_owed(self, root: str) -> bool:
        """Whether place names, text, faces or fingerprints that are switched on still owe
        work in this folder that their last finished pass did not leave
        (scanner.still_owed): a pass that ended waiting for its turn, while
        the scan called itself done. Or people found and never grouped."""
        from .media import scanner as scanner_mod            # noqa: PLC0415
        from .storage import db                              # noqa: PLC0415

        wanted = [kind for kind, setting in (("places", "place_names"),
                                             ("text", "ocr_enabled"),
                                             ("faces", "faces_enabled"),
                                             ("fingerprints", "content_fingerprints"))
                  if getattr(self.cfg, setting, False)]
        if not wanted:
            return False
        try:
            conn = db.connect(self.cfg.db_path)
            if ("faces" in wanted
                    and db.get_meta(conn, f"faces_ungrouped:{root}") == "1"):
                return True
            return any(scanner_mod.still_owed(conn, root, kind) for kind in wanted)
        except Exception:                                    # noqa: BLE001
            return False

    def _read_through(self, root: str) -> bool:
        """Whether the last scan of *root* read all of it, moments ago, and
        stopped only in its analysis — so a restart owes just the analysis.

        Moments: inside the same window that lets start-up skip a scan
        altogether, with the watcher live until the process stopped. A scan
        that died without closing its run says nothing about when it died.
        """
        from .storage import db                              # noqa: PLC0415

        if not self.cfg.watch:
            return False
        window = float(getattr(self.cfg, "boot_scan_after", 900) or 0)
        try:
            conn = db.connect(self.cfg.db_path)
            last = db.last_scan(conn, str(root))
            if not last or not last.get("ended_at"):
                return False
            if time.time() - float(last["ended_at"]) > window:
                return False
            return db.get_meta(conn, f"read_through:{root}") == str(last["id"])
        except Exception:                                    # noqa: BLE001
            return False
