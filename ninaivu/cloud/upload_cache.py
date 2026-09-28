"""Where encrypted uploads wait until Drive has them.

The sync engine is written so that it cannot delete a local file — the words
are not in it, and a test checks. Encrypted upload needs one thing deleted:
the ciphertext it made, once the upload is done. So that lives here, and can
delete nothing else: only a ``.ninaivu`` file directly inside the cache folder,
named by this module.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

SUFFIX = ".ninaivu"


class UploadCache:
    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)

    def path_for(self, root: str, rel_path: str) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(f"{root}|{rel_path}".encode("utf-8")).hexdigest()
        return self.folder / f"{name}{SUFFIX}"

    def discard(self, path: Path) -> None:
        """Remove a finished or abandoned ciphertext — and refuse anything else."""
        path = Path(path)
        if path.parent.resolve() != self.folder.resolve() or path.suffix != SUFFIX \
                or len(path.stem) != 64:
            raise ValueError(f"not an upload cache file: {path}")
        path.unlink(missing_ok=True)
