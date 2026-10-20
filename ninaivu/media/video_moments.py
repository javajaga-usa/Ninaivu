"""Finding the moments inside videos, in the background.

Every clip long enough to hold more than one scene (storage/video_moments.py,
``MIN_SECONDS``) is read once by ffmpeg at a frame every few seconds, from its
keyframes only where it has them, so a twenty-minute clip is a short read
rather than a full decode. Each frame is turned into a vector by the
gallery's own picture-search model (``embed_images``: SigLIP 2 on the
on-device engine, or CLIP through PyTorch), and frames in a row that look
alike are folded into one scene, which keeps the second it starts at and the
mean of its frames. A clip of a birthday is then "the cake comes in", "the
candles", "the presents", each with its time, and the search finds the right
one of them (api/api_video_moments.py).

Only with ``video_moments`` switched on (Advanced settings), the AI passes on
and a picture-search model loaded; otherwise it does nothing and starts
nothing. It is analysis, so it gives way to the household as every analysis
pass does (server/workload.py), between clips and between batches of frames.

When it runs: a while after start-up, after a scan has finished adding
files (:meth:`MomentFinder.wake`, from Services), and every :data:`EVERY`
seconds in case anything was missed.
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from PIL import Image

from ..storage import video_moments
from . import media

log = logging.getLogger(__name__)

#: Seconds between looks for clips nobody woke this for.
EVERY = 3600.0
#: Clips read from the index at a time.
PAGE = 50
#: The closest two frames are read, in seconds, and the most frames read of
#: one clip: a longer clip is read more sparsely instead.
MIN_STEP = 2.0
MAX_FRAMES = 400
#: The longest side of a frame handed to the model (it shrinks it further).
FRAME_SIDE = 384
#: Frames handed to the model at a time.
BATCH = 16
#: How alike (cosine of their vectors) a frame must be to the first frame of
#: the scene it follows to be counted as the same scene. Frames of one scene
#: sit around 0.9 with the picture-search model, a new place or a new subject
#: well below; the real library will say whether this wants moving.
SAME_SCENE = 0.88
#: The most scenes kept of one clip; past it, evenly chosen ones.
MAX_MOMENTS = 120


def step_for(duration: float) -> float:
    """Seconds between the frames read of a clip *duration* seconds long."""
    return max(MIN_STEP, float(duration) / MAX_FRAMES)


def _ppm_frames(stream) -> Iterator[Image.Image]:  # noqa: ANN001
    """The frames of an ffmpeg ``ppm`` image2pipe, one image each."""
    while True:
        header: list[bytes] = []
        token = b""
        while len(header) < 4:
            char = stream.read(1)
            if not char:
                return
            if char.isspace():
                if token:
                    header.append(token)
                    token = b""
                continue
            token += char
        if header[0] != b"P6":
            return
        width, height = int(header[1]), int(header[2])
        size = width * height * 3
        data = stream.read(size)
        if len(data) < size:
            return
        yield Image.frombytes("RGB", (width, height), data)


def read_frames(path: Path, duration: float, *, keyframes_only: bool = True,
                stop: Callable[[], bool] | None = None) -> Iterator[tuple[float, Image.Image]]:
    """``(seconds, frame)`` every :func:`step_for` seconds of a clip, as
    ffmpeg decodes it. With *keyframes_only*, from the clip's keyframes, the
    nearest before each time: far quicker, and within a second or two."""
    if not media.FFMPEG:
        return
    step = step_for(duration)
    fit = f"'min({FRAME_SIDE},iw)':'min({FRAME_SIDE},ih)'"
    command = [media.FFMPEG, "-v", "quiet", "-nostdin", "-threads", "2"]
    if keyframes_only:
        command += ["-skip_frame", "nokey"]
    command += [*media.LOCAL_ONLY, "-i", str(path), "-an", "-sn", "-dn",
                "-vf", f"fps=1/{step:.3f},scale={fit}:force_original_aspect_ratio=decrease",
                "-frames:v", str(MAX_FRAMES + 1), "-pix_fmt", "rgb24",
                "-f", "image2pipe", "-vcodec", "ppm", "-"]
    # Twice the clip's length at most, and two minutes for a short one: past
    # that ffmpeg is stuck on a drive asleep or a broken file.
    deadline = time.monotonic() + max(120.0, 2 * float(duration))
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        for index, frame in enumerate(_ppm_frames(proc.stdout)):
            if (stop is not None and stop()) or time.monotonic() > deadline:
                return
            yield index * step, frame
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:                          # pragma: no cover
            pass
        if proc.stdout is not None:
            proc.stdout.close()


def scenes(frames: list[tuple[float, Any]], same: float = SAME_SCENE,
           most: int = MAX_MOMENTS) -> list[tuple[float, Any]]:
    """Fold frames in a row that look alike into scenes: ``(start, vector)``
    each, the vector the unit mean of the scene's frames. Each frame is
    compared with the scene's first, so a slow pan becomes new scenes as it
    goes rather than one scene that drifts."""
    import numpy as np                                          # noqa: PLC0415

    found: list[tuple[float, Any]] = []
    anchor = None
    group: list[Any] = []
    start = 0.0

    def close() -> None:
        if group:
            mean = np.mean(np.stack(group), axis=0)
            norm = float(np.linalg.norm(mean))
            found.append((start, mean / norm if norm else mean))

    for at, vector in frames:
        vector = np.asarray(vector, dtype="float32")
        if anchor is None or float(vector @ anchor) < same:
            close()
            anchor, group, start = vector, [vector], float(at)
        else:
            group.append(vector)
    close()
    if len(found) > most:
        picks = np.linspace(0, len(found) - 1, most).round().astype(int)
        found = [found[i] for i in sorted(set(int(p) for p in picks))]
    return found


class MomentFinder:
    """Keeps every long clip's moments up to date while ``video_moments`` is on."""

    def __init__(self, cfg: Any, connect_db: Callable[[], sqlite3.Connection], *,
                 engine: Callable[[], Any],
                 hold: Callable[[], str | None] | None = None,
                 every: float = EVERY) -> None:
        self.cfg = cfg
        self._connect = connect_db
        self._engine = engine
        self._hold = hold
        self._every = every
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False, "done": 0, "failed": 0, "owed": 0,
                                       "message": "", "seconds": 0.0, "finished_at": 0.0}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "video_moments", False)) and \
            bool(getattr(self.cfg, "ai_enabled", True))

    @property
    def running(self) -> bool:
        return bool(self._state["running"])

    def keep(self) -> None:
        """Start the background thread (once); it does nothing while off."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-video-moments",
                                        daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        self._wake.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(10)

    def _loop(self) -> None:
        # A first look a while after start-up, out of the way of the scan and
        # of the model loading.
        self._wake.wait(180)
        while not self._stop.is_set():
            self._wake.clear()
            if self.enabled:
                try:
                    self.run()
                except Exception:                             # noqa: BLE001 - next time
                    log.exception("could not look for the moments in videos")
            self._wake.wait(self._every)

    def _say(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def ready(self) -> str | None:
        """Why moments cannot be found on this computer now, or None."""
        if not media.FFMPEG:
            return "ffmpeg is not installed"
        engine = self._engine()
        if engine is None or not getattr(engine, "semantic", False) \
                or not callable(getattr(engine, "embed_images", None)):
            return "the picture-search model is not loaded (AI settings)"
        return None

    def _wait_for_the_household(self) -> bool:
        """False when stopped while waiting."""
        waited = False
        while self._hold is not None and not self._stop.is_set():
            reason = self._hold()
            if not reason:
                break
            waited = True
            self._say(message=f"Waiting: {reason}")
            self._stop.wait(5)
        if waited:
            self._say(message="")
        return not self._stop.is_set()

    def run(self) -> dict[str, Any]:
        """One pass over every clip owed. Returns the state at its end."""
        why_not = self.ready()
        if why_not:
            self._say(message=why_not, owed=0)
            return self.status()
        engine = self._engine()
        model = str(getattr(engine, "model_id", "") or "")
        conn = self._connect()
        self._say(running=True, done=0, failed=0, message="", seconds=0.0,
                  owed=video_moments.count_owed(conn, model))
        try:
            after = 0
            while not self._stop.is_set() and self.enabled:
                page = video_moments.owed(conn, model, limit=PAGE, after=after)
                if not page:
                    break
                after = int(page[-1]["id"])
                for row in page:
                    if not self._wait_for_the_household() or not self.enabled:
                        return self.status()
                    self._clip(conn, engine, model, row)
        finally:
            self._say(running=False, finished_at=time.time(),
                      owed=video_moments.count_owed(conn, model))
        return self.status()

    def _clip(self, conn: sqlite3.Connection, engine: Any, model: str, row: Any) -> None:
        path = Path(row["root"]) / row["rel_path"]
        if not path.is_file():
            # Its drive is away, or it is gone and the next scan says so:
            # left owed, not counted against.
            return
        started = time.monotonic()
        try:
            found = self.find(engine, path, float(row["duration"] or 0))
        except _Stopped:
            return
        except Exception as exc:                                  # noqa: BLE001 - one clip
            log.debug("moments of %s: %s", path, exc)
            video_moments.fail(conn, int(row["id"]), str(exc), model=model,
                               mtime=float(row["mtime"] or 0))
            self._say(failed=self._state["failed"] + 1)
            return
        if not found:
            video_moments.fail(conn, int(row["id"]), "no frames could be read", model=model,
                               mtime=float(row["mtime"] or 0))
            self._say(failed=self._state["failed"] + 1)
            return
        video_moments.store(conn, int(row["id"]), found, model=model,
                            mtime=float(row["mtime"] or 0))
        self._say(done=self._state["done"] + 1,
                  seconds=self._state["seconds"] + time.monotonic() - started,
                  owed=max(0, self._state["owed"] - 1))

    def find(self, engine: Any, path: Path, duration: float) -> list[tuple[float, Any]]:
        """The scenes of one clip, ``(start, vector)`` each."""
        vectors: list[tuple[float, Any]] = []
        for keyframes_only in (True, False):
            vectors = self._embed(engine, read_frames(path, duration,
                                                      keyframes_only=keyframes_only,
                                                      stop=self._stop.is_set))
            if vectors:
                break
            # A clip whose keyframes ffmpeg will not hand over on their own:
            # read the whole of it instead, the slow way.
        return scenes(vectors)

    def _embed(self, engine: Any, frames: Iterator[tuple[float, Image.Image]]
               ) -> list[tuple[float, Any]]:
        out: list[tuple[float, Any]] = []
        batch: list[tuple[float, Image.Image]] = []

        def flush() -> None:
            if not batch:
                return
            if not self._wait_for_the_household():
                raise _Stopped
            rows = engine.embed_images([image for _, image in batch])
            out.extend((at, row) for (at, _), row in zip(batch, rows))
            batch.clear()

        for at, image in frames:
            if self._stop.is_set():
                raise _Stopped
            batch.append((at, image))
            if len(batch) >= BATCH:
                flush()
        if self._stop.is_set():
            raise _Stopped
        flush()
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["enabled"] = self.enabled
        done = int(state.get("done") or 0)
        state["per_clip"] = round(state["seconds"] / done, 1) if done else None
        return state


class _Stopped(Exception):
    """The pass was stopped while a clip was being read."""
