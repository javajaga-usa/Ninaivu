"""Straightening a library that was indexed before anybody looked at it.

The scanner decides which way up a photograph goes as it indexes it. That is
the right place for the decision and the wrong place for a *correction*: a
library indexed last year carries last year's answer, and re-scanning will not
revisit it, because a rescan only looks at files whose size or timestamp
changed. Nothing about a photograph changes when the software gets better at
reading it.

So this is a pass of its own, and it runs in two halves on purpose.

**The survey** looks at every photograph the household has never had an
opinion about, asks the model which way up it goes, and writes the answers
down without touching a single file or thumbnail. Nothing visible changes.
At the end there is a list to look at.

**The application** takes the answers that were approved and does the work:
the index records the turn, the thumbnails are baked the right way up, and the
original file on disk is left exactly as it was found. Every applied turn
remembers what it replaced, so the whole batch can be put back.

Splitting it this way is what makes the feature safe to run on a library of
thousands. A model that is right 99 times in 100 is still wrong about thirty
photographs in three thousand, and the difference between a good feature and a
bad one is whether those thirty are seen before they are baked or discovered
months later.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from ..storage import db, resume
from . import media, upright

log = logging.getLogger(__name__)

#: How a survey or an apply is remembered across a restart (storage/resume.py).
#: One name for both, as only one of them runs at a time.
RESUME_NAME = "straighten"

__all__ = ["init_schema", "Straightener", "SCHEMA"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS orientation_proposals (
    asset_id     INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    rotation     INTEGER NOT NULL,
    confidence   REAL    NOT NULL DEFAULT 0,
    source       TEXT    NOT NULL DEFAULT 'model',
    -- what was true before, so an applied batch can be put back exactly
    prev_rotation   INTEGER NOT NULL DEFAULT 0,
    prev_rot_source TEXT    NOT NULL DEFAULT 'none',
    status       TEXT    NOT NULL DEFAULT 'pending',   -- pending|applied|dismissed
    surveyed_at  REAL    NOT NULL DEFAULT 0,
    applied_at   REAL,
    batch        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orient_status ON orientation_proposals(status, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_orient_batch  ON orientation_proposals(batch);
-- Which photographs the survey has judged, and as which indexing of them. A
-- photograph the model found upright leaves no proposal behind, so without
-- this every survey — and the one after every scan — put the whole library
-- through the model again to learn nothing. Keyed by the indexing, not an id
-- watermark: a drive that was away, a file that could not be read this time,
-- and a file that changed and was re-indexed are all looked at again.
CREATE TABLE IF NOT EXISTS orientation_seen (
    asset_id    INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    indexed_at  REAL NOT NULL
);
"""


def init_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


