"""Videos for people Ninaivu has never met, without where they were shot.

A phone writes the place a video was taken into the file, as it does for a
photograph. A guest and a share-link visitor are given a photograph's viewing
copy, never its EXIF; this is the same promise for a video: the streams are
copied as they are (no re-encode, so it is quick and nothing is lost) into a
new container with every metadata atom left behind. From Ninaivu Lite.

Without ffmpeg, or for a file ffmpeg cannot remux, the answer is None and the
caller sends the original: better a video that plays than one that does not.
The copies are kept in the state folder, one per video and version, and the
oldest unused ones go once the folder is over its budget.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from . import media

log = logging.getLogger(__name__)

#: How much room the stripped copies may take before the least recently used go.
DEFAULT_CACHE_MB = 4096
#: A remux reads and writes the whole file; past this it is given up.
TIMEOUT_SECONDS = 600

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(asset_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(int(asset_id), threading.Lock())


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
        if target.is_file() and target.stat().st_size > 0:
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
        command = [media.FFMPEG, "-v", "error", "-nostdin", "-y", "-i", str(source),
                   "-map_metadata", "-1", "-map_metadata:s", "-1",
                   "-map_chapters", "-1", "-c", "copy"]
        if target.suffix in (".mp4", ".m4v", ".mov"):
            command += ["-movflags", "+faststart"]
        command.append(str(temporary))
        try:
            proc = subprocess.run(command, capture_output=True, timeout=TIMEOUT_SECONDS,
                                  check=False)
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
    _evict(cache_dir(state_dir), cache_mb, keep=target)
    return target


def _evict(directory: Path, cache_mb: int, keep: Path | None = None) -> None:
    """Remove the least recently used copies until the folder fits its budget."""
    try:
        files = sorted((p for p in directory.iterdir()
                        if p.is_file() and not p.name.startswith(".")),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    total = sum(p.stat().st_size for p in files)
    budget = int(cache_mb) * 1024 * 1024
    for path in files:
        if total <= budget:
            break
        if path == keep:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            continue
