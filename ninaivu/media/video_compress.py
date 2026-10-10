"""Making a large video smaller, from the largest-files worklist.

Keep and Delete were the only two answers that list had, and for a video the
right one is often neither: a two-hour 4K recording of a wedding is worth
keeping and not worth 40 GB. So there are two more.

* **Compress** writes a smaller copy beside the original, as an MP4 with
  H.264 video and AAC sound, no larger than 1080p, and adds it to the library
  with the original's date, place and visibility. The original is not touched.
* **Replace** does the same, checks the copy, and only then swaps it in for
  the original. The original goes into the library's bin first
  (``_deleted/_originals``, the same safety copy turning a file makes — see
  :func:`recycle.keep_original`), and if that copy cannot be made, nothing is
  replaced. The item keeps its place in albums, its favourites and its faces,
  because it is the same item with smaller bytes.

The new file is always an MP4, whatever the original was: it is what every
browser and phone plays. A Replace of ``wedding.avi`` therefore leaves
``wedding.mp4`` where it was.

The copy is checked before anything else happens: it has to open, run as long
as the original, give a picture, and actually be smaller. One that is not
smaller (a phone clip that was already compressed well) is thrown away and
said so, rather than swapping a file for a larger one.

Encoding takes the processor for a long time, so one video is compressed at a
time, on one background thread, and the rest wait in order. Jobs live in
memory, like :mod:`.jobs`: a restart forgets the queue, and a half-written
copy is a hidden ``.tmp`` file that no scan indexes. Its name is the video's
(:func:`temporary_for`), so the next compression of that video removes it,
and start-up clears any left beside the library's videos
(:func:`clear_leftovers`): it used to carry the job's random id, so a restart
in the middle of an encode left gigabytes nothing would ever look for again.

Needs ffmpeg with an H.264 encoder. Without one the buttons say why and do
nothing; nothing else in Ninaivu changes.

On a Mac the encode runs on the video engine built into the chip
(VideoToolbox) when ffmpeg has it, which is several times faster than the
processor and leaves the processor to the family. That engine has no
quality-for-size dial like x264's, so it is given a bitrate instead: what
1080p H.264 needs to look like the original, scaled down for smaller
pictures and never more than half of what the original spends. If it fails
for a particular video, the same video is compressed again on the processor,
as it would be anywhere else.

A video whose file was cut short where it came from (a camera that lost
power, a copy that stopped) has no index: MP4 and MOV keep the list of where
each frame is in one block, usually written last, and without it nothing can
open the file. ffmpeg says "moov atom not found". Nothing here can rebuild
that block from the file alone, so such a video is told apart before any
encode starts (:func:`damaged`), the job ends with a plain sentence saying so
instead of ffmpeg's, the processor is not tried after the video engine, and
the largest-files list marks the video so Keep or Delete is the answer.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import media
from ..words import said

log = logging.getLogger(__name__)

#: The one setting offered, and the one the console calls recommended:
#: visually close to the original for family video, usually a third to a half
#: of a phone's or camcorder's size, and playable everywhere.
PRESET = {
    "codec": "H.264",
    "max_lines": 1080,       # the shorter side, so a portrait clip stays upright-sized
    "crf": 23,
    "speed": "medium",
    "audio_kbps": 128,
}
#: How the console names it, in English; the locales carry the Tamil.
PRESET_LABEL = said("MP4 (H.264), up to 1080p")

OUTPUT_EXT = "mp4"
#: A copy that runs this much shorter or longer than the original is not a
#: copy of it: a cut-off encode, or a stream ffmpeg could not read through.
DURATION_SLACK = 0.02
DURATION_SLACK_MIN = 1.5

MODES = ("copy", "replace")
KEEP_FOR = 30 * 60
#: Finished jobs remembered at most; a long evening of compressing is fine.
MAX_REMEMBERED = 200

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()
_worker: threading.Thread | None = None
_encoder: str | None | bool = False      # False: not looked for yet
_hardware: str | None | bool = False     # the same, for the Mac's video engine

HARDWARE = "h264_videotoolbox"
#: Bitrate for a 1080p picture on the video engine, which has no CRF: about
#: what x264 at CRF 23 spends on family video. Smaller pictures get less.
HARDWARE_1080P_BPS = 6_000_000
HARDWARE_MIN_BPS = 1_000_000
#: Never more than this share of what the original spends per second, so the
#: copy comes out smaller even when the original was already lean.
HARDWARE_SHARE = 0.5


class CompressError(RuntimeError):
    """A compression could not be started or did not finish."""


class DamagedVideo(CompressError):
    """The original itself cannot be opened: no encoder would do better."""


DAMAGED = said("This video is damaged at its source: the file was cut short, so its index is missing and it cannot be played or compressed. Keep it or delete it; only an older copy from elsewhere can bring it back.")  # noqa: E501

#: What ffmpeg and ffprobe say about a file whose bytes cannot be read as a
#: video at all, as opposed to a disk, a permission or an encoder problem.
_DAMAGE_SIGNS = ("moov atom not found", "invalid data found when processing input")

#: (path, size, mtime) → damaged, so a list read again does not probe again.
_damage_seen: dict[tuple[str, int, float], bool] = {}


def is_damage(message: str) -> bool:
    """Whether ffmpeg's or ffprobe's error output means a damaged file."""
    text = (message or "").lower()
    return any(sign in text for sign in _DAMAGE_SIGNS)


def damaged(path: Path) -> bool:
    """Whether the video at *path* cannot be opened because its file is
    damaged. False when it opens, when it is missing, or when nothing can
    tell (no ffprobe, or it ran out of time): only a sure answer marks it."""
    try:
        stat = path.stat()
    except OSError:
        return False
    key = (str(path), stat.st_size, stat.st_mtime)
    if key in _damage_seen:
        return _damage_seen[key]
    if not media.FFPROBE:
        return False
    try:
        proc = subprocess.run([media.FFPROBE, "-v", "error", *media.LOCAL_ONLY,
                               "-show_entries", "format=duration", "-of", "csv=p=0",
                               str(path)],
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    answer = proc.returncode != 0 and is_damage(proc.stderr.decode("utf-8", "replace"))
    _damage_seen[key] = answer
    return answer


# --- can this machine do it --------------------------------------------------

def _look_for_encoders() -> None:
    global _encoder, _hardware
    software: str | None = None
    hardware: str | None = None
    if media.FFMPEG:
        try:
            proc = subprocess.run([media.FFMPEG, "-hide_banner", "-v", "quiet", "-encoders"],
                                  capture_output=True, timeout=20, check=False)
            listing = proc.stdout.decode("utf-8", "replace")
            for name in ("libx264", "libopenh264"):
                if f" {name} " in listing:
                    software = name
                    break
            if sys.platform == "darwin" and f" {HARDWARE} " in listing:
                hardware = HARDWARE
        except (OSError, subprocess.SubprocessError):
            pass
    _encoder = software or ""
    _hardware = hardware or ""


def encoder() -> str | None:
    """The H.264 encoder this ffmpeg has, or None. Looked for once.

    The processor's encoder when there is one, since it is the one every
    video can fall back to; the Mac's video engine otherwise."""
    if _encoder is False:
        _look_for_encoders()
    return _encoder or hardware_encoder() or None


