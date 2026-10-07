"""Browser-viewable copies of photographs a browser cannot open.

An iPhone writes HEIC and a camera writes raw, and no browser decodes either.
Ninaivu has always made thumbnails of them — which is why they appear in the
grid at all — but opening one showed the 640-pixel thumbnail with an apology
underneath, and the Photo Studio refused them outright. For a library shot on
phones that is a lot of photographs you can see and not actually look at.

This is the still-image twin of :mod:`ninaivu.proxies`, and deliberately much
simpler than it. A video proxy needs ffmpeg, minutes of work and a progress
bar; a HEIC is decoded and re-encoded by Pillow in well under a second, so
there is no build state, no polling and no 202 — the first request converts it
and every request after that is a file read.

Two things it is careful about:

**The copy is a viewing copy, not a second original.** It is capped at a size
that fills any screen someone will look at this on, and it is written to the
state directory rather than beside the photograph. Nothing appears in the
library folder; nothing is indexed; deleting the whole cache costs one
reconversion.

**The cache is bounded.** Left alone, a folder of full-size JPEGs beside a
50,000-photograph library is a second library. It evicts by least-recently-
used, the same rule and the same reasoning as the video proxies.
"""

from __future__ import annotations

import logging
import threading
import weakref
from pathlib import Path

from PIL import Image

from ..server.config import BROWSER_NATIVE
from ..utils.source_version import matches_source, source_version, stamp_source

log = logging.getLogger(__name__)

__all__ = ["needs_rendition", "rendition_path", "StillStore",
           "RENDITION_VERSION", "MAX_EDGE", "DEFAULT_CACHE_MB"]

#: Bumped when the encode changes in a way that makes older copies wrong.
RENDITION_VERSION = 1

#: Longest edge of a viewing copy.
#:
#: 2560 covers a 5K display at the size a photograph is actually shown, and
#: keeps a typical frame around half a megabyte. Going to the full 48-megapixel
#: original would make the cache larger than the library it serves and would
#: not change what anybody sees.
MAX_EDGE = 2560

#: JPEG quality. 88 is above the point where more bytes stop being visible on
#: a photograph and below the point where the file doubles.
QUALITY = 88

#: Default ceiling for the whole folder.
DEFAULT_CACHE_MB = 2048

#: Held weakly, as phone_backup's are: a plain dict kept a lock for every
#: photograph anybody ever opened, for as long as Ninaivu ran.
_locks: weakref.WeakValueDictionary[int, threading.Lock] = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()


def needs_rendition(ext: str, kind: str = "picture") -> bool:
    """Whether a browser needs this converted before it can show it."""
    if kind != "picture":
        return False
    return f".{str(ext).lower().lstrip('.')}" not in BROWSER_NATIVE


def renditions_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "renditions"


def rendition_path(state_dir: Path | str, asset_id: int, variant: str = "") -> Path:
    """Where the viewable copy is kept. *variant* names a copy made some other
    way than the plain one (``-t90``: with the index's turn baked in)."""
    return renditions_dir(state_dir) / f"{int(asset_id)}-v{RENDITION_VERSION}{variant}.jpg"


def _lock_for(asset_id: int) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(int(asset_id))
        if lock is None:
            lock = threading.Lock()
            _locks[int(asset_id)] = lock
        return lock


def _remove(path: Path) -> None:
    """A viewing copy and the note beside it of which version it was made from.

    Removing only the picture left the note behind, one per photograph ever
    opened, and clearing the cache emptied nothing but the pictures.
    """
    path.unlink()
    Path(f"{path}.source").unlink(missing_ok=True)


class StillStore:
    """The folder of viewable copies, and the conversions filling it."""

    def __init__(self, state_dir: Path | str, cache_mb: int = DEFAULT_CACHE_MB) -> None:
        self.state_dir = Path(state_dir)
        self.cache_mb = int(cache_mb)

    def path_for(self, asset_id: int, variant: str = "") -> Path:
        return rendition_path(self.state_dir, asset_id, variant)

    def ready(self, asset_id: int, source: Path | None = None, *,
              variant: str = "") -> Path | None:
        path = self.path_for(asset_id, variant)
        if source is not None and not matches_source(path, source):
            return None
        # One stat rather than exists() and then stat(): eviction for another
        # photograph can remove this copy in between, and that raised.
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > 0:
            # Looking at something keeps it alive: eviction is by last use, and
            # on most systems reading a file does not update its mtime.
            try:
                path.touch()
            except OSError:
                pass
            return path
        return None

    def build(self, asset_id: int, source: Path, *, orient,
              variant: str = "") -> Path | None:
        """Convert one photograph, or return the copy somebody else just made.

        ``orient`` opens the file the right way up — passed in rather than
        imported so this module never has to know how Ninaivu decides which way
        up a photograph goes.
        """
        with _lock_for(asset_id):
            existing = self.ready(asset_id, source, variant=variant)
            if existing is not None:
                return existing
            target = self.path_for(asset_id, variant)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".part")
            try:
                version = source_version(source)
                with orient(source) as image:
                    frame = image.convert("RGB")
                    frame.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                    frame.save(temporary, "JPEG", quality=QUALITY,
                               optimize=True, progressive=True)
                if source_version(source) != version:
                    raise OSError("source changed during conversion")
                temporary.replace(target)
                stamp_source(target, version)
            except Exception as exc:                    # noqa: BLE001
                temporary.unlink(missing_ok=True)
                log.warning("stills: could not convert %s — %s", source, exc)
                return None
        self.evict()
        return target

    def evict(self) -> int:
        """Keep the folder under its ceiling, oldest-looked-at first."""
        directory = renditions_dir(self.state_dir)
        if not directory.is_dir():
            return 0
        # A file another request removed between the listing and the look at
        # it is not counted, rather than failing this request with a 500.
        files: list[tuple[float, int, Path]] = []
        try:
            for path in directory.glob("*.jpg"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, path))
        except OSError:
            return 0
        files.sort(key=lambda item: item[0])
        budget = self.cache_mb * 1024 * 1024
        total = sum(size for _, size, _ in files)
        removed = 0
        for _, size, path in files:
            if total <= budget:
                break
            try:
                _remove(path)
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def clear(self) -> int:
        directory = renditions_dir(self.state_dir)
        removed = 0
        for path in directory.glob("*.jpg") if directory.is_dir() else []:
            try:
                _remove(path)
                removed += 1
            except OSError:
                continue
        return removed
