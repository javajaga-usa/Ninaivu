"""Reading files the same way everywhere.

Five parts of Ninaivu hashed a file to say whether a copy matched -- the
second copy, the repair, the index backup, the restore test and the backup
tool -- and each had its own loop for it. One here, so a change to how it
reads (or a bug in it) is made once.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["CHUNK", "sha256_file"]

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
