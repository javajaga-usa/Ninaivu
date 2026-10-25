"""Writing down what is said in home videos, in the background.

Every video between storage/video_words.py's ``MIN_SECONDS`` and
``MAX_SECONDS`` is handed to the model host's ``transcribe`` capability, the
one that writes down voice stories (runtime/speech.py), a window of
:data:`WINDOW` seconds at a time: ffmpeg reads only that stretch of the
sound, the speech detector leaves out the silence and the music between the
words, Whisper writes down what is left in the language it hears (Tamil or
English, as the household set in ``speech_languages``), and a line not said
in English is translated too when large-v3 is the model downloaded. Each line
keeps the second it starts and ends at, in the video's own time.

A long video is read in windows so that a voice story told meanwhile is not
kept waiting behind a two-hour wedding, the pass can stop between them, and
no more than ten minutes of sound is held in memory at once.

Only with ``video_words`` and the on-device engine (``ai_runtime``) switched
on, a speech model downloaded and ffmpeg there; otherwise it does nothing and
starts nothing. It is analysis, so it gives way to the household as every
analysis pass does (server/workload.py), and to the voice stories, which are
written down first: Services passes both in *hold*.

When it runs: a while after start-up, after a scan has added files
(:meth:`WordListener.wake`, from Services), and every :data:`EVERY` seconds in
case anything was missed.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..runtime import speech
from ..runtime.host import HostError, HostStopped, ModelHost, Unavailable
from ..storage import video_words
from ..words import said
from . import media

log = logging.getLogger(__name__)

#: Seconds between looks for videos nobody woke this for.
EVERY = 3600.0
#: Videos read from the index at a time.
PAGE = 50
#: Seconds of sound handed to the speech model in one call.
WINDOW = 600.0
#: How long one window may take, at least, and per second of sound, before
#: the host is taken to be hung (as runtime/transcriber.py: large-v3 on four
#: cloud cores took six seconds per second of speech, and a Tamil window is
#: read twice).
LEAST_SECONDS = 900.0
PER_SECOND = 20.0


def windows(duration: float, window: float = WINDOW) -> list[tuple[float, float]]:
    """``(offset, length)`` of each window of a video *duration* seconds long.
    A last window of under a minute is folded into the one before it."""
    duration = max(0.0, float(duration or 0))
    found: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        length = min(window, duration - start)
        if found and length < 60:
            offset, before = found.pop()
            found.append((offset, before + length))
            break
        found.append((start, length))
        start += window
    return found or [(0.0, window)]


class WordListener:
    """Keeps every home video's spoken words up to date while ``video_words`` is on."""

    def __init__(self, cfg: Any, connect_db: Callable[[], sqlite3.Connection], *,
                 host: Callable[[], ModelHost],
                 hold: Callable[[], str | None] | None = None,
                 every: float = EVERY) -> None:
        self.cfg = cfg
        self._connect = connect_db
        self._host = host
        self._hold = hold
        self._every = every
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False, "done": 0, "failed": 0, "owed": 0,
                                       "message": "", "seconds": 0.0, "heard": 0.0,
                                       "clip": None, "finished_at": 0.0}

    @property
    def enabled(self) -> bool:
        from ..server import features                      # noqa: PLC0415
        return features.on(self.cfg, "video_words") and \
            features.on(self.cfg, "ai_runtime")

    @property
    def running(self) -> bool:
        return bool(self._state["running"])

    def keep(self) -> None:
        """Start the background thread (once); it does nothing while off."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-video-words",
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
        # A first look a while after start-up, after the voice stories'.
        self._wake.wait(240)
        while not self._stop.is_set():
            self._wake.clear()
            if self.enabled:
                try:
                    self.run()
                except Exception:                             # noqa: BLE001 - next time
                    log.exception("could not write down what is said in the videos")
            self._wake.wait(self._every)

    def _say(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def ready(self) -> str | None:
        """Why the videos cannot be listened to on this computer now, or None."""
        if not media.FFMPEG:
            return said("ffmpeg is not installed")
        from ..runtime.transcriber import ready                  # noqa: PLC0415
        return ready()

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
        """One pass over every video owed. Returns the state at its end."""
        why_not = self.ready()
        if why_not:
            self._say(message=why_not, owed=0)
            return self.status()
        version = video_words.wanted_version(speech.version_of(speech.chosen_model()))
        conn = self._connect()
        self._say(running=True, done=0, failed=0, message="", seconds=0.0, heard=0.0,
                  owed=video_words.count_owed(conn, version))
        try:
            after = 0
            while not self._stop.is_set() and self.enabled:
                page = video_words.owed(conn, version, limit=PAGE, after=after)
                if not page:
                    break
                after = int(page[-1]["id"])
                for row in page:
                    if not self._wait_for_the_household() or not self.enabled:
                        return self.status()
                    self._say(clip=int(row["id"]))
                    if not self._clip(conn, row, version):
                        return self.status()
        finally:
            self._say(running=False, clip=None, finished_at=time.time(),
                      owed=video_words.count_owed(conn, version))
        return self.status()

    def _clip(self, conn: sqlite3.Connection, row: Any, version: int) -> bool:
        """Listen to one video. False when the pass should end."""
        path = Path(row["root"]) / row["rel_path"]
        mtime = float(row["mtime"] or 0)
        if not path.is_file():
            # Its drive is away, or it is gone and the next scan says so:
            # left owed, not counted against.
            return True
        started = time.monotonic()
        languages = speech.languages(getattr(self.cfg, "speech_languages", "ta,en"))
        pieces: list[dict[str, Any]] = []
        duration = float(row["duration"] or 0)
        for offset, length in windows(duration):
            if not self._wait_for_the_household():
                return False
            try:
                result = self._host().call(
                    "transcribe", stop=self._stop,
                    timeout=max(LEAST_SECONDS, PER_SECOND * length),
                    path=str(path), ffmpeg=media.FFMPEG, languages=languages,
                    translate=True, offset=offset, length=length)
            except HostStopped:
                return False
            except Unavailable as exc:
                # Not this video's fault: the model went, or memory ran short
                # before it loaded. Said on the console; the videos stay owed.
                self._say(message=str(exc))
                return False
            except HostError as exc:
                log.info("what is said in %s could not be written down: %s", path, exc)
                video_words.fail(conn, int(row["id"]), str(exc), version=version, mtime=mtime)
                self._say(failed=self._state["failed"] + 1)
                return True
            language = str(result.get("language") or "")
            pieces += [{**piece, "language": language} for piece in result.get("segments") or []]
        lines = video_words.lines_of(pieces)
        if video_words.store(conn, int(row["id"]), lines, version=version, mtime=mtime):
            log.info("video %s: %d lines written down in %.0f s", row["id"], len(lines),
                     time.monotonic() - started)
        self._say(done=self._state["done"] + 1,
                  seconds=self._state["seconds"] + time.monotonic() - started,
                  heard=self._state["heard"] + duration,
                  owed=max(0, self._state["owed"] - 1))
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["enabled"] = self.enabled
        done = int(state.get("done") or 0)
        state["per_clip"] = round(state["seconds"] / done, 1) if done else None
        state["model"] = speech.chosen_model()
        return state
