"""Reading files the same way everywhere.

Five parts of Ninaivu hashed a file to say whether a copy matched -- the
second copy, the repair, the index backup, the restore test and the backup
tool -- and each had its own loop for it. One here, so a change to how it
reads (or a bug in it) is made once.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = ["CHUNK", "same_bytes", "sha256_file", "stays_inside"]

#: Bytes read at a time when a whole file is hashed or copied. Large enough
#: that a photograph is one read; small enough that a film does not sit in
#: memory.
CHUNK = 4 * 1024 * 1024


def sha256_file(path: Path | str, chunk: int = CHUNK) -> str:
    """The SHA-256 of the file's bytes, as hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def same_bytes(a: Path | str, b: Path | str, chunk: int = CHUNK) -> bool:
    """Whether two files hold exactly the same bytes. False if either cannot
    be read."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as one, open(b, "rb") as other:
            while True:
                left, right = one.read(chunk), other.read(chunk)
                if left != right:
                    return False
                if not left:
                    return True
    except OSError:
        return False


def stays_inside(base: Path | str, path: Path | str) -> bool:
    """Whether *path* is inside *base* once links are followed.

    ``abspath`` only tidies the spelling, so ``base/linked/photo.jpg`` passed
    even when ``linked`` was a link to somewhere else entirely, and whatever
    was written there landed outside the folder that was chosen. ``realpath``
    follows every link that already exists along the way; the parts that do
    not exist yet are kept as written, and those are made as real folders.
    """
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(base))
