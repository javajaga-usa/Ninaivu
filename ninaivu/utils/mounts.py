"""Reading Linux's mount table.

``/proc/mounts`` separates its fields with spaces, so the kernel writes a
space, tab, newline or backslash inside a field as three octal digits after a
backslash: ``\\040``, ``\\011``, ``\\012``, ``\\134``. Only the space was
turned back, so a drive or folder whose name had a backslash or a tab in it
never matched the path it was mounted at.
"""

from __future__ import annotations

import re

__all__ = ["unescape"]

_ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape(field: str) -> str:
    """One field of ``/proc/mounts`` (a device or a mount point) as the path
    it names."""
    return _ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)
