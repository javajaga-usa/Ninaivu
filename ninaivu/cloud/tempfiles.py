"""Opening a temporary or staged file without following a link put in its place.

A restore stages each download under a name anybody can predict
(``.photo.jpg.ninaivu-part``, ``<drive id>.part``), in a folder the household
can write to. Opened the ordinary way, a link planted at that name sends the
write wherever it points — out of the destination, over a file that is not
Ninaivu's — and the restore reports success. These open the name itself:
whatever is there is removed first (removing a link leaves what it points at
alone), and the file is made new, exclusively, never through a link.

Standard library only: the stand-alone restore tools load this without the
rest of Ninaivu.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

#: Not on Windows; there, a link needs privileges to make and O_EXCL still
#: refuses to open one that exists.
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
BINARY = getattr(os, "O_BINARY", 0)


def create_new(path: Path | str, mode: int = 0o600) -> BinaryIO:
    """*path*, opened for writing as a file made just now.

    Whatever had the name — an old staged file, or a link — is removed
    first. O_CREAT|O_EXCL (with O_NOFOLLOW where there is one) then refuses
    anything put there in the moment between.
    """
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | BINARY
    return os.fdopen(os.open(path, flags, mode), "wb")


def is_plain_file(path: Path | str) -> bool:
    """A regular file at *path* itself — a link to one is not."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def open_to_append(path: Path | str) -> BinaryIO:
    """A staged file being carried on with: refused when it is a link."""
    if not is_plain_file(path):
        raise OSError(f"refusing to carry on with {Path(path).name}: it is not a plain file")
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | NOFOLLOW | BINARY), "ab")
