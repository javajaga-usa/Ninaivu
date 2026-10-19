"""FriBiDi for Pillow, so Tamil is spelt right in the portable builds.

Pillow lays out text with raqm, which its wheels carry, and raqm needs
FriBiDi, which they do not: Pillow looks for it when ``PIL._imagingft`` is
first imported, by a bare file name, and turns raqm off when it is not
found. Linux has FriBiDi as part of its desktop, so there it is found. The
Mac and Windows have none, and without raqm Tamil was laid out letter by
letter: a vowel sign written before its consonant landed after it, so books
left the Tamil out rather than print it misspelt (``media/books.py``).

The portable builds now carry a copy beside their own Python
(``installers/windows/build-portable.ps1``, ``installers/macos/build.sh``):
``libfribidi-0.dll`` next to ``python.exe``, where Windows looks first for a
library named that way, and ``lib/libfribidi.dylib`` in the Mac app's Python.
macOS looks for a bare name in the current folder too, so :func:`make_ready`
loads the copy and imports ``PIL._imagingft`` from that folder, once, before
anything else has. Anywhere else it does nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["bundled", "make_ready"]


def bundled(platform: str | None = None, prefix: str | None = None) -> Path | None:
    """The copy of FriBiDi this Python carries, or None when it carries none."""
    platform = platform or sys.platform
    base = Path(prefix or sys.base_prefix)
    if platform == "win32":
        path = base / "libfribidi-0.dll"
    elif platform == "darwin":
        path = base / "lib" / "libfribidi.dylib"
    else:
        return None
    return path if path.is_file() else None


def make_ready() -> bool:
    """Load the bundled FriBiDi before Pillow looks for it. True when it did."""
    if "PIL._imagingft" in sys.modules:
        return False
    path = bundled()
    if path is None:
        return False
    import ctypes                                         # noqa: PLC0415
    try:
        if sys.platform == "win32":
            ctypes.WinDLL(str(path))
            return True
        ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
        here = os.getcwd()
        os.chdir(path.parent)
        try:
            import PIL._imagingft                         # noqa: F401, PLC0415
        finally:
            os.chdir(here)
        return True
    except Exception:                                     # noqa: BLE001
        # Without it Pillow is as it was: books leave the Tamil out.
        return False