def hardware_encoder() -> str | None:
    """The Mac's built-in video encoder, if this ffmpeg can use it."""
    if _hardware is False:
        _look_for_encoders()
    return _hardware or None


def hardware_bitrate(width: int, height: int, size: int, duration: float) -> int:
    """Bits per second for the video engine (see the module's note)."""
    lines = PRESET["max_lines"]
    short, long_ = sorted((max(1, int(width or 0)), max(1, int(height or 0))))
    if not width or not height:
        short, long_ = lines, lines * 16 // 9
    scale = min(1.0, lines / short)
    pixels = (short * scale) * (long_ * scale)
    rate = HARDWARE_1080P_BPS * pixels / (1920 * 1080)
    if size > 0 and duration > 0:
        rate = min(rate, size * 8 / duration * HARDWARE_SHARE)
    return int(max(HARDWARE_MIN_BPS, rate))


def unavailable_reason() -> str | None:
    """Why Compress and Replace cannot run here, in the console's English."""
    if not media.FFMPEG:
        return said("Compressing videos needs ffmpeg, which is not installed on this computer.")
    if not encoder():
        return said("This computer's ffmpeg has no H.264 encoder, so it cannot compress videos.")
    return None


def _reset_for_tests() -> None:
    global _encoder, _hardware
    _encoder = False
    _hardware = False
    _damage_seen.clear()
    with _lock:
        _jobs.clear()


# --- the encode itself ---------------------------------------------------------

