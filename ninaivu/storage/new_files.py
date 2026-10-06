"""Where a new file goes when its library folder cannot be written.

A library on an NTFS drive is read-only on a Mac: macOS reads the drive but
does not write to it. Everything Ninaivu adds to a library then failed — an
edited copy, an approved upload, a phone backup — with a message about disk
space and permissions that was true of neither.

So a new file goes to the library folder it belongs in when that folder can be
written, and otherwise to ``new_files_folder`` (``~/Pictures/Ninaivu`` unless
set), in the same folder layout: an edit of ``2019/12/20/a.jpg`` goes to
``~/Pictures/Ninaivu/2019/12/20/``. That folder joins the library folders the
first time it is used, so the gallery shows what is saved there, the scanner
and the cloud backup include it, and nothing is lost when the drive becomes
writable — the files can be moved in then.

Only *new* files are redirected. Deleting, changing a date and other changes
to a file that is already in a read-only library cannot be made anywhere else,
and say so (:func:`read_only_reason`).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Two saves at once must not both add the folder to the library.
_registering = threading.Lock()


def writable(root: str | Path) -> bool:
    """Whether new files can be written into *root*. A read-only mount answers
    no here even when the folder's permission bits look writable."""
    try:
        return os.path.isdir(root) and os.access(root, os.W_OK)
    except OSError:
        return False


def destination(cfg: Any, root: str) -> str:
    """The library folder a new file for *root* is written into.

    *root* itself when it can be written; otherwise the new-files folder,
    created and added to the library folders (and saved) the first time.

    Raises OSError when *root* is not there at all (:func:`roots.root_present`):
    an unplugged drive under a ``nofail`` mount leaves an empty, writable
    folder, and what was saved into it was hidden by the drive coming back,
    then dropped from the index by the next scan.
    """
    from . import roots as roots_kit                    # noqa: PLC0415

    if not roots_kit.root_present(root):
        raise OSError(f"The library folder {root} is not there. Is its disk plugged in?")
    if writable(root):
        return root
    folder = Path(getattr(cfg, "new_files_folder", "") or "~/Pictures/Ninaivu").expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    resolved = str(folder.resolve())
    if not writable(resolved):
        raise OSError(f"Neither {root} nor {resolved} can be written to.")
    with _registering:
        if resolved not in cfg.roots and resolved != root:
            cfg.add_library(resolved)
            try:
                cfg.save()
            except OSError as exc:                        # still usable this run
                log.warning("could not save %s as a library folder: %s", resolved, exc)
            log.info("%s is read-only; new files go to %s", root, resolved)
    return resolved


#: Said beside each file a change could not be made to, in a list of them.
READ_ONLY = "its library folder is read-only on this computer"


def read_only_reason(root: str | Path) -> str | None:
    """What to say when a change to a file already in *root* cannot be made."""
    if writable(root) or not os.path.isdir(root):
        return None
    return (f"{root} is read-only on this computer, so files already in it cannot "
            "be changed, moved or deleted here. A drive formatted for Windows "
            "(NTFS) is read-only on a Mac; new copies and uploads are saved to "
            "another library folder instead.")
