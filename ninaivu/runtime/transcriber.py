"""Writing down voice stories in the background.

Every voice story without words, or with words from an older model, is handed
to the model host to be written down (runtime/speech.py), and what comes back
is kept with the story (storage/story_words.py), where the gallery's search
reads it and the story's route hands it to the page.

When it runs: shortly after somebody tells a story (:meth:`Transcriber.wake`,
from the story's route), after a speech model or the speech reader is
installed, and once every :data:`EVERY` seconds in case anything was missed.
Only with the on-device AI engine switched on (``ai_runtime``), a speech model
downloaded, and the speech reader installed; otherwise it does nothing and
starts nothing.

It is analysis, so it gives way to the household as every analysis pass does
(server/workload.py): it waits while a video plays, and in overnight mode
until the night. A story is never interrupted part way to give way, because a
model host stopped mid-story would have to start that story again.
"""
from __future__ import annotations

import importlib.util
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..storage import stories, story_words
from ..words import said
from . import speech
from .host import HostError, HostStopped, ModelHost, Unavailable

log = logging.getLogger(__name__)

#: Seconds between looks for stories nobody woke this for.
EVERY = 1800.0
#: How long one story may take, at least, and per second of recording,
#: before the host is taken to be hung. large-v3 on four cores of a cloud
#: machine took six seconds per second of English (37 s in 215 s), and a
#: Tamil story is read twice (words, then English); this is well past both.
LEAST_SECONDS = 900.0
PER_SECOND = 20.0


def ready() -> str | None:
    """Why stories cannot be written down on this computer, or None."""
    if importlib.util.find_spec("sherpa_onnx") is None:
        return said("the speech reader is not installed (Voice stories as words, under Extras on the Home & extensions page)")
    if speech.chosen_model() is None:
        return said("no speech model is downloaded (AI models page)")
    return None


class Transcriber:
    """Keeps every voice story's words up to date while the engine is on."""

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
        self._state: dict[str, Any] = {"running": False, "done": 0, "failed": 0,
                                       "owed": 0, "message": "", "story": None,
                                       "finished_at": 0.0}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "ai_runtime", False))

    @property
    def running(self) -> bool:
        return bool(self._state["running"])

    def keep(self) -> None:
        """Start the background thread (once); it does nothing while off."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-story-words",
                                        daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Look for stories to write down now, not at the next half hour."""
        self._wake.set()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        self._wake.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(10)

    def _loop(self) -> None:
        # A first look shortly after start-up, out of the way of the scan.
        self._wake.wait(60)
        while not self._stop.is_set():
            self._wake.clear()
            if self.enabled:
                try:
                    self.run()
                except Exception:                             # noqa: BLE001 - next time
                    log.exception("could not write down the voice stories")
            self._wake.wait(self._every)

    def _say(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

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
        """One pass over every story owed. Returns the state at its end."""
        why_not = ready()
        if why_not:
            self._say(message=why_not, owed=0)
            return self.status()
        model = speech.chosen_model()
        version = speech.version_of(model)
        conn = self._connect()
        folder = stories.folder_for(self.cfg.state_dir)
        self._say(running=True, done=0, failed=0, message="",
                  owed=story_words.count_owed(conn, version))
        try:
            for story_id in story_words.owed(conn, version):
                if not self._wait_for_the_household():
                    break
                story = stories.get(conn, story_id)
                if story is None:
                    continue                             # taken away meanwhile
                self._say(story=story_id)
                if not self._one(conn, story, folder, version):
                    break
        finally:
            self._say(running=False, story=None, finished_at=time.time(),
                      owed=story_words.count_owed(conn, version))
        return self.status()

    def _one(self, conn: sqlite3.Connection, story: dict[str, Any], folder: Path,
             version: int) -> bool:
        """Write down one story. False when the pass should end."""
        from ..media import media                                 # noqa: PLC0415

        path = folder / str(story["file"])
        if not path.is_file():
            story_words.fail(conn, story["id"], version, "the recording is not there")
            self._say(failed=self._state["failed"] + 1)
            return True
        seconds = float(story.get("duration") or 0) or path.stat().st_size / 4000
        try:
            result = self._host().call(
                "transcribe", stop=self._stop,
                timeout=max(LEAST_SECONDS, PER_SECOND * seconds),
                path=str(path), ffmpeg=media.FFMPEG,
                languages=speech.languages(getattr(self.cfg, "speech_languages", "ta,en")),
                translate=True)
        except HostStopped:
            return False
        except Unavailable as exc:
            # Not this story's fault: the model went, or memory ran short
            # before it loaded. Said on the console; the stories stay owed.
            self._say(message=str(exc))
            return False
        except HostError as exc:
            log.warning("voice story %s could not be written down: %s", story["id"], exc)
            story_words.fail(conn, story["id"], version, str(exc))
            self._say(failed=self._state["failed"] + 1)
            return True
        if story_words.store(conn, story["id"], result):
            self._say(done=self._state["done"] + 1)
            log.info("voice story %s written down (%s, %s s of sound in %s s)", story["id"],
                     result.get("language"), result.get("seconds"), result.get("took"))
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["enabled"] = self.enabled
        state["model"] = speech.chosen_model()
        return state