def command(source: Path, target: Path, enc: str, threads: int | None = None,
            bitrate: int | None = None) -> list[str]:
    """The ffmpeg command for the preset. *target* may end in ``.tmp``, so the
    container is named rather than guessed from the extension."""
    lines = PRESET["max_lines"]
    # The shorter side down to 1080 lines at most, never up, and even: H.264
    # in 4:2:0 refuses odd sizes. ffmpeg has already turned a rotated phone
    # clip upright by the time this runs, so iw and ih are as it is watched.
    scale = (f"scale=w='if(gte(iw,ih),-2,trunc(min(iw,{lines})/2)*2)'"
             f":h='if(gte(iw,ih),trunc(min(ih,{lines})/2)*2,-2)'")
    video = ["-c:v", enc, "-pix_fmt", "yuv420p"]
    decode: list[str] = []
    if enc == "libx264":
        video += ["-preset", PRESET["speed"], "-crf", str(PRESET["crf"])]
    elif enc == HARDWARE:
        rate = int(bitrate or HARDWARE_1080P_BPS)
        video += ["-b:v", str(rate), "-maxrate", str(rate * 3 // 2),
                  "-bufsize", str(rate * 2), "-profile:v", "high"]
        # Reading the original on the same engine too: a 4K HEVC phone clip
        # is as much work to decode as the encode is.
        decode = ["-hwaccel", "videotoolbox"]
    else:                                     # openh264 has no CRF; a fair bitrate instead
        video += ["-b:v", "4M"]
    cmd = [media.FFMPEG, "-v", "error", "-nostdin", "-y", *media.LOCAL_ONLY,
           *decode, "-i", str(source),
           "-map", "0:v:0", "-map", "0:a:0?",
           # The date it was filmed and where: this is the family's own copy,
           # and the index reads its date from here at the next scan.
           "-map_metadata", "0",
           "-vf", scale, *video,
           "-c:a", "aac", "-b:a", f"{PRESET['audio_kbps']}k",
           "-movflags", "+faststart+use_metadata_tags",
           "-progress", "pipe:1", "-nostats"]
    if threads:
        cmd += ["-threads", str(threads)]
    cmd += ["-f", "mp4", str(target)]
    return cmd


def _threads() -> int | None:
    """Leave one core for the people using Ninaivu meanwhile."""
    count = os.cpu_count() or 1
    return max(1, count - 1) if count > 2 else None


def encode(source: Path, target: Path, duration: float,
           report: Callable[[float], None], cancelled: Callable[[], bool]) -> None:
    """Run the encode, reporting progress from 0 to 1. Raises CompressError.

    On the Mac's video engine first when there is one; a video it cannot do
    is done again on the processor, from the start."""
    encoder()                                 # looks for both, once
    software = _encoder or None
    hardware = hardware_encoder()
    if not (software or hardware):
        raise CompressError(unavailable_reason() or "Cannot compress here.")
    if damaged(source):
        raise DamagedVideo(DAMAGED)
    if hardware:
        info = media.probe_video(source)
        try:
            size = source.stat().st_size
        except OSError:
            size = 0
        rate = hardware_bitrate(info.get("width") or 0, info.get("height") or 0,
                                size, duration or float(info.get("duration") or 0))
        try:
            _ffmpeg(command(source, target, hardware, None, rate), duration, report, cancelled)
            return
        except CompressError as exc:
            if cancelled() or not software or isinstance(exc, DamagedVideo):
                raise
            log.info("The video engine could not compress %s; using the processor.", source.name)
            report(0.0)
    _ffmpeg(command(source, target, software, _threads()), duration, report, cancelled)


def _ffmpeg(cmd: list[str], duration: float,
         report: Callable[[float], None], cancelled: Callable[[], bool]) -> None:
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise CompressError(f"ffmpeg could not be started: {exc}") from exc
    errors: list[bytes] = []
    reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()),
                              daemon=True)
    reader.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if cancelled():
                proc.terminate()
                break
            line = raw.decode("ascii", "replace").strip()
            key, _, value = line.partition("=")
            if key in ("out_time_us", "out_time_ms") and duration > 0:
                try:
                    # Both are microseconds; out_time_ms is misnamed in ffmpeg.
                    report(min(1.0, max(0.0, int(value) / 1_000_000 / duration)))
                except ValueError:
                    pass
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(timeout=5)
    if cancelled():
        raise CompressError(said("Stopped."))
    if proc.returncode != 0:
        tail = b"".join(errors).decode("utf-8", "replace").strip()[-300:]
        if is_damage(tail):
            raise DamagedVideo(DAMAGED)
        raise CompressError(f"ffmpeg could not compress this video: {tail or proc.returncode}")


