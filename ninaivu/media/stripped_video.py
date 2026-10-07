"""Videos for people Ninaivu has never met, without where they were shot.

A phone writes the place a video was taken into the file, as it does for a
photograph. A guest and a share-link visitor are given a photograph's viewing
copy, never its EXIF; this is the same promise for a video: the streams are
copied as they are (no re-encode, so it is quick and nothing is lost) into a
new container with every metadata atom left behind. From Ninaivu Lite.

Without ffmpeg, or for a file ffmpeg cannot remux, the answer is None and the
caller refuses the video. It used to send the original instead, on the grounds
that a video that plays beats one that does not; but the original is exactly
what carries the place it was filmed to someone who was not to have it.
The copies are kept in the state folder, one per video and version, and the
oldest unused ones go once the folder is over its budget.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import weakref
from pathlib import Path

from . import media

log = logging.getLogger(__name__)

#: How much room the stripped copies may take before the least recently used go.
DEFAULT_CACHE_MB = 4096
#: A remux reads and writes the whole file; past this it is given up.
TIMEOUT_SECONDS = 600

#: Held weakly: a plain dict kept one for every video ever played to a guest.
_locks: weakref.WeakValueDictionary[int, threading.Lock] = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()
#: Copies made at once, across every video. Each reads and writes a whole
#: file; several family members opening videos together on a Raspberry Pi in
#: the middle of an import each started their own, and fought the scan and
#: each other for the one disk.
MAX_AT_ONCE = 2
_making = threading.BoundedSemaphore(MAX_AT_ONCE)
_evicting = threading.Lock()


def _lock_for(asset_id: int) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(int(asset_id))
        if lock is None:
            lock = threading.Lock()
            _locks[int(asset_id)] = lock
        return lock


def cache_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "stripped-videos"


def _target(state_dir: Path | str, asset_id: int, source: Path) -> Path:
    stat = source.stat()
    ext = source.suffix.lower() or ".mp4"
    return cache_dir(state_dir) / f"{int(asset_id)}-{stat.st_size}-{int(stat.st_mtime)}{ext}"


def stripped_copy(state_dir: Path | str, asset_id: int, source: Path, *,
                  cache_mb: int = DEFAULT_CACHE_MB) -> Path | None:
    """The metadata-free copy of *source*, made if need be; None without one."""
    if not media.FFMPEG:
        return None
    try:
        target = _target(state_dir, asset_id, source)
    except OSError:
        return None
    # One remux per video at a time: the player's range requests arrive
    # together, and each starting its own would read the file several times.
    with _lock_for(asset_id):
        # One stat, not is_file() and then stat(): another video's eviction
        # can remove this copy in between, and that raised mid-stream.
        try:
            ready = target.stat().st_size > 0
        except OSError:
            ready = False
        if ready:
            try:
                target.touch()
            except OSError:
                pass
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        for old in target.parent.glob(f"{int(asset_id)}-*"):
            try:
                old.unlink()
            except OSError:
                pass
        temporary = target.with_name(f".{target.stem}.part{target.suffix}")
        # ffmpeg's own choice of streams (the video and the sound) rather
        # than every stream: a camera's data tracks (a GoPro's GPS among
        # them) stay behind, and a container that cannot hold them does not
        # fail the copy.
        command = [media.FFMPEG, "-v", "error", "-nostdin", "-y", *media.LOCAL_ONLY,
                   "-i", str(source),
                   "-map_metadata", "-1", "-map_metadata:s", "-1",
                   "-map_chapters", "-1", "-c", "copy"]
        if target.suffix in (".mp4", ".m4v", ".mov"):
            command += ["-movflags", "+faststart"]
        command.append(str(temporary))
        try:
            with _making:
                proc = subprocess.run(command, capture_output=True,
                                      timeout=TIMEOUT_SECONDS, check=False)
            if proc.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 0:
                os.replace(temporary, target)
            else:
                log.info("stripped video: ffmpeg could not remux %s: %s", source,
                         proc.stderr.decode("utf-8", "replace")[-300:])
                target = None
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("stripped video: %s: %s", source, exc)
            target = None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        # Only after a copy was made: a copy already there adds nothing to
        # the folder, and every range request of every video playing walked
        # it, stat by stat.
        _evict(cache_dir(state_dir), cache_mb, keep=target)
    return target


def _evict(directory: Path, cache_mb: int, keep: Path | None = None) -> None:
    """Remove the least recently used copies until the folder fits its budget.

    One at a time, and a file that goes between being listed and being looked
    at is simply not counted: two requests evicting together raised
    FileNotFoundError for a video whose copy was there.
    """
    with _evicting:
        files: list[tuple[float, int, Path]] = []
        try:
            for path in directory.iterdir():
                if path.name.startswith("."):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, path))
        except OSError:
            return
        files.sort(key=lambda item: item[0])
        total = sum(size for _, size, _ in files)
        budget = int(cache_mb) * 1024 * 1024
        for _, size, path in files:
            if total <= budget:
                break
            if path == keep:
                continue
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
