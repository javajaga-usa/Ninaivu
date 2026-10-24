"""The smaller-videos trial: HEVC tried on a sample before anything changes.

Compress & Replace (media/video_compress.py) makes an H.264 MP4 no larger
than 1080p. HEVC, through the Mac's own video engine, makes the same picture
in about half the bytes, and could keep a 4K video at 4K. Before that is
offered for real, this tries it on a sample of the library's own videos, so
the household can see the saving and look at the result with their own eyes:

* **The sample** (:func:`pick`) is up to 500 videos, spread evenly from the
  smallest to the largest, so short phone clips and long camera recordings
  are both in it. Videos that are already HEVC or iPhone videos are left
  out, as the display copies leave them (mugil/display.py's ``plan``).
* **Each copy** is HEVC in MP4 at the video's own size, with its tags,
  rotation and modified time carried across, the same command the display
  copies use without making it smaller. It is written to the state folder
  (``video-trial/``), never into the library.
* **Each copy is measured**: its size against the original's, how long the
  encode took, whether it runs as long, whether its tags and rotation came
  through, and how close its picture is to the original's (SSIM over the
  first :data:`SSIM_SECONDS` seconds; 1.0 is the same picture).
* **A person looks**: the console plays each original beside its copy, and
  "Looks the same" or "Looks worse" is kept with it.

Nothing in the library is read for anything but making the copies, nothing
is replaced, and Clear removes every trial copy. One video at a time, at a
low priority, making way for the household like the copying does.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

#: In the state folder: the trial copies, and their record.
FOLDER = "video-trial"
LEDGER = "video-trial.db"
#: How many videos the trial takes by default, and at most.
SAMPLE = 500
MOST = 2000
#: How much of each video the picture comparison looks at.
SSIM_SECONDS = 30
#: A copy is given at most this many times its own length to encode.
TIME_ALLOWANCE = 8
MIN_SECONDS = 10 * 60

VERDICTS = ("same", "worse")
OUTCOMES = ("waiting", "made", "larger", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    asset_id      INTEGER PRIMARY KEY,
    root          TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    size          INTEGER NOT NULL,
    width         INTEGER,
    height        INTEGER,
    duration      REAL,
    outcome       TEXT NOT NULL DEFAULT 'waiting',
    copy_size     INTEGER NOT NULL DEFAULT 0,
    seconds       REAL,
    ssim          REAL,
    tags_kept     INTEGER,
    rotation_kept INTEGER,
    why           TEXT,
    verdict       TEXT,
    made_at       REAL
);
CREATE TABLE IF NOT EXISTS trial (key TEXT PRIMARY KEY, value TEXT);
"""


def folder(state_dir: str | Path) -> Path:
    return Path(state_dir) / FOLDER