def verify(source_duration: float, original_size: int, target: Path) -> dict[str, Any]:
    """Check the new file before anything relies on it. Raises CompressError."""
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise CompressError(said("The compressed copy was not written.")) from exc
    if size <= 0:
        raise CompressError(said("The compressed copy was empty."))
    info = media.probe_video(target)
    if not info.get("width") or not info.get("height"):
        raise CompressError(said("The compressed copy could not be read back."))
    if source_duration and source_duration > 0:
        length = float(info.get("duration") or 0)
        slack = max(DURATION_SLACK_MIN, source_duration * DURATION_SLACK)
        if abs(length - source_duration) > slack:
            raise CompressError(
                f"The compressed copy runs {length:.0f} s, not {source_duration:.0f} s, "
                "so it was not kept.")
    if size >= original_size:
        raise CompressError(said("Compressing would not make this video any smaller, so nothing was changed."))
    frame = media.extract_video_frame(target, 1.0)
    if frame is None:
        raise CompressError(said("The compressed copy gives no picture, so it was not kept."))
    info["size"] = size
    info["frame"] = frame
    return info


# --- names on disk ---------------------------------------------------------------

def free_name(folder: Path, stem: str, suffix: str = "") -> Path:
    """``stem{suffix}.mp4`` in *folder*, or with ``-2``, ``-3``… if that is taken."""
    first = folder / f"{stem}{suffix}.{OUTPUT_EXT}"
    if not first.exists():
        return first
    for n in range(2, 10000):
        candidate = folder / f"{stem}{suffix}-{n}.{OUTPUT_EXT}"
        if not candidate.exists():
            return candidate
    raise CompressError(said("No free name for the compressed copy."))


def temporary_for(folder: Path, stem: str, asset_id: int | str) -> Path:
    """A hidden, non-media name in the same folder (so the last step is a
    rename on one disk, and no scan takes a half-written file for a video).

    Named after the video (its index id), not the job: a job's id was random,
    so a copy half written when Ninaivu restarted had a name the next job for
    the same video never looked for, and stayed, hidden, the size of most of
    a film. The same name every time means the next start removes it.
    """
    return folder / f".{stem[:80]}.a{int(asset_id)}.compress.tmp"


#: Ninaivu's own half-written copies, by name: the per-video name above, and
#: the per-job one earlier builds left (twelve hex digits of a job id).
_LEFTOVER = re.compile(r"^\..+\.(?:a\d+|[0-9a-f]{12})\.compress\.tmp$")
#: A file written to in the last quarter-hour is left alone, whatever its
#: name: an encode in progress writes continuously, so this is never one.
LEFTOVER_QUIET_FOR = 15 * 60
#: At most this many folders are looked in, so a start-up on a library of
#: a hundred thousand videos in as many folders is not held up by it.
LEFTOVER_FOLDERS = 5000


def clear_leftovers(conn: Any, now: float | None = None) -> int:
    """Remove half-written copies a restart left. Returns how many went.

    Only in folders that hold a video in the index, only files named the way
    :func:`temporary_for` names them (and named after a video in that
    folder), only when no compression is queued or running in this process,
    and only files nobody has written to for a quarter of an hour. Anything
    else with ``.tmp`` in its name, a folder outside the library, or a copy
    still being written is never touched.
    """
    with _lock:
        if any(job["state"] in ("queued", "running", "checking") for job in _jobs.values()):
            return 0
    now = time.time() if now is None else now
    folders: dict[Path, set[str]] = {}
    for row in conn.execute(
            "SELECT root, rel_path FROM assets WHERE kind='video' "
            "AND COALESCE(trashed, 0)=0"):
        path = Path(row["root"]) / row["rel_path"]
        stems = folders.get(path.parent)
        if stems is None:
            if len(folders) >= LEFTOVER_FOLDERS:
                continue
            stems = folders[path.parent] = set()
        stems.add(path.stem[:80])
    removed = 0
    for folder, stems in folders.items():
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue                    # a drive that is not plugged in
        for entry in entries:
            name = entry.name
            if not _LEFTOVER.match(name):
                continue
            if not any(name.startswith(f".{stem}.") for stem in stems):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if now - entry.stat(follow_symlinks=False).st_mtime < LEFTOVER_QUIET_FOR:
                    continue
                os.unlink(entry.path)
            except OSError:
                continue
            removed += 1
            log.info("removed a half-written compressed copy left by a restart: %s",
                     entry.path)
    return removed


def publish(temporary: Path, target: Path) -> None:
    """Move the finished copy to *target*, never over a file already there."""
    try:
        os.link(temporary, target)            # atomic, and refuses an existing name
    except FileExistsError:
        raise
    except OSError as exc:
        # exFAT and FAT drives have no hard links.
        if target.exists():
            raise FileExistsError(str(target)) from exc
        os.rename(temporary, target)
        return
    temporary.unlink(missing_ok=True)


