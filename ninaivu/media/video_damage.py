"""Telling a video that is damaged at its source from one that only failed.

A video whose file was cut short where it came from (a camera that lost
power, a copy that stopped) has no index: MP4 and MOV keep the list of where
each frame is in one block, usually written last, and without it nothing can
open the file. ffprobe says "moov atom not found". Nothing here can rebuild
that block from the file alone, so the largest-files list marks such a video,
and Keep or Delete is the answer.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from . import media

log = logging.getLogger(__name__)

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


# -- what the removed Compress & Replace may have left ---------------------------
#
# Until 4.2 a video could be compressed where it was. The copy was written
# beside it under a hidden name and renamed when it had been checked; a
# restart in the middle left that hidden file, most of a film's size, where it
# was. Nothing writes one any more, so they are looked for once and removed.

#: The name such a copy had: hidden, a job id of twelve hex digits, and an
#: ending no camera or program writes.
_COMPRESS_TEMPORARY = re.compile(r"^\..*\.[0-9a-f]{12}\.compress\.tmp$")
#: Written to the index's ``meta`` once every folder has been looked in.
LEFTOVERS_CLEARED = "compress_leftovers_cleared"
#: At most this many folders, so start-up is not held up by a huge library.
LEFTOVER_FOLDERS = 5000


def clear_compress_leftovers(conn) -> int:
    """Remove the half-written compressed copies an earlier version left
    beside the library's videos; returns how many went. Only files named as
    those copies were, only in folders holding a video in the index, and only
    until every such folder could be read once (a drive that is not plugged
    in is tried again at the next start)."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (LEFTOVERS_CLEARED,)).fetchone()
    if row is not None:
        return 0
    folders: dict[str, None] = {}
    for found in conn.execute(
            "SELECT root, rel_path FROM assets WHERE kind='video' AND COALESCE(trashed, 0)=0"):
        folders.setdefault(os.path.dirname(os.path.join(found["root"], found["rel_path"])))
        if len(folders) >= LEFTOVER_FOLDERS:
            break
    removed, every = 0, True
    for folder in folders:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            every = False
            continue
        for entry in entries:
            if not _COMPRESS_TEMPORARY.match(entry.name):
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                every = False
    if removed:
        log.info("Removed %d unfinished compressed copies an earlier version left", removed)
    if every:
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (LEFTOVERS_CLEARED, str(removed)))
        conn.commit()
    return removed
