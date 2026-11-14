"""Telling a video that is damaged at its source from one that only failed.

A video whose file was cut short where it came from (a camera that lost
power, a copy that stopped) has no index: MP4 and MOV keep the list of where
each frame is in one block, usually written last, and without it nothing can
open the file. ffprobe says "moov atom not found". Nothing here can rebuild
that block from the file alone, so the largest-files list marks such a video,
and Keep or Delete is the answer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import media

#: What ffmpeg and ffprobe say about a file whose bytes cannot be read as a
#: video at all, as opposed to a disk or a permission problem.
_DAMAGE_SIGNS = ("moov atom not found", "invalid data found when processing input")

#: (path, size, mtime) → damaged, so a list read again does not probe again.
_damage_seen: dict[tuple[str, int, float], bool] = {}

#: The most answers the cache keeps; past it, it starts again, so a server up
#: for months does not keep one for every video it ever saw (audit C-51).
SEEN_MOST = 20000


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
    if len(_damage_seen) >= SEEN_MOST:
        _damage_seen.clear()
    _damage_seen[key] = answer
    return answer
