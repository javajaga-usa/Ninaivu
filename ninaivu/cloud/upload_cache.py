"""Where encrypted uploads wait until Drive has them.

The sync engine is written so that it cannot delete a local file — the words
are not in it, and a test checks. Encrypted upload needs one thing deleted:
the ciphertext it made, once the upload is done. So that lives here, and can
delete nothing else: only a ``.ninaivu`` file directly inside the cache folder,
named by this module.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Iterable

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

    def prune(self, keep: Iterable[str], older_than: float = 3600.0) -> int:
        """Remove ciphertext no upload will carry on with. Returns how many.

        An upload abandoned part-way — the file deleted, hidden, or set aside
        after too many failures — left its ciphertext here for good. *keep*
        names the files a saved resumable session still needs. Only files
        older than *older_than* seconds go, and only this folder's own: cache
        files, and the half-written ``.ninaivu-crypto-*.tmp`` an encryption
        stopped mid-way leaves.
        """
        if not self.folder.is_dir():
            return 0
        kept, cutoff, removed = set(keep), time.time() - older_than, 0
        for path in list(self.folder.iterdir()):
            try:
                if not path.is_file() or path.is_symlink() or path.stat().st_mtime > cutoff:
                    continue
                if path.name.startswith(".ninaivu-crypto-") and path.suffix == ".tmp":
                    path.unlink(missing_ok=True)
                elif path.suffix == SUFFIX and len(path.stem) == 64 and path.name not in kept:
                    self.discard(path)
                else:
                    continue
            except OSError:
                continue
            removed += 1
        return removed
