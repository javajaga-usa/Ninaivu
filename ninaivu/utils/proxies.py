"""Playable copies of videos a browser cannot open.

A browser plays H.264 in MP4 and little else. A household archive swept up
from old drives is full of what camcorders and capture cards actually wrote:
AVI, MKV, WMV, MTS, FLV. Those files are perfectly good — they simply cannot
be handed to a ``<video>`` tag, and until now the viewer said so and stopped.

So Ninaivu keeps a second, playable copy beside the index and serves that
instead. Three properties make this safe to do:

*The original is never touched.* It is opened read-only and re-encoded into a
new file somewhere else entirely. The "nothing is re-encoded" promise the
archive makes is about your library; this is a derivative, in the same spirit
as a thumbnail, and it lives in the state directory with the thumbnails.

*The proxy is never part of the library.* It is not indexed, never scanned,
never backed up to the cloud, and deleting the whole folder costs nothing but
the time to rebuild.

*It is disposable.* The folder is capped, and the least recently played copies
are dropped when it is full, so a big library cannot quietly fill a disk.

Sound is the same problem, smaller: WMA, AIFF, ALAC. Those are converted to
AAC in the same container, and kept in the same folder.

**It plays while it converts.** A whole conversion of a two-hour film is
minutes of somebody looking at a progress bar. So beside the copy being made,
the viewer can be sent a *live* conversion (:func:`live`): ffmpeg writing a
fragmented MP4 to the response as it goes, which a browser plays from the
first second. It cannot be skipped through — there is no end to seek to yet —
and the viewer swaps to the finished copy, at the same moment, once there is
one.

Without ffmpeg there are no proxies and the viewer says what it said before.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..server.config import BROWSER_NATIVE
from .source_version import matches_source, source_version, stamp_source

#: Before every library file ffmpeg reads (``media.LOCAL_ONLY``): from the
#: disk only, never an address a playlist disguised as a video names.
LOCAL_ONLY = ("-protocol_whitelist", "file")

#: Bumped when the encode changes in a way that makes older copies wrong.
#: 2: the copies leave the original's metadata behind (the place a video was
#: filmed among it), as the copy a guest is given does.
PROXY_VERSION = 2

#: A playable copy carries the picture and the sound, not the original's
#: metadata: a family member whose copies leave out the home location was
#: otherwise handed it in the converted video.
NO_METADATA = ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"]

#: The longest edge of a proxy. 720p is the point where a phone on the sofa
#: cannot tell the difference and a laptop can still decode it in software.
PROXY_HEIGHT = 720

#: Default ceiling for the whole folder. Roughly forty hours of 720p.
DEFAULT_CACHE_MB = 4096

#: How many conversions run at once. Each is a full software H.264 encode;
#: without a ceiling, opening a folder of old camcorder clips — or anybody
#: signed in asking for one video after another — started one per clip and
#: took the whole machine with them. The rest wait their turn.
MAX_CONVERSIONS = 2

#: The least time a conversion is allowed before it is presumed stuck, and
#: how many times the clip's own length it may take beyond that.
MIN_CONVERSION_SECONDS = 15 * 60
CONVERSION_SPEED_ALLOWANCE = 6

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def ffmpeg_path() -> str | None:
    from ..media import media  # noqa: PLC0415 - read at call time so tests can patch
    return media.FFMPEG


def proxies_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "proxies"


def proxy_path(state_dir: Path | str, asset_id: int) -> Path:
    return proxies_dir(state_dir) / f"{int(asset_id)}-v{PROXY_VERSION}.mp4"


def needs_proxy(ext: str, kind: str = "video") -> bool:
    """Whether this file has to be converted before a browser will play it."""
    if kind not in ("video", "audio"):
        return False
    return f".{str(ext).lower().lstrip('.')}" not in BROWSER_NATIVE


def _video_args(threads: int, preset: str, crf: str) -> list[str]:
    return ["-vf", f"scale=-2:'min({PROXY_HEIGHT},ih)'",
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-threads", str(threads), "-pix_fmt", "yuv420p"]


AUDIO_ARGS = ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]


@dataclass
class BuildState:
    """What a running or finished conversion looks like to the API."""

    asset_id: int
    state: str = "idle"          # idle | building | ready | failed | unavailable
    percent: int = 0
    message: str = ""
    started_at: float = field(default_factory=time.time)

    def payload(self) -> dict[str, Any]:
        return {"asset_id": self.asset_id, "state": self.state,
                "percent": self.percent, "message": self.message}


class ProxyStore:
    """The folder of playable copies, and the conversions filling it."""

    def __init__(self, state_dir: Path | str, cache_mb: int = DEFAULT_CACHE_MB) -> None:
        self.state_dir = Path(state_dir)
        self.cache_mb = int(cache_mb)
        self._builds: dict[int, BuildState] = {}
        self._guard = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONVERSIONS)

    # -- reading ----------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(ffmpeg_path())

    def path_for(self, asset_id: int) -> Path:
        return proxy_path(self.state_dir, asset_id)

    def ready(self, asset_id: int, source: Path | None = None) -> Path | None:
        path = self.path_for(asset_id)
        if source is not None and not matches_source(path, source):
            return None
        if path.exists() and path.stat().st_size > 0:
            # Playing something keeps it alive: eviction is by last use, and
            # on most systems reading does not update mtime.
            try:
                os.utime(path, None)
            except OSError:
                pass
            return path
        return None

    def building(self) -> list[BuildState]:
        """Every conversion running right now, for the activity strip.

        Two ffmpeg processes with the processor to themselves is the heaviest
        thing Ninaivu does, and it used to be visible only to whoever opened
        the video.
        """
        with self._guard:
            return [state for state in self._builds.values()
                    if state.state == "building"]

    def status(self, asset_id: int, source: Path | None = None) -> BuildState:
        if self.ready(asset_id, source):
            return BuildState(asset_id, "ready", 100)
        with self._guard:
            existing = self._builds.get(int(asset_id))
        if existing and (existing.state != "ready" or source is None):
            return existing
        if not self.available:
            return BuildState(asset_id, "unavailable", 0,
                              "ffmpeg is not installed, so this video cannot "
                              "be converted for the browser.")
        return BuildState(asset_id, "idle", 0)

    # -- building ---------------------------------------------------------
    def start(self, asset_id: int, source: Path, duration: float | None = None,
              kind: str = "video") -> BuildState:
        """Begin a conversion unless one is already running or done.

        *kind* ``audio`` makes a sound-only copy; anything else, a video.
        """
        asset_id = int(asset_id)
        if self.ready(asset_id, source):
            return BuildState(asset_id, "ready", 100)
        if not self.available:
            return self.status(asset_id, source)

        with self._guard:
            running = self._builds.get(asset_id)
            if running and running.state == "building":
                return running
            state = BuildState(asset_id, "building", 0, "Converting…")
            self._builds[asset_id] = state

        thread = threading.Thread(
            target=self._run, args=(asset_id, Path(source), duration, kind),
            name=f"proxy-{asset_id}", daemon=True)
        thread.start()
        return state

    def _run(self, asset_id: int, source: Path, duration: float | None,
             kind: str = "video") -> None:
        state = self._builds[asset_id]
        target = self.path_for(asset_id)
        # A temporary name, renamed into place only once ffmpeg is happy, so a
        # killed process can never leave a half-encoded file that looks ready.
        tmp = target.with_suffix(f".{os.getpid()}.part.mp4")
        if not self._slots.acquire(blocking=False):
            state.message = "Waiting for another video to finish converting…"
            self._slots.acquire()
            state.message = "Converting…"
        try:
            self._convert(asset_id, state, source, target, tmp, duration, kind)
        finally:
            self._slots.release()

    def _convert(self, asset_id: int, state: BuildState, source: Path,
                 target: Path, tmp: Path, duration: float | None,
                 kind: str = "video") -> None:
        import tempfile

        try:
            version = source_version(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not source.exists():
                self._finish(asset_id, "failed", "The original file is missing.")
                return

            from .resources import budget
            threads = budget()['compute_threads']
            picture = (["-vn"] if kind == "audio"
                       else _video_args(threads, "veryfast", "23"))
            command = [
                ffmpeg_path(), "-v", "error", "-nostdin", "-y",
                "-threads", str(threads),
                "-filter_threads", str(threads),
                *LOCAL_ONLY, "-i", str(source),
                *NO_METADATA,
                *picture,
                *(AUDIO_ARGS if kind == "audio"
                  else ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]),
                # Puts the index at the front so the browser can start playing
                # before the whole file has arrived.
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats",
                str(tmp),
            ]
            # Errors go to a file, not a pipe. Progress is read from stdout to
            # the end before anything else, so a stderr pipe nobody drained
            # filled up on a damaged clip — ffmpeg blocked writing its
            # complaints, stdout never closed, and the conversion said
            # "Converting…" for ever.
            limit = MIN_CONVERSION_SECONDS + CONVERSION_SPEED_ALLOWANCE * float(duration or 0)
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                        errors="replace") as log:
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=log,
                    universal_newlines=True)
                watchdog = threading.Timer(limit, process.kill)
                watchdog.daemon = True
                watchdog.start()
                try:
                    self._track(process, state, duration)
                    process.wait()
                finally:
                    timed_out = not watchdog.is_alive() and process.returncode != 0
                    watchdog.cancel()
                log.seek(0)
                errors = log.read()[-4000:]

            if process.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
                tmp.unlink(missing_ok=True)
                if timed_out:
                    message = "Conversion took too long and was stopped."
                else:
                    message = (errors.strip().splitlines()[-1][:200]
                               if errors.strip() else "Conversion failed.")
                self._finish(asset_id, "failed", message)
                return

            if source_version(source) != version:
                raise OSError("source changed during conversion")
            tmp.replace(target)
            stamp_source(target, version)
            self.evict()
            self._finish(asset_id, "ready", "")
        except Exception as exc:  # noqa: BLE001 - a failed proxy is not fatal
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self._finish(asset_id, "failed", str(exc)[:200])

    @staticmethod
    def _track(process, state: BuildState, duration: float | None) -> None:
        """Follow ffmpeg's progress stream so the viewer can show a bar."""
        if not process.stdout:
            return
        total = float(duration or 0)
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_ms" and total > 0:
                try:
                    done = int(value) / 1_000_000.0
                    state.percent = max(0, min(99, int(done / total * 100)))
                except ValueError:
                    pass
            elif key == "progress" and value == "end":
                state.percent = 100

    def _finish(self, asset_id: int, state_name: str, message: str) -> None:
        with self._guard:
            state = self._builds.get(asset_id)
            if state:
                state.state = state_name
                state.message = message
                state.percent = 100 if state_name == "ready" else state.percent

    # -- housekeeping -----------------------------------------------------
    def usage(self) -> dict[str, Any]:
        directory = proxies_dir(self.state_dir)
        files = []
        if directory.exists():
            for path in directory.glob("*.mp4"):
                if path.name.endswith(".part.mp4"):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
        return {
            "count": len(files),
            "bytes": sum(size for _, size, _ in files),
            "cap_bytes": self.cache_mb * 1024 * 1024,
            "dir": str(directory),
        }

    def evict(self) -> int:
        """Drop the least recently played copies until the folder fits.

        Least *recently used*, not oldest: playing a proxy touches it, so the
        film the household watches every Christmas is not thrown away to make
        room for one somebody opened by accident.
        """
        directory = proxies_dir(self.state_dir)
        if not directory.exists():
            return 0
        cap = self.cache_mb * 1024 * 1024
        if cap <= 0:
            return 0
        entries = []
        for path in directory.glob("*.mp4"):
            if path.name.endswith(".part.mp4"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in entries)
        if total <= cap:
            return 0
        removed = 0
        for _, size, path in sorted(entries):        # oldest use first
            if total <= cap:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def forget(self, asset_id: int) -> bool:
        path = self.path_for(asset_id)
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def clear(self) -> int:
        directory = proxies_dir(self.state_dir)
        if not directory.exists():
            return 0
        removed = 0
        for path in directory.glob("*.mp4"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed


# ---------------------------------------------------------------------------
# Playing while it converts
# ---------------------------------------------------------------------------

#: Live conversions at once, across everybody. Each is a real-time encode; the
#: finished copies being made beside them are limited separately.
MAX_LIVE = 3
_live_slots = threading.BoundedSemaphore(MAX_LIVE)

#: Bytes handed to the browser at a time.
LIVE_CHUNK = 64 * 1024


class Busy(RuntimeError):
    """Every live conversion slot is taken."""


def live_command(source: str, kind: str = "video") -> list[str]:
    """ffmpeg, writing a playable copy of *source* to its standard output.

    Fragmented MP4 — an empty index up front and a fragment at every key
    frame — is what a browser can play from a response with no end in sight.
    The fastest encoder settings: this has to keep ahead of somebody watching,
    and the finished copy being made beside it is the one that is kept.
    ``-`` as *source* reads from standard input.
    """
    from .resources import budget
    threads = max(1, int(budget()["compute_threads"]))
    picture = ["-vn"] if kind == "audio" else _video_args(threads, "ultrafast", "26")
    # -nostdin keeps ffmpeg off a terminal it does not own — except when its
    # standard input is the video itself.
    # And reads only what it was given: the file, or what arrives on its
    # standard input, never an address a playlist inside either names.
    reading = (["-protocol_whitelist", "pipe", "-i", "pipe:0"] if source == "-"
               else ["-nostdin", *LOCAL_ONLY, "-i", source])
    return [ffmpeg_path() or "ffmpeg", "-v", "error", *reading,
            *NO_METADATA, *picture, *AUDIO_ARGS,
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1"]


def live(source: Path | str | None, kind: str = "video", *,
         feed=None) -> "Any":
    """A generator of a live conversion's bytes, or :class:`Busy`.

    *source* is a file; or None with *feed*, an iterator of the original's
    bytes, which is written to ffmpeg as it reads — how a file that is in
    Google Drive and not on this disk is played. Closing the generator (the
    browser went away) stops ffmpeg at once: an encode nobody is watching is
    only heat.
    """
    if not ffmpeg_path():
        raise OSError("ffmpeg is not installed")
    if not _live_slots.acquire(blocking=False):
        raise Busy("Too many videos are being converted to play right now.")
    try:
        process = subprocess.Popen(
            live_command("-" if feed is not None else str(source), kind),
            stdin=subprocess.PIPE if feed is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        _live_slots.release()
        raise

    def pour() -> None:
        try:
            for piece in feed:
                process.stdin.write(piece)
        except (BrokenPipeError, OSError, ValueError):
            pass                    # ffmpeg stopped reading: it is done or killed
        except Exception:           # noqa: BLE001 — the original could not be fetched
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    if feed is not None:
        threading.Thread(target=pour, name="proxy-live-feed", daemon=True).start()

    def stream():
        try:
            while True:
                chunk = process.stdout.read1(LIVE_CHUNK) if hasattr(process.stdout, "read1") \
                    else process.stdout.read(LIVE_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                pass
            _live_slots.release()

    # Started here, not by whoever reads it: a generator that never started
    # does not run its `finally` when dropped, and that is where ffmpeg is
    # stopped and the slot given back. This also means a file ffmpeg cannot
    # read is an error now, while there is still a response to send instead.
    flowing = stream()
    try:
        first = next(flowing)
    except StopIteration:
        raise OSError("ffmpeg could not convert this file") from None

    def whole():
        try:
            yield first
            yield from flowing
        finally:
            flowing.close()

    return whole()
