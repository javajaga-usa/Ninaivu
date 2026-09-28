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
    "SELECT id, root, rel_path, thumb, rotation, rot_source, orientation "
    "FROM assets "
    "WHERE kind='picture' AND trashed=0 AND rot_source IN ('none','') "
    "AND root IN ({roots}) "
    "ORDER BY id"
)


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
        thread = self._thread
        if join and thread and thread.is_alive():
            thread.join(timeout)

    def _start(self, target: Callable[[], None], name: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._holding(target),
                                            name=name, daemon=True)
            self._thread.start()
            return True

    def _holding(self, target: Callable[[], None]) -> Callable[[], None]:
        """*target*, run with the library indexer standing aside."""
        scanner = self._scanner
        if scanner is None:
            return target

        def run() -> None:
            from .scanner import CLAIM_STRAIGHTEN            # noqa: PLC0415
            with scanner.held(CLAIM_STRAIGHTEN):
                target()
        return run

    # -- the survey --------------------------------------------------------

    def survey(self, roots: list[str], *, limit: int | None = None,
               rescan: bool = False) -> bool:
        """Ask the model about every candidate. Changes nothing on disk."""
        return self._start(lambda: self._survey(roots, limit, rescan),
                           "ninaivu-straighten-survey")

    def _survey(self, roots: list[str], limit: int | None, rescan: bool) -> None:
        self.progress.reset("surveying")
        self.progress._set(started_at=time.time(), phase="Looking at photographs")
        conn = db.connect(self.cfg.db_path)
        init_schema(conn)
        # Carried on after a restart without `rescan`: what this run already
        # proposed is kept, and a second rescan would throw it away.
        resume.want(conn, RESUME_NAME, {"job": "survey", "limit": limit})
        try:
            self._survey_all(conn, roots, limit, rescan)
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
            conn.commit()

        placeholders = ",".join("?" * len(roots))
        rows = conn.execute(_CANDIDATES.format(roots=placeholders), roots).fetchall()
        if limit:
            rows = rows[:limit]
        self.progress._set(total=len(rows))

        seen = {r["asset_id"] for r in conn.execute(
            "SELECT asset_id FROM orientation_proposals").fetchall()}

        now = time.time()
        last_flush = now
        pending: list[tuple] = []
        # Twice the model's input, not the model's input: the same picture is
        # handed to the face check, and faces in a group photograph shrunk to
        # 416 px with NEAREST were too small to find — so photographs with
        # people in them were skipped as having nobody. Decoding the file is
        # the cost here; this resize is not.
        survey_edge = orientnet.IMAGE_SIZE * 2
        for row in rows:
            if self._stop.is_set():
                self.progress._set(status="stopped", ended_at=time.time())
                self._flush(conn, pending)
                return
            self.progress._bump(processed=1)
            if row["id"] in seen:
                self.progress._bump(skipped=1)
                continue
            path = Path(row["root"]) / row["rel_path"]
            try:
                # The tag is read from the file, not from the index. The
                # `orientation` column defaults to 1, so a photograph that
                # carried no tag at all is stored indistinguishably from one
                # whose camera said "upright" — and those two want different
                # confidence bars. Only the file knows which this is.
                tag = _file_orientation(path)
                with media._open_oriented(path) as shown:   # noqa: SLF001
                    work = shown.convert("RGB")
                    work.thumbnail((survey_edge, survey_edge),
                                   Image.Resampling.BILINEAR)
                    verdict = upright.decide(
                        work,
                        exif_orientation=tag,
                        enabled=True,
                    )
            except Exception as exc:                        # noqa: BLE001
                log.debug("straighten: %s could not be read — %s", path, exc)
                self.progress._bump(errors=1)
                continue
            if verdict.source != "model" or not verdict.turns:
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
        self.progress._set(status="done", ended_at=time.time(),
                           phase="Survey complete")

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
        with media._open_oriented(path) as source:          # noqa: SLF001
            turned = upright.apply(source, row["rotation"])
            fields: dict[str, Any] = {
                "rotation": row["rotation"],
                "rot_source": "model",
                # The thumbnails are about to be rewritten and `indexed_at` is
                # their cache version — without this the new file sits behind a
                # URL every browser was told would never change.
                "indexed_at": now,
                "width": turned.size[0],
                "height": turned.size[1],
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