# --- the queue ---------------------------------------------------------------------

def _sweep(now: float) -> None:
    finished = [(job["touched"], job_id) for job_id, job in _jobs.items()
                if job["state"] in ("done", "error", "cancelled")]
    for touched, job_id in finished:
        if now - touched > KEEP_FOR:
            _jobs.pop(job_id, None)
    finished = sorted((job["touched"], job_id) for job_id, job in _jobs.items()
                      if job["state"] in ("done", "error", "cancelled"))
    for _, job_id in finished[:max(0, len(finished) - MAX_REMEMBERED)]:
        _jobs.pop(job_id, None)


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {key: job[key] for key in (
        "id", "asset_id", "mode", "state", "progress", "error", "result", "name", "ahead",
        "damaged")}


def start(asset_id: int, mode: str, name: str,
          work: Callable[[dict[str, Any], Callable[[float], None], Callable[[], bool]], dict]
          ) -> dict[str, Any]:
    """Queue *work* for one video. CompressError if that video already has one."""
    if mode not in MODES:
        raise CompressError(said("Choose to compress a copy or replace the original."))
    now = time.time()
    with _lock:
        _sweep(now)
        for job in _jobs.values():
            if job["asset_id"] == asset_id and job["state"] in ("queued", "running", "checking"):
                raise CompressError(said("This video is already being compressed."))
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "asset_id": int(asset_id), "mode": mode, "name": name,
               "state": "queued", "progress": 0.0, "error": "", "result": None,
               "ahead": 0, "damaged": False, "cancel": False, "work": work, "touched": now}
        _jobs[job_id] = job
        _queue.put(job_id)
        _ensure_worker()
        _number_the_queue()
        return _public(job)


def _number_the_queue() -> None:
    waiting = sorted((j for j in _jobs.values() if j["state"] == "queued"),
                     key=lambda j: j["touched"])
    for ahead, job in enumerate(waiting):
        job["ahead"] = ahead + sum(1 for j in _jobs.values()
                                   if j["state"] in ("running", "checking"))


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run_forever, name="video-compress", daemon=True)
        _worker.start()


def _run_forever() -> None:
    while True:
        job_id = _queue.get()
        try:
            _run(job_id)
        except Exception:                         # noqa: BLE001 — the worker must live on
            log.exception("video compression %s failed", job_id)


def _run(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job["state"] != "queued":
            return
        if job["cancel"]:
            job.update(state="cancelled", touched=time.time())
            return
        job.update(state="running", ahead=0, touched=time.time())
        _number_the_queue()

    def report(progress: float) -> None:
        with _lock:
            job["progress"] = round(progress, 3)
            if progress >= 1.0:
                job["state"] = "checking"
            job["touched"] = time.time()

    def cancelled() -> bool:
        return bool(job["cancel"])

    try:
        result = job["work"](job, report, cancelled)
        outcome = {"state": "done", "progress": 1.0, "result": result}
    except CompressError as exc:
        outcome = {"state": "cancelled" if job["cancel"] else "error", "error": str(exc),
                   "damaged": isinstance(exc, DamagedVideo)}
    except Exception as exc:                      # noqa: BLE001
        log.exception("video compression of %s failed", job.get("name"))
        outcome = {"state": "error", "error": f"Could not compress this video: {exc}"}
    with _lock:
        job.update(outcome, touched=time.time())
        job["work"] = None


def status(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def active() -> list[dict[str, Any]]:
    """Every job still known, newest last, for a console that was reloaded."""
    with _lock:
        _sweep(time.time())
        return [_public(j) for j in sorted(_jobs.values(), key=lambda j: j["touched"])]


def cancel(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        if job["state"] in ("queued", "running", "checking"):
            job["cancel"] = True
            if job["state"] == "queued":
                job.update(state="cancelled", error=said("Stopped."), touched=time.time())
                _number_the_queue()
        return _public(job)


def wait(job_id: str, timeout: float = 120.0) -> dict[str, Any] | None:
    """For tests: block until the job has finished."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = status(job_id)
        if state is None or state["state"] in ("done", "error", "cancelled"):
            return state
        time.sleep(0.05)
    return status(job_id)


def remove_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def same_disk_space(folder: Path, needed: int) -> bool:
    """Whether *folder*'s disk has room for about *needed* more bytes."""
    try:
        return shutil.disk_usage(folder).free > needed
    except OSError:
        return True
