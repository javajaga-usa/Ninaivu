"""Will it fit? What a copy onto another disk could not keep, said before it starts.

A library kept on a Mac's APFS disk or a Linux ext4 disk holds names that a
Windows-formatted drive will not take: ``Diwali: day 2``, ``what?.jpg``, a
folder ending in a dot. Two files called ``IMG_1.JPG`` and ``img_1.jpg`` are
two files on ext4 and one on APFS, exFAT or NTFS. A name typed on a Mac
(Unicode spelled one way) and the same name from a phone (spelled the other)
are one name on a Mac. A 6 GB video does not go onto a FAT32 pendrive at
all. Each copier in Ninaivu (the mirror, drive export, the archive, the
hand-over) copes with some of these when it meets them, file by file, part
way through a copy. The household finds out afterwards, from a log.

This says it first. Given where a copy is going, it works out the kind of
disk (:func:`filesystem_of`) and checks every file the copy would write
against what that kind of disk can keep (:data:`FAMILIES`), and lists what
it could not: the file, the problem, and a name that would work. It reads
the index only; nothing on either disk is opened or changed.

The rules per kind of disk are what each file system documents, not what a
particular driver forgives: a Mac writing ``:`` to an exFAT drive gets an
error, and Samba on Linux quietly stores it under a stand-in character that
comes back as something else after a restore.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..utils.filenames import portable_name

try:                                   # psutil is in requirements.txt; still optional here
    import psutil
except ImportError:                    # pragma: no cover
    psutil = None

FAT32_LIMIT = 4 * 1024 ** 3 - 1

_FORBIDDEN = set('<>:"\\|?*')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)


@dataclass(frozen=True)
class Family:
    """What one kind of file system can keep."""
    name: str
    windows_names: bool          # refuses <>:"\|?*, trailing dots and spaces, device names
    case_insensitive: bool       # IMG_1.JPG and img_1.jpg are one file
    unicode_insensitive: bool    # the two Unicode spellings of a name are one file
    name_limit_utf16: bool       # 255 UTF-16 units per name (else 255 UTF-8 bytes)
    max_file: int | None         # largest file it can hold
    coarse_times: bool           # modified times kept to 2 seconds only


FAMILIES: dict[str, Family] = {
    "fat": Family("fat", True, True, True, True, FAT32_LIMIT, True),
    "exfat": Family("exfat", True, True, True, True, None, False),
    "ntfs": Family("ntfs", True, True, False, True, None, False),
    "smb": Family("smb", True, True, True, True, None, False),
    "apfs": Family("apfs", False, True, True, False, None, False),
    "hfs": Family("hfs", False, True, True, True, None, False),
    "unix": Family("unix", False, False, False, False, None, False),
    # A disk whose kind is not known is checked against the strictest rules
    # that do not refuse large files, so nothing is promised that may fail.
    "unknown": Family("unknown", True, True, True, True, None, False),
}

#: What each file system type, as the system names it, belongs to.
FSTYPES = {
    "vfat": "fat", "msdos": "fat", "fat": "fat", "fat32": "fat", "fat16": "fat",
    "exfat": "exfat",
    "ntfs": "ntfs", "ntfs3": "ntfs", "fuseblk": "ntfs",
    "smbfs": "smb", "cifs": "smb", "smb3": "smb", "smb2": "smb",
    "apfs": "apfs",
    "hfs": "hfs", "hfsplus": "hfs",
    "ext2": "unix", "ext3": "unix", "ext4": "unix", "btrfs": "unix", "xfs": "unix",
    "zfs": "unix", "f2fs": "unix", "nfs": "unix", "nfs4": "unix", "tmpfs": "unix",
    "overlay": "unix",
}

#: The problems, in the order a report lists them.
PROBLEMS = ("too_large", "forbidden_character", "trailing_dot_or_space", "reserved_name",
            "name_too_long", "same_name_different_case", "same_name_different_spelling")


def family_of(fstype: str | None) -> Family:
    return FAMILIES[FSTYPES.get((fstype or "").lower(), "unknown")]


def filesystem_of(path: str | Path) -> str | None:
    """The file system type of the disk *path* is on (or would be on, for a
    folder not made yet), as the system names it: ``apfs``, ``exfat``,
    ``ext4``. None when it cannot be told.

    Worked out from the path's spelling against the mounted disks, the longest
    mount point it falls under, without opening or listing anything at the
    path: it is a folder an administrator typed, and nothing there is read."""
    if psutil is None:
        return None
    target = os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))
    try:
        partitions = psutil.disk_partitions(all=True)
    except Exception:                                   # noqa: BLE001 - asked, not needed
        return None
    best, length = None, -1
    for part in partitions:
        mount = os.path.normcase(os.path.abspath(part.mountpoint))
        inside = target == mount or target.startswith(mount.rstrip("\\/") + os.sep)
        if inside and len(mount) > length:
            best, length = part, len(mount)
    if best is None:
        return None
    return (best.fstype or "").lower() or None


def _parts(rel_path: str) -> list[str]:
    return [p for p in re.split(r"[/\\]", rel_path) if p]


def _name_problems(part: str, family: Family) -> list[str]:
    found = []
    if family.windows_names:
        if any(ch in _FORBIDDEN or ord(ch) < 32 for ch in part):
            found.append("forbidden_character")
        if part.endswith((".", " ")):
            found.append("trailing_dot_or_space")
        if _RESERVED.match(part):
            found.append("reserved_name")
    length = (len(part.encode("utf-16-le")) // 2 if family.name_limit_utf16
              else len(part.encode("utf-8")))
    if length > 255:
        found.append("name_too_long")
    return found


def _suggest(rel_path: str, family: Family) -> str:
    """The same path with each name that cannot be kept made one that can."""
    out = []
    for part in _parts(rel_path):
        out.append(portable_name(part) if _name_problems(part, family) else part)
    return "/".join(out)


def fitting_path(rel_path: str, family: Family) -> str:
    """*rel_path* as a copy onto a disk of *family* can write it: unchanged
    (with ``/`` between its folders) when every name in it can be kept, and
    otherwise with each name that cannot made one that can (``Diwali: day 2``
    becomes ``Diwali day 2``). Clashes with other files are the copier's to
    settle; this looks at one path alone."""
    return _suggest(rel_path, family)


def too_large(size: int, family: Family) -> bool:
    """Whether a file of *size* bytes cannot go onto a disk of *family* at all."""
    return family.max_file is not None and int(size or 0) > family.max_file


def check(entries: Iterable[tuple[str, int]], fstype: str | None, *,
          limit: int = 1000) -> dict[str, Any]:
    """What a copy of *entries* (``(relative path, size)``) onto a disk of
    *fstype* could not keep. ``limit`` caps the files listed; the counts are
    always whole."""
    family = family_of(fstype)
    counts = {p: 0 for p in PROBLEMS}
    listed: list[dict[str, Any]] = []
    folded: dict[str, list[str]] = {}
    spelled: dict[str, list[str]] = {}
    files = total = 0

    def note(rel: str, problem: str, **extra: Any) -> None:
        counts[problem] += 1
        if len(listed) < limit:
            listed.append({"path": rel, "problem": problem, **extra})

    for rel, size in entries:
        rel = str(rel)
        files += 1
        total += int(size or 0)
        if family.max_file is not None and int(size or 0) > family.max_file:
            note(rel, "too_large", size=int(size))
        bad = sorted({p for part in _parts(rel) for p in _name_problems(part, family)},
                     key=PROBLEMS.index)
        for problem in bad:
            note(rel, problem, suggestion=_suggest(rel, family))
        if family.case_insensitive or family.unicode_insensitive:
            nfc = unicodedata.normalize("NFC", rel)
            if family.case_insensitive:
                folded.setdefault(nfc.casefold(), []).append(rel)
            if family.unicode_insensitive:
                spelled.setdefault(nfc, []).append(rel)

    for group in folded.values():
        if len(group) > 1:
            # Two spellings that differ only in Unicode form are the next
            # check's; a case clash is two paths that still differ in NFC.
            if len({unicodedata.normalize("NFC", g) for g in group}) > 1:
                for rel in group:
                    note(rel, "same_name_different_case",
                         clashes_with=[g for g in group if g != rel])
    for group in spelled.values():
        if len(group) > 1:
            for rel in group:
                note(rel, "same_name_different_spelling",
                     clashes_with=[g for g in group if g != rel])

    return {
        "filesystem": fstype or None,
        "family": family.name,
        "files": files,
        "bytes": total,
        "problems": {p: n for p, n in counts.items() if n},
        "problem_files": len({item["path"] for item in listed}),
        "listed": listed,
        "complete": sum(counts.values()) <= limit,
        "coarse_times": family.coarse_times,
        "fits": not any(counts.values()),
    }


def check_library(conn: sqlite3.Connection, roots: Iterable[str], target: str | Path, *,
                  fstype: str | None = None, limit: int = 1000) -> dict[str, Any]:
    """:func:`check` for every file of the library in *roots* (the bin left
    out), as a copy into *target* would write them."""
    roots = [str(r) for r in roots]
    if fstype is None:
        fstype = filesystem_of(target)
    entries: list[tuple[str, int]] = []
    for root in roots:
        # Each library folder is copied into a folder of its own name when
        # there are several, so their files cannot clash with each other.
        prefix = f"{Path(root).name}/" if len(roots) > 1 else ""
        entries += [(prefix + row[0], row[1]) for row in conn.execute(
            "SELECT rel_path, size FROM assets WHERE trashed=0 AND root=? ORDER BY rel_path",
            (root,))]
    report = check(entries, fstype, limit=limit)
    report["target"] = str(target)
    return report