@contextlib.contextmanager
def ledger(state_dir: str | Path):
    conn = sqlite3.connect(str(Path(state_dir) / LEDGER), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def copy_path(state_dir: str | Path, asset_id: int) -> Path:
    return folder(state_dir) / f"{int(asset_id)}.mp4"


# -- the sample -----------------------------------------------------------------

def candidates(conn: sqlite3.Connection, roots: Iterable[str]) -> list[dict[str, Any]]:
    """Every video in *roots* the trial could try, smallest first."""
    from ..mugil.display import plan                              # noqa: PLC0415
    roots = [str(r) for r in roots]
    if not roots:
        return []
    marks = ",".join("?" * len(roots))
    rows = conn.execute(
        f"SELECT id, root, rel_path, ext, kind, camera, size, width, height, duration "
        f"FROM assets WHERE root IN ({marks}) AND kind='video' AND trashed=0 AND size>0 "
        f"ORDER BY size, id", roots).fetchall()
    return [dict(r) for r in rows if plan(r["ext"], r["kind"], r["camera"]) == "video"]


def pick(conn: sqlite3.Connection, roots: Iterable[str], n: int = SAMPLE) -> list[dict[str, Any]]:
    """Up to *n* videos spread evenly from the smallest to the largest."""
    found = candidates(conn, roots)
    n = max(1, min(int(n), MOST))
    if len(found) <= n:
        return found
    step = (len(found) - 1) / (n - 1) if n > 1 else 0
    return [found[round(i * step)] for i in range(n)]


# -- one copy -------------------------------------------------------------------

def ssim(ffmpeg: str, original: Path, copy: Path, seconds: int = SSIM_SECONDS) -> float | None:
    """How alike the two pictures are, 0 to 1, over the first *seconds*."""
    from .media import LOCAL_ONLY                                 # noqa: PLC0415
    command = [ffmpeg, "-hide_banner", "-nostdin", "-noautorotate", *LOCAL_ONLY,
               "-t", str(seconds), "-i", str(copy),
               "-noautorotate", *LOCAL_ONLY, "-t", str(seconds), "-i", str(original),
               # The original at the even size its copy was made at.
               "-lavfi", "[1:v]scale='trunc(iw/2)*2':'trunc(ih/2)*2'[ref];[0:v][ref]ssim",
               "-f", "null", "-"]
    try:
        done = subprocess.run(command, capture_output=True, timeout=max(MIN_SECONDS, seconds * 20),
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.findall(r"All:([0-9.]+)", done.stderr.decode("utf-8", "replace"))
    try:
        return round(float(found[-1]), 4) if found else None
    except ValueError:
        return None


def try_one(state_dir: str | Path, row: dict[str, Any]) -> dict[str, Any]:
    """Make and measure the trial copy of one sampled video; what was found."""
    from ..mugil import display                                   # noqa: PLC0415
    from . import media                                           # noqa: PLC0415
    source = Path(row["root"]) / row["rel_path"]
    encoder = display.hevc_encoder()
    if not media.FFMPEG or not encoder:
        return {"outcome": "failed", "why": "this computer has no HEVC encoder"}
    target = copy_path(state_dir, row["asset_id"])
    partial = target.with_name(target.name + ".partial")
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        before = display._probe(source)
        length = display._duration(before)

        def lower():
            if hasattr(os, "nice"):
                with contextlib.suppress(OSError):
                    os.nice(10)
        done = subprocess.run(display.video_command(media.FFMPEG, source, partial, encoder,
                                                    lines=None),
                              capture_output=True, check=False,
                              timeout=max(MIN_SECONDS, (length or 0) * TIME_ALLOWANCE),
                              preexec_fn=lower if os.name == "posix" else None)
        seconds = round(time.monotonic() - started, 1)
        if done.returncode != 0 or not partial.is_file():
            said = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg"]
            return {"outcome": "failed", "why": said[0][:200], "seconds": seconds}
        after = display._probe(partial)
        size = partial.stat().st_size
        found: dict[str, Any] = {
            "seconds": seconds, "copy_size": size,
            "tags_kept": int(display._tags(after) == display._tags(before)),
            "rotation_kept": int(display._rotation(after) == display._rotation(before))}
        if length and abs(display._duration(after) - length) > max(1.0, length * 0.01):
            return {**found, "outcome": "failed", "why": "the copy does not run as long"}
        if size >= source.stat().st_size:
            return {**found, "outcome": "larger", "why": "no smaller than the original"}
        stamp = source.stat()
        os.utime(partial, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(partial, target)
        return {**found, "outcome": "made", "ssim": ssim(media.FFMPEG, source, target)}
    except subprocess.TimeoutExpired:
        return {"outcome": "failed", "why": "the copy took too long",
                "seconds": round(time.monotonic() - started, 1)}
    except OSError as exc:
        return {"outcome": "failed", "why": f"the video could not be read ({exc.strerror or exc})"}
    finally:
        partial.unlink(missing_ok=True)


# -- what the trial found -------------------------------------------------------

def summary(state_dir: str | Path, *, library_video_bytes: int = 0) -> dict[str, Any]:
    """Counts, the saving, the picture scores and the verdicts, and the saving
    the whole library's videos would see at the same rate."""
    out: dict[str, Any] = {o: 0 for o in OUTCOMES}
    out.update({"sampled": 0, "original_bytes": 0, "copy_bytes": 0, "saving": None,
                "seconds": 0.0, "ssim_median": None, "ssim_lowest": None,
                "verdicts": {v: 0 for v in VERDICTS}, "tags_lost": 0, "rotation_lost": 0,
                "library_video_bytes": int(library_video_bytes), "library_saving_bytes": None,
                "started_at": None})
    if not (Path(state_dir) / LEDGER).exists():
        return out
    with ledger(state_dir) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM samples")]
        started = conn.execute("SELECT value FROM trial WHERE key='started_at'").fetchone()
    out["started_at"] = float(started[0]) if started else None
    out["sampled"] = len(rows)
    scores = []
    for row in rows:
        out[row["outcome"]] = out.get(row["outcome"], 0) + 1
        out["seconds"] += float(row["seconds"] or 0)
        if row["outcome"] != "waiting" and row["tags_kept"] == 0:
            out["tags_lost"] += 1
        if row["outcome"] != "waiting" and row["rotation_kept"] == 0:
            out["rotation_lost"] += 1
        if row["outcome"] == "made":
            out["original_bytes"] += int(row["size"])
            out["copy_bytes"] += int(row["copy_size"])
            if row["ssim"] is not None:
                scores.append(float(row["ssim"]))
        if row["verdict"] in VERDICTS:
            out["verdicts"][row["verdict"]] += 1
    out["seconds"] = round(out["seconds"], 1)
    tried = sum(int(r["size"]) for r in rows if r["outcome"] in ("made", "larger"))
    if tried:
        # A video that came out no larger is counted as no saving, as Replace
        # would leave it as it is.
        out["saving"] = round(1 - (out["copy_bytes"] + (tried - out["original_bytes"])) / tried, 4)
        if library_video_bytes:
            out["library_saving_bytes"] = int(library_video_bytes * out["saving"])
    if scores:
        out["ssim_median"] = round(statistics.median(scores), 4)
        out["ssim_lowest"] = round(min(scores), 4)
    return out


def samples(state_dir: str | Path, *, offset: int = 0, limit: int = 50,
            outcome: str | None = None) -> list[dict[str, Any]]:
    if not (Path(state_dir) / LEDGER).exists():
        return []
    where, args = (("WHERE outcome=?", [outcome]) if outcome in OUTCOMES
                   else ("WHERE outcome!='waiting'", []))
    with ledger(state_dir) as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM samples {where} ORDER BY outcome='made' DESC, "
            f"COALESCE(ssim, 2), asset_id LIMIT ? OFFSET ?",
            [*args, max(1, min(int(limit), 500)), max(0, int(offset))])]


def sample(state_dir: str | Path, asset_id: int) -> dict[str, Any] | None:
    if not (Path(state_dir) / LEDGER).exists():
        return None
    with ledger(state_dir) as conn:
        row = conn.execute("SELECT * FROM samples WHERE asset_id=?", (int(asset_id),)).fetchone()
    return dict(row) if row else None


def judge(state_dir: str | Path, asset_id: int, verdict: str | None) -> bool:
    """Keep what a person said of one copy (None takes it back)."""
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(verdict)
    if not (Path(state_dir) / LEDGER).exists():
        return False
    with ledger(state_dir) as conn:
        done = conn.execute("UPDATE samples SET verdict=? WHERE asset_id=? AND outcome='made'",
                            (verdict, int(asset_id)))
        return done.rowcount > 0


# -- the worker -----------------------------------------------------------------

class VideoTrial:
    """Runs the trial on its own thread once it is started, and again after a
    restart until every sampled video has been tried."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 hold: Callable[[], str | None] | None = None,
                 attempt: Callable[[str | Path, dict[str, Any]], dict[str, Any]] = try_one) -> None:
        self.cfg = cfg
        self.connect_db = connect_db
        self.hold = hold or (lambda: None)
        self.attempt = attempt
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.doing: dict[str, Any] = {}

    def roots(self) -> list[str]:
        cfg = self.cfg
        return [str(r) for r in (cfg.roots or ([cfg.active_root] if cfg.active_root else []))]

    def available(self) -> str | None:
        """Why the trial cannot run here, or None."""
        from ..mugil.display import hevc_encoder                    # noqa: PLC0415
        from . import media                                         # noqa: PLC0415
        if not media.FFMPEG:
            return "ffmpeg was not found"
        if not hevc_encoder():
            return "this ffmpeg has no HEVC encoder"
        return None

    def start(self, n: int = SAMPLE) -> dict[str, Any]:
        """Take a sample (once) and start trying it."""
        state = Path(self.cfg.state_dir)
        if not self.sampled():
            # The index's own connection for this thread: not closed here.
            chosen = pick(self.connect_db(), self.roots(), n)
            with ledger(state) as led:
                led.executemany(
                    "INSERT OR IGNORE INTO samples (asset_id, root, rel_path, size, width, "
                    "height, duration) VALUES (?,?,?,?,?,?,?)",
                    [(r["id"], r["root"], r["rel_path"], r["size"], r["width"], r["height"],
                      r["duration"]) for r in chosen])
                led.execute("INSERT OR REPLACE INTO trial (key, value) VALUES ('started_at', ?)",
                            (str(time.time()),))
        self._set_running(True)
        self.keep()
        return self.status()

    def sampled(self) -> int:
        if not (Path(self.cfg.state_dir) / LEDGER).exists():
            return 0
        with ledger(self.cfg.state_dir) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0])

    def waiting(self) -> list[dict[str, Any]]:
        if not (Path(self.cfg.state_dir) / LEDGER).exists():
            return []
        with ledger(self.cfg.state_dir) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM samples WHERE outcome='waiting' ORDER BY asset_id")]

    def _set_running(self, on: bool) -> None:
        with ledger(self.cfg.state_dir) as conn:
            conn.execute("INSERT OR REPLACE INTO trial (key, value) VALUES ('running', ?)",
                         ("1" if on else "0",))

    def _running(self) -> bool:
        if not (Path(self.cfg.state_dir) / LEDGER).exists():
            return False
        with ledger(self.cfg.state_dir) as conn:
            row = conn.execute("SELECT value FROM trial WHERE key='running'").fetchone()
        return bool(row and row[0] == "1")

    def keep(self) -> None:
        """Carry on a trial that was running when Ninaivu last stopped."""
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="video-trial", daemon=True)
        self._thread.start()

    def pause(self) -> dict[str, Any]:
        if (Path(self.cfg.state_dir) / LEDGER).exists():
            self._set_running(False)
        self.stop(join=True)
        return self.status()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=15)
        self._thread = None

    def clear(self) -> dict[str, Any]:
        """Stop, and remove every trial copy and what was found."""
        self.pause()
        shutil.rmtree(folder(self.cfg.state_dir), ignore_errors=True)
        (Path(self.cfg.state_dir) / LEDGER).unlink(missing_ok=True)
        return self.status()

    def library_video_bytes(self) -> int:
        return sum(int(r["size"]) for r in candidates(self.connect_db(), self.roots()))

    def status(self) -> dict[str, Any]:
        with self._lock:
            doing = dict(self.doing)
        running = self._thread is not None and self._thread.is_alive()
        return {**summary(self.cfg.state_dir, library_video_bytes=self.library_video_bytes()),
                "running": running, "doing": doing, "unavailable": self.available()}

    def run_once(self) -> int:
        """Try every waiting video, until stopped; how many were tried."""
        tried = 0
        for row in self.waiting():
            if self._stop.is_set():
                break
            while self.hold() and not self._stop.wait(30):
                pass
            if self._stop.is_set():
                break
            with self._lock:
                self.doing = {"path": row["rel_path"], "since": time.time()}
            try:
                found = self.attempt(self.cfg.state_dir, row)
            except Exception as exc:                               # noqa: BLE001 - one file
                log.warning("The video trial could not try %s: %s", row["rel_path"], exc)
                found = {"outcome": "failed", "why": "could not be tried"}
            with ledger(self.cfg.state_dir) as conn:
                conn.execute(
                    "UPDATE samples SET outcome=?, copy_size=?, seconds=?, ssim=?, tags_kept=?, "
                    "rotation_kept=?, why=?, made_at=? WHERE asset_id=?",
                    (found.get("outcome", "failed"), int(found.get("copy_size") or 0),
                     found.get("seconds"), found.get("ssim"), found.get("tags_kept"),
                     found.get("rotation_kept"), found.get("why"), time.time(), row["asset_id"]))
            tried += 1
        with self._lock:
            self.doing = {}
        return tried

    def _run(self) -> None:
        try:
            self.run_once()
            if not self._stop.is_set():
                self._set_running(False)
                log.info("The smaller-videos trial has tried every sampled video")
        except Exception:                                           # noqa: BLE001
            log.exception("The smaller-videos trial stopped")


__all__ = ["VideoTrial", "pick", "candidates", "try_one", "summary", "samples", "sample",
           "judge", "ssim", "copy_path", "SAMPLE", "VERDICTS"]
