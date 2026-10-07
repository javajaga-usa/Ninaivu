"""Move media to its calendar-date folder without changing asset identities."""
from datetime import date, datetime
import logging
import os
from pathlib import Path
import shutil
import threading

from ..archive.dates import to_timestamp
from ..storage import db

log = logging.getLogger(__name__)

edit_lock = threading.Lock()


def parse_date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Use a creation date in YYYY-MM-DD format.")
    # Local midnight, as every other capture time in the index is local wall
    # time (archive/dates.py). Midnight UTC was the evening before anywhere
    # west of Greenwich, so the photo sorted and grouped with the wrong day.
    return datetime.combine(date.fromisoformat(value), datetime.min.time())


def copy_exclusive(source, target):
    """Copy *source* to a *target* that must not exist yet, synced to disk."""
    with target.open("xb") as output:
        try:
            with source.open("rb") as original:
                shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            output.close()
            target.unlink()
            raise
    try:
        shutil.copystat(source, target)
    except OSError:
        target.unlink()
        raise


def publish_exclusive(staged, target):
    """Give a file already in its folder its real name, never replacing one.

    A hard link is atomic and refuses an existing name. exFAT and FAT32 — what
    most external drives ship formatted as — have no hard links, so there the
    file is renamed after checking the name is free; on Windows the rename
    itself also refuses an existing name.
    """
    try:
        os.link(staged, target)
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    os.rename(staged, target)


def _move(source, target):
    """Exclusive destination creation; never overwrite another household file."""
    try:
        os.link(source, target)
    except FileExistsError:
        raise
    except OSError:
        # No hard links — exFAT and FAT32, what most external drives come
        # formatted as. On the same drive a rename does the move without
        # copying a byte; the copy is only for another drive. A date change
        # runs holding the index's write lock, and copying a four-gigabyte
        # video there held up every other change for minutes.
        if _same_drive(source, target.parent):
            if target.exists() or target.is_symlink():
                raise FileExistsError(target) from None
            os.rename(source, target)
            return
        copy_exclusive(source, target)
    try:
        source.unlink()
    except OSError:
        target.unlink()
        raise


def _same_drive(path, folder):
    try:
        return os.stat(path).st_dev == os.stat(folder).st_dev
    except OSError:
        return False


def _identity(path):
    """What makes two paths the same file: its device and inode.

    Some filesystems report no inode number (0); then the case-folded path is
    the best available answer.
    """
    info = os.stat(path)
    if info.st_ino:
        return info.st_dev, info.st_ino
    return "path", os.path.normcase(os.path.abspath(path)).casefold()


def relocate(conn, asset_id, when, roots, archive_path=None):
    moved = []
    with db._write_lock:
        attached = archive_path is not None and Path(archive_path).is_file()
        if attached:
            conn.execute("ATTACH DATABASE ? AS date_edit_archive", (str(archive_path),))
        try:
            conn.execute("BEGIN IMMEDIATE")
            asset = db.get_asset(conn, asset_id)
            if not asset or asset["root"] not in roots:
                raise ValueError("File is not in an active library.")
            if asset.get("trashed"):
                raise ValueError("Restore this file from the bin before changing its date.")
            root = Path(asset["root"]).resolve()
            source = root / asset["rel_path"]
            folder = when.date().isoformat().replace("-", "/")
            destination = root / folder
            if not source.resolve().is_relative_to(root) or source.is_symlink():
                raise ValueError("File must be inside its library.")
            if not destination.resolve().is_relative_to(root):
                raise ValueError("Destination must be inside its library.")
            if not source.is_file():
                raise ValueError("The original file is unavailable.")
            sources = [source]
            # Keep Live Photo pairs and common sidecars together.
            if asset.get("live_video_path"):
                sources.append(root / asset["live_video_path"])
            # Both spellings of a sidecar are looked for, because on a
            # case-sensitive disk they are different files. On a
            # case-insensitive one — every Mac volume and exFAT drive by
            # default — ``IMG.xmp`` and ``IMG.XMP`` are the same file, both
            # pass is_file(), and listing it twice made the second move find
            # the first one's target already there, refusing every date change.
            # So files are compared by what they are on disk, not by name.
            seen = {_identity(path) for path in sources if path.is_file()}
            for suffix in (".xmp", ".XMP", ".aae", ".AAE", ".json"):
                for sidecar in (source.with_suffix(suffix), Path(str(source) + suffix)):
                    if not sidecar.is_file():
                        continue
                    identity = _identity(sidecar)
                    if identity not in seen:
                        seen.add(identity)
                        sources.append(sidecar)
            plan = []
            for original in sources:
                if not original.resolve().is_relative_to(root) or original.is_symlink() or not original.is_file():
                    raise ValueError("A companion file is unavailable or outside the library.")
                target = destination / original.name
                if original == target:
                    continue
                if target.exists() or target.is_symlink() or conn.execute(
                    "SELECT 1 FROM assets WHERE root=? AND rel_path=?",
                    (asset["root"], target.relative_to(root).as_posix()),
                ).fetchone():
                    raise FileExistsError("A file with this name already exists at the new date. Nothing was moved.")
                plan.append((original, target))
            destination.mkdir(parents=True, exist_ok=True)
            for original, target in plan:
                _move(original, target)
                moved.append((original, target))
                old_relative = original.relative_to(root).as_posix()
                new_relative = target.relative_to(root).as_posix()
                conn.execute("UPDATE assets SET rel_path=?, folder=? WHERE root=? AND rel_path=?",
                             (new_relative, folder, asset["root"], old_relative))
                conn.execute("UPDATE assets SET live_video_path=? WHERE root=? AND live_video_path=?",
                             (new_relative, asset["root"], old_relative))
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='cloud_uploads'").fetchone():
                    conn.execute("UPDATE cloud_uploads SET rel_path=? WHERE root=? AND rel_path=?",
                                 (new_relative, asset["root"], old_relative))
                if attached:
                    # Archive verification/deduplication must follow the file;
                    # its content hash and source history have not changed.
                    conn.execute("UPDATE date_edit_archive.files SET destination_path=? "
                                 "WHERE destination_path=?", (str(target), str(original)))
                    conn.execute("UPDATE date_edit_archive.files SET duplicate_of=? "
                                 "WHERE duplicate_of=?", (str(target), str(original)))
            # IDs, favourites, album membership, thumbnails and all other
            # metadata remain intact. Both members of a Live Photo share a date.
            for original in sources:
                relative = (destination / original.name).relative_to(root).as_posix()
                conn.execute("UPDATE assets SET captured_at=?, date_key=?, date_source='manual' "
                             "WHERE root=? AND rel_path=?",
                             (to_timestamp(when), when.date().isoformat(), asset["root"], relative))
            conn.commit()
        except BaseException:
            conn.rollback()
            # Every file that moved is put back, even if one of them cannot
            # be: stopping at the first failure would strand the rest at the
            # new date too, and would report that failure instead of the one
            # that actually stopped the date change.
            for original, target in reversed(moved):
                try:
                    _move(target, original)
                except Exception:                  # noqa: BLE001
                    log.exception("date change: could not move %s back to %s",
                                  target, original)
            raise
        finally:
            if attached:
                conn.execute("DETACH DATABASE date_edit_archive")
    return db.get_asset(conn, asset_id)