class Progress:
    """What the console shows while a pass is running."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset("idle")

    def reset(self, status: str = "idle") -> None:
        with self._lock:
            self.status = status          # idle|surveying|applying|done|error|stopped
            self.phase = ""
            self.total = 0
            self.processed = 0
            self.proposed = 0
            #: Looked at by an earlier run, before a restart: counted as done.
            self.earlier = 0
            self.no_person = 0
            self.applied = 0
            self.skipped = 0
            self.errors = 0
            self.message = ""
            self.started_at = 0.0
            self.ended_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        done = data["status"] in {"idle", "done", "error", "stopped"}
        total = data["total"] or 1
        data["percent"] = 100 if done else min(99, int(data["processed"] * 100 / total))
        data["running"] = not done
        data["elapsed"] = round(
            (data["ended_at"] or time.time()) - data["started_at"], 1
        ) if data["started_at"] else 0.0
        return data

    def _set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def _bump(self, **fields: int) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, getattr(self, key) + value)



def _file_orientation(path: Path) -> Any:
    """The EXIF orientation the file itself carries, or None when it carries none."""
    try:
        with Image.open(path) as img:
            return (img.getexif() or {}).get(274)
    except Exception:                                       # noqa: BLE001
        return None


#: Photographs nobody has expressed an opinion about. A turn somebody set by
#: hand, or one already applied from a previous survey, is left alone — the
#: whole point is that this never overwrites a human decision.
_CANDIDATES = (
    "SELECT id, root, rel_path, thumb, rotation, rot_source, orientation, indexed_at "
    "FROM assets "
    "WHERE kind='picture' AND trashed=0 AND rot_source IN ('none','') "
    "AND root IN ({roots}) "
    "AND NOT EXISTS (SELECT 1 FROM orientation_seen s "
    "                WHERE s.asset_id = assets.id AND s.indexed_at = assets.indexed_at) "
    "ORDER BY id"
)

#: The photographs an earlier run already looked at, of the ones a survey
#: covers: what it carries on from after a restart.
_ALREADY_SEEN = (
    "SELECT COUNT(*) FROM assets "
    "WHERE kind='picture' AND trashed=0 AND rot_source IN ('none','') "
    "AND root IN ({roots}) "
    "AND EXISTS (SELECT 1 FROM orientation_seen s "
    "            WHERE s.asset_id = assets.id AND s.indexed_at = assets.indexed_at)"
)

#: How often, at most, what a survey has looked at is written down. A restart
#: that is not an orderly one (a power cut, a stop that had to be forced) loses
#: only what was looked at since.
REMEMBER_EVERY = 10.0

#: The most photographs judged at once, however large the machine.
MAX_READERS = 6


def room_beside_the_scan(cfg: Any, scanner: Any) -> bool:
    """Whether a job that reads the library can run beside the scan's
    analysis on this machine (see :meth:`Straightener._room_beside_the_scan`):
    the image model on a graphics processor, at least two survey readers, and
    every library folder on a solid-state disk. Anything not known to be so
    is not."""
    if readers_for(cfg) < 2:
        return False
    ai = getattr(scanner, "ai", None)
    if getattr(ai, "reads_ahead", False) is not True:
        return False
    try:
        from ..server import capacity                # noqa: PLC0415

        roots = [str(r) for r in (getattr(cfg, "library_roots", None) or [])]
        return bool(roots) and all(
            capacity.storage(r, "library").get("solid_state") is True
            for r in roots[:4])
    except Exception as exc:                        # noqa: BLE001
        log.debug("straighten: could not tell the library's disk — %s", exc)
        return False


def readers_for(cfg: Any) -> int:
    """How many photographs a survey judges at the same time.

    Half the scan workers the Tuning page chose, and never more than
    :data:`MAX_READERS`. Each judgement is one thread from start to finish:
    OpenCV is held to one thread per call (upright._face_cascades), so a
    survey that judged one photograph at a time used one core of the machine
    however many it had. Half, not all, because the survey runs while the
    family is using the gallery, and each reader holds its own copy of the
    orientation network. A Raspberry Pi, or any machine in Power saving,
    gets one: the survey runs as it always did.
    """
    try:
        workers = int(getattr(cfg, "workers", 1) or 1)
    except (TypeError, ValueError):
        workers = 1
    return max(1, min(MAX_READERS, workers // 2))


class Straightener:
    """Owns the survey and application threads, one at a time."""

    def __init__(self, cfg: Any, scanner: Any = None) -> None:
        self.cfg = cfg
        #: Held down while a survey or an apply runs: see CLAIM_STRAIGHTEN.
        self._scanner = scanner
        self.progress = Progress()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._faces = None
        self._waiting = False
        self._cancel_wait = threading.Event()
        #: Whether the run going now looks beside the scan instead of holding it.
        self._beside = False

    # -- the second witness ------------------------------------------------

    def _face_engine(self):
        """The same YuNet detector the Faces tab uses, loaded once.

        Not a second model: the household has already downloaded this one to
        group people, and asking it "is there a person here" costs one pass
        over a photograph the orientation model has already picked out.
        """
        if self._faces is None:
            from . import faces as faces_mod                # noqa: PLC0415

            self._faces = faces_mod.FaceEngine(self.cfg.state_dir)
        return self._faces

    def _has_a_person(self, img) -> bool:
        """Is anybody in this picture, the way up we are proposing to leave it?

        Asked of the *turned* image on purpose. A detector finds upright faces,
        so a face that appears once the photograph is turned is evidence for
        both halves of the claim at once: there is a person here, and this is
        the way up they are standing.
        """
        engine = self._face_engine()
        if not engine.available:
            return False
        try:
            return bool(engine.detect_image(img))
        except Exception as exc:                            # noqa: BLE001
            log.debug("straighten: face check failed — %s", exc)
            return False

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def stop(self, join: bool = False, timeout: float = 30.0) -> None:
        self._stop.set()
        self._cancel_wait.set()
        thread = self._thread
        if join and thread and thread.is_alive():
            thread.join(timeout)

    def _start(self, target: Callable[[], None], name: str,
               may_run_beside: bool = False) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._holding(target, may_run_beside),
                name=name, daemon=True)
            self._thread.start()
            return True

    def _holding(self, target: Callable[[], None],
                 may_run_beside: bool = False) -> Callable[[], None]:
        """*target*, run with the library indexer standing aside — or, for a
        survey on a machine with room for both, beside it (see
        :meth:`_room_beside_the_scan`)."""
        scanner = self._scanner
        if scanner is None:
            return target

        def run() -> None:
            from .scanner import CLAIM_STRAIGHTEN            # noqa: PLC0415
            self._beside = may_run_beside and self._room_beside_the_scan()
            if self._beside:
                log.info("straighten: looking beside the scan, which carries on — "
                         "graphics processor, solid-state library, %d at once",
                         readers_for(self.cfg))
                target()
                return
            with scanner.held(CLAIM_STRAIGHTEN):
                target()
        return run

    def _room_beside_the_scan(self) -> bool:
        """Whether a survey can run while the scan carries on, rather than
        pausing it.

        The survey held the scan down because both read every original from
        the same disk and, on a processor alone, both want every core: side by
        side, each took twice as long. Neither is true everywhere. With the
        image model on a graphics processor the scan's analysis leaves most of
        the processor free, a solid-state disk does not slow down for two
        readers, and the survey uses only half the workers the Tuning page
        chose. On such a machine both now carry on together. Anything not
        known to be so — a spinning or unknown disk, no graphics processor, a
        small machine — is held as before. Only the look: turning photographs
        always holds the scan down (see :meth:`_survey`).
        """
        return room_beside_the_scan(self.cfg, self._scanner)

    # -- the survey --------------------------------------------------------

    def survey(self, roots: list[str], *, limit: int | None = None,
               rescan: bool = False, auto_apply: bool = False,
               since: float | None = None) -> bool:
        """Ask the model about every candidate it has not seen. On its own it
        changes nothing on disk. *rescan* forgets what it has seen and
        proposed. *auto_apply* then turns what this run found, as one batch:
        it is only ever set for the survey that follows a scan. *since* is for
        carrying on after a restart: when the run began, so what it proposed
        before the restart is turned with the rest."""
        return self._start(lambda: self._survey(roots, limit, rescan, auto_apply, since),
                           "ninaivu-straighten-survey", may_run_beside=True)

    def after_scan(self, roots: list[str]) -> bool:
        """The survey that follows a scan when ``Config.straighten_auto`` is on.

        Quiet about everything it cannot do: no model yet, the face models
        missing while they are required, a pass already running. A scan
        finishes many times a day on a library phones back up to, and each
        of those is not an occasion for an error message — the Straighten
        page says what is missing when somebody opens it.
        """
        from . import orientnet                              # noqa: PLC0415

        if not getattr(self.cfg, "straighten_auto", True):
            return False
        if not orientnet.available():
            return False
        if (getattr(self.cfg, "straighten_requires_face", True)
                and not self._face_engine().available):
            return False
        conn = db.connect(self.cfg.db_path)
        init_schema(conn)
        if not self._candidates(conn, roots, limit=1):
            return False
        if self._scan_alive():
            # Called as the scan says "done" — from inside it, while its thread
            # is still finishing. Starting now held the scanner aside, which
            # stopped the rest of a multi-folder scan and queued another walk
            # of every library afterwards. So wait for it to be over.
            with self._lock:
                if self._waiting:
                    return True
                self._waiting = True
            threading.Thread(target=self._survey_when_scan_ends, args=(list(roots),),
                             name="ninaivu-straighten-wait", daemon=True).start()
            return True
        return self.survey(roots, auto_apply=self._auto_apply)

    @property
    def _auto_apply(self) -> bool:
        return bool(getattr(self.cfg, "straighten_auto_apply", True))

    def _scan_alive(self) -> bool:
        """Is the scan thread still going — asked of the thread itself.

        Not ``Scanner.running``: that answers False from inside the scan
        thread, which is exactly where the "done" notice that calls
        :meth:`after_scan` comes from.
        """
        thread = getattr(self._scanner, "_thread", None)
        return bool(thread is not None and thread.is_alive())

    def _survey_when_scan_ends(self, roots: list[str], patience: float = 6 * 3600) -> None:
        deadline = time.time() + patience
        self._cancel_wait.clear()
        try:
            while self._scan_alive() and time.time() < deadline:
                if self._cancel_wait.wait(0.5):
                    return                       # stopped by somebody, or shutting down
            if not self._scan_alive():
                self.survey(roots, auto_apply=self._auto_apply)
        finally:
            with self._lock:
                self._waiting = False

    def _candidates(self, conn, roots: list[str], limit: int | None = None) -> list:
        if not roots:
            return []
        placeholders = ",".join("?" * len(roots))
        sql = _CANDIDATES.format(roots=placeholders)
        args: list[Any] = [*roots]
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return conn.execute(sql, args).fetchall()

    def _already_seen(self, conn, roots: list[str]) -> int:
        if not roots:
            return 0
        sql = _ALREADY_SEEN.format(roots=",".join("?" * len(roots)))
        return int(conn.execute(sql, roots).fetchone()[0])

    def _survey(self, roots: list[str], limit: int | None, rescan: bool,
                auto_apply: bool = False, since: float | None = None) -> None:
        self.progress.reset("surveying")
        began = since or time.time()
        self.progress._set(started_at=time.time(), phase="Looking at photographs")
        conn = db.connect(self.cfg.db_path)
        init_schema(conn)
        # Carried on after a restart without `rescan`: what this run already
        # proposed is kept, and a second rescan would throw it away.
        resume.want(conn, RESUME_NAME, {"job": "survey", "limit": limit,
                                        "auto_apply": auto_apply, "since": began})
        try:
            self._survey_all(conn, roots, limit, rescan)
            if auto_apply and self._beside and self._scanner is not None:
                # Looked beside the scan; turning changes the index, so the
                # scan stands aside for that part as it always did.
                from .scanner import CLAIM_STRAIGHTEN        # noqa: PLC0415
                with self._scanner.held(CLAIM_STRAIGHTEN):
                    self._turn_what_was_found(conn, began)
            elif auto_apply:
                self._turn_what_was_found(conn, began)
        finally:
            if not self._stop.is_set():
                resume.done(conn, RESUME_NAME)

    def _survey_all(self, conn, roots: list[str], limit: int | None,
                    rescan: bool) -> None:
        from . import orientnet

        if not orientnet.available():
            self.progress._set(status="error", ended_at=time.time(),
                               message="The orientation model is not installed. "
                                       "Download it first.")
            return
        requires_face = bool(getattr(self.cfg, "straighten_requires_face", True))
        if requires_face:
            # Check that the face model *files* are present before starting the
            # survey, so a person gets the error up front rather than thousands
            # of photographs in.  The actual model weights are loaded lazily on
            # the first call to _has_a_person — most surveys skip most files, so
            # paying the load only when it is needed saves seconds of startup.
            face_eng = self._face_engine()
            if not face_eng.available:
                self.progress._set(
                    status="error", ended_at=time.time(),
                    message="Straightening is set to only touch photographs with "
                            "people in them, and the face models are not "
                            "downloaded. Download them on the Faces tab, or turn "
                            "that setting off.")
                return

        if rescan:
            conn.execute("DELETE FROM orientation_proposals WHERE status='pending'")
            conn.execute("DELETE FROM orientation_seen")
            conn.commit()

        rows = self._candidates(conn, roots, limit)
        # Counted from the whole library, not from what is left. After a
        # restart the survey carries on from what it had looked at, but the
        # console read "0 of" the rest, which looked exactly like starting
        # again from the beginning.
        earlier = 0 if limit else self._already_seen(conn, roots)
        self.progress._set(total=earlier + len(rows), processed=earlier,
                           earlier=earlier)
        if earlier and rows:
            log.info("straighten: carrying on — %d photographs already looked at, %d to go",
                     earlier, len(rows))
        judged: list[tuple] = []

        seen = {r["asset_id"] for r in conn.execute(
            "SELECT asset_id FROM orientation_proposals").fetchall()}

        now = time.time()
        pending: list[tuple] = []
        #: Photographs found upright whose old pending proposal is to be dropped.
        #: Deleted in a batch, where the batch is committed: a DELETE in the loop
        #: opened a write transaction that stayed open across every photograph
        #: decoded and judged after it — tens of seconds of a held write lock,
        #: during which signing in (or anything else that writes) failed with
        #: "database is locked".
        stale: list[int] = []
        # Twice the model's input, not the model's input: the same picture is
        # handed to the face check, and faces in a group photograph shrunk to
        # 416 px with NEAREST were too small to find — so photographs with
        # people in them were skipped as having nobody. Decoding the file is
        # the cost here; this resize is not.
        survey_edge = orientnet.IMAGE_SIZE * 2

        def judge(row) -> tuple[Any, Any]:
            path = Path(row["root"]) / row["rel_path"]
            # The tag is read from the file, not from the index. The
            # `orientation` column defaults to 1, so a photograph that
            # carried no tag at all is stored indistinguishably from one
            # whose camera said "upright" — and those two want different
            # confidence bars. Only the file knows which this is.
            tag = _file_orientation(path)
            # Decoded no larger than the survey looks at it. A JPEG can be
            # decoded at a fraction of its size directly, and on a 24 MP
            # photograph that is the difference between 280 ms and 40 ms
            # before the model has even seen it — most of what a survey
            # used to spend on each file.
            with media.open_for_index(path, survey_edge)[0] as shown:
                work = shown.convert("RGB")
                work.thumbnail((survey_edge, survey_edge),
                               Image.Resampling.BILINEAR)
                verdict = upright.decide(
                    work,
                    exif_orientation=tag,
                    enabled=True,
                )
            return work, verdict

        # Judged several at a time on a machine that has the room (see
        # readers_for), and taken back here in the order the list gives them:
        # the face check, what is written down and the progress all stay on
        # this one thread, exactly as they were. Only a few photographs wait
        # ahead, so a stop is quick and memory stays small.
        readers = readers_for(self.cfg)
        pool = (ThreadPoolExecutor(max_workers=readers,
                                   thread_name_prefix="ninaivu-straighten")
                if readers > 1 else None)
        todo = iter(rows)
        ahead: deque = deque()

        def top_up() -> None:
            while len(ahead) < readers * 2:
                row = next(todo, None)
                if row is None:
                    return
                if pool is None or row["id"] in seen:
                    ahead.append((row, None))
                else:
                    ahead.append((row, pool.submit(judge, row)))

        try:
            self._judge_in_order(conn, ahead, top_up, judge, seen, judged,
                                 pending, stale, requires_face, now)
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)

    def _judge_in_order(self, conn, ahead, top_up, judge, seen, judged,
                        pending, stale, requires_face, now) -> None:
        last_flush = last_remembered = now
        while True:
            top_up()
            if not ahead:
                break
            row, future = ahead.popleft()
            if self._stop.is_set():
                self.progress._set(status="stopped", ended_at=time.time())
                self._flush(conn, pending)
                self._drop_stale(conn, stale)
                self._remember(conn, judged)
                return
            # At least as long, not longer: Windows reads the clock in steps of
            # about 15 ms, so two photographs judged in the same step read
            # no time apart and "longer than nothing" never came true.
            if len(judged) >= 500 or (judged and time.time() - last_remembered
                                      >= REMEMBER_EVERY):
                # Between photographs, and what they proposed first: a
                # photograph written down as looked at always has its
                # proposal written down with it.
                self._flush(conn, pending)
                self._drop_stale(conn, stale)
                self._remember(conn, judged)
                last_flush = last_remembered = time.time()
            self.progress._bump(processed=1)
            if row["id"] in seen:
                self.progress._bump(skipped=1)
                continue
            try:
                work, verdict = future.result() if future is not None else judge(row)
            except Exception as exc:                        # noqa: BLE001
                # Not remembered as judged: a drive that was busy or away is
                # looked at again next time.
                log.debug("straighten: %s could not be read — %s",
                          Path(row["root"]) / row["rel_path"], exc)
                self.progress._bump(errors=1)
                continue
            judged.append((row["id"], row["indexed_at"] or 0))
            if verdict.source != "model" or not verdict.turns:
                # Upright now. A proposal left from an earlier version of the
                # file — it was re-indexed — no longer describes it.
                stale.append(row["id"])
                self.progress._bump(skipped=1)
                continue
            if requires_face and not self._has_a_person(
                    upright.apply(work, verdict.rotation)):
                # Confident, but nobody is in it. Left alone by choice: see
                # Config.straighten_requires_face.
                self.progress._bump(no_person=1, skipped=1)
                continue
            pending.append((row["id"], verdict.rotation, verdict.confidence,
                            row["rotation"], row["rot_source"], now))
            self.progress._bump(proposed=1)
            # Flush on count or on time, whichever comes first, so the UI
            # stays responsive even when most photographs are skipped.
            if len(pending) >= 200 or (pending and time.time() - last_flush > 10):
                self._flush(conn, pending)
                pending = []
                last_flush = time.time()

        self._flush(conn, pending)
        self._drop_stale(conn, stale)
        self._remember(conn, judged)
        self.progress._set(status="done", ended_at=time.time(),
                           phase="Survey complete")

    def _turn_what_was_found(self, conn, since: float) -> None:
        """Apply the proposals this survey has just made, as one batch.

        Only those: a photograph somebody has undone goes back to waiting, and
        must not be turned again by the next scan. Nothing is applied after an
        error or a stop, and what stays pending is still there to review.
        """
        if self._stop.is_set() or self.progress.snapshot().get("status") != "done":
            return
        ids = [r[0] for r in conn.execute(
            "SELECT asset_id FROM orientation_proposals "
            "WHERE status='pending' AND surveyed_at >= ?", (since,))]
        if not ids:
            return
        batch = int(conn.execute(
            "SELECT COALESCE(MAX(batch),0)+1 FROM orientation_proposals").fetchone()[0])
        found = self.progress.snapshot().get("proposed", 0)
        self.progress.reset("applying")
        self.progress._set(started_at=time.time(), phase="Turning photographs",
                           proposed=found)
        resume.want(conn, RESUME_NAME, {"job": "apply", "ids": ids,
                                        "min_confidence": 0.0, "batch": batch})
        self._apply_all(conn, ids, 0.0, batch)

    @staticmethod
    def _drop_stale(conn, stale: list[int]) -> None:
        """Forget the pending proposals of photographs now found upright. The caller
        commits straight after (see ``_remember``), so this holds the write lock
        only for as long as it takes to say so."""
        if stale:
            conn.executemany("DELETE FROM orientation_proposals "
                             "WHERE asset_id = ? AND status = 'pending'",
                             [(asset_id,) for asset_id in stale])
            stale.clear()

    @staticmethod
    def _remember(conn, judged: list[tuple]) -> None:
        """What this survey has looked at, so the next one does not again."""
        if judged:
            conn.executemany("INSERT OR REPLACE INTO orientation_seen(asset_id, indexed_at) "
                             "VALUES(?, ?)", judged)
            judged.clear()
        conn.commit()

    @staticmethod
    def _flush(conn, pending: list[tuple]) -> None:
        if not pending:
            return
        conn.executemany(
            "INSERT OR REPLACE INTO orientation_proposals"
            "(asset_id, rotation, confidence, source, prev_rotation,"
            " prev_rot_source, status, surveyed_at) "
            "VALUES(?,?,?,'model',?,?,'pending',?)", pending)
        conn.commit()
        pending.clear()

    # -- applying ----------------------------------------------------------

    def apply(self, asset_ids: list[int] | None = None,
              min_confidence: float = 0.0, batch: int | None = None) -> bool:
        """Bake the approved turns into the index and the thumbnails.

        *batch* is for carrying on after a restart: the rest of the run joins
        the batch it started, so one Undo still puts back all of it.
        """
        return self._start(lambda: self._apply(asset_ids, min_confidence, batch),
                           "ninaivu-straighten-apply")

    def _apply(self, asset_ids: list[int] | None, min_confidence: float,
               batch: int | None = None) -> None:
        self.progress.reset("applying")
        self.progress._set(started_at=time.time(), phase="Turning photographs")
        conn = db.connect(self.cfg.db_path)
        init_schema(conn)
        if batch is None:
            batch = int(conn.execute(
                "SELECT COALESCE(MAX(batch),0)+1 FROM orientation_proposals"
            ).fetchone()[0])
        resume.want(conn, RESUME_NAME, {"job": "apply", "ids": asset_ids or [],
                                        "min_confidence": min_confidence,
                                        "batch": batch})
        try:
            self._apply_all(conn, asset_ids, min_confidence, batch)
        finally:
            if not self._stop.is_set():
                resume.done(conn, RESUME_NAME)

    def _apply_all(self, conn, asset_ids: list[int] | None,
                   min_confidence: float, batch: int) -> None:

        sql = ("SELECT p.asset_id, p.rotation, a.root, a.rel_path, a.thumb "
               "FROM orientation_proposals p JOIN assets a ON a.id=p.asset_id "
               "WHERE p.status='pending' AND p.confidence >= ?")
        params: list[Any] = [min_confidence]
        if asset_ids:
            sql += f" AND p.asset_id IN ({','.join('?' * len(asset_ids))})"
            params += asset_ids
        rows = conn.execute(sql, params).fetchall()
        self.progress._set(total=len(rows))
        now = time.time()
        uncommitted = 0
        last_commit = time.time()

        for row in rows:
            if self._stop.is_set():
                self.progress._set(status="stopped", ended_at=time.time())
                if uncommitted:
                    conn.commit()
                return
            self.progress._bump(processed=1)
            try:
                self._turn_one(conn, row, batch, now)
            except Exception as exc:                        # noqa: BLE001
                log.warning("straighten: %s could not be turned — %s",
                            row["rel_path"], exc)
                self.progress._bump(errors=1)
                continue
            self.progress._bump(applied=1)
            uncommitted += 1
            # Batched, but never for long: an open transaction holds the write
            # lock through every photograph's decode and thumbnails, and the
            # cloud upload and scan passes are waiting on that lock.
            if uncommitted >= 50 or time.time() - last_commit > 1.0:
                conn.commit()
                uncommitted = 0
                last_commit = time.time()

        conn.commit()
        self.progress._set(status="done", ended_at=time.time(),
                           phase="Straightening complete")

    def _turn_one(self, conn, row, batch: int, now: float) -> None:
        cfg = self.cfg
        path = Path(row["root"]) / row["rel_path"]
        # Decoded no larger than the thumbnails made from it: nothing here
        # looks at the full picture, and the index records the file's own size.
        source, (width, height) = media.open_for_index(path, max(cfg.thumb_sizes))
        with source:
            turned = upright.apply(source, row["rotation"])
            if row["rotation"] % 180 == 90:
                width, height = height, width
            fields: dict[str, Any] = {
                "rotation": row["rotation"],
                "rot_source": "model",
                # The thumbnails are about to be rewritten and `indexed_at` is
                # their cache version — without this the new file sits behind a
                # URL every browser was told would never change.
                "indexed_at": now,
                "width": width,
                "height": height,
            }
            if row["thumb"]:
                media.write_thumbnails(turned, cfg.thumbs_dir, row["thumb"],
                                       cfg.thumb_sizes, cfg.thumb_format,
                                       cfg.thumb_quality, fast=True)
                # The blur placeholder and the grid's background colour are both
                # pictures of how the photograph used to be. Make them again.
                # A 180° turn has the same average colour — skip the recompute.
                try:
                    fields["blurhash"] = media.blurhash_encode(turned)
                    if row["rotation"] != 180:
                        fields["color"] = media.dominant_color(turned)
                except Exception:                           # noqa: BLE001
                    pass
        db.update_asset(conn, row["asset_id"], **fields)
        conn.execute(
            "UPDATE orientation_proposals SET status='applied', applied_at=?, "
            "batch=? WHERE asset_id=?", (now, batch, row["asset_id"]))

    # -- putting it back ---------------------------------------------------

    def undo(self, batch: int | None = None) -> dict[str, Any]:
        """Put an applied batch back exactly as it was, thumbnails included."""
        conn = db.connect(self.cfg.db_path)
        init_schema(conn)
        if batch is None:
            got = conn.execute(
                "SELECT MAX(batch) FROM orientation_proposals WHERE status='applied'"
            ).fetchone()[0]
            if not got:
                return {"restored": 0, "batch": None}
            batch = int(got)
        rows = conn.execute(
            "SELECT p.asset_id, p.prev_rotation, p.prev_rot_source, "
            "       a.root, a.rel_path, a.thumb "
            "FROM orientation_proposals p JOIN assets a ON a.id=p.asset_id "
            "WHERE p.status='applied' AND p.batch=?", (batch,)).fetchall()
        cfg = self.cfg
        restored = 0
        now = time.time()
        for row in rows:
            try:
                path = Path(row["root"]) / row["rel_path"]
                with media._open_oriented(path) as source:  # noqa: SLF001
                    back = upright.apply(source, row["prev_rotation"])
                    fields = {"rotation": row["prev_rotation"],
                              "rot_source": row["prev_rot_source"] or "none",
                              "indexed_at": now,
                              "width": back.size[0], "height": back.size[1]}
                    if row["thumb"]:
                        media.write_thumbnails(back, cfg.thumbs_dir, row["thumb"],
                                               cfg.thumb_sizes, cfg.thumb_format,
                                               cfg.thumb_quality)
                db.update_asset(conn, row["asset_id"], **fields)
                restored += 1
            except Exception as exc:                        # noqa: BLE001
                log.warning("straighten undo: %s — %s", row["rel_path"], exc)
        conn.execute("UPDATE orientation_proposals SET status='pending', "
                     "applied_at=NULL WHERE batch=?", (batch,))
        conn.commit()
        return {"restored": restored, "batch": batch}

    def dismiss(self, asset_ids: list[int]) -> int:
        """Say no to a proposal. It is not offered again."""
        if not asset_ids:
            return 0
        conn = db.connect(self.cfg.db_path)
        init_schema(conn)
        cur = conn.execute(
            f"UPDATE orientation_proposals SET status='dismissed' "
            f"WHERE status='pending' AND asset_id IN "
            f"({','.join('?' * len(asset_ids))})", asset_ids)
        conn.commit()
        return cur.rowcount

    # -- reading -----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        conn = db.connect(self.cfg.db_path)
        init_schema(conn)
        counts = {status: 0 for status in ("pending", "applied", "dismissed")}
        for row in conn.execute(
                "SELECT status, COUNT(*) n FROM orientation_proposals "
                "GROUP BY status").fetchall():
            counts[row["status"]] = row["n"]
        bands = conn.execute(
            "SELECT SUM(confidence>=0.95) very_sure, "
            "       SUM(confidence>=0.80 AND confidence<0.95) sure, "
            "       SUM(confidence<0.80) unsure "
            "FROM orientation_proposals WHERE status='pending'").fetchone()
        last = conn.execute(
            "SELECT MAX(batch) b FROM orientation_proposals "
            "WHERE status='applied'").fetchone()
        return {
            "counts": counts,
            "confidence": {k: (bands[k] or 0) for k in
                           ("very_sure", "sure", "unsure")},
            "last_batch": last["b"],
            "progress": self.progress.snapshot(),
        }
