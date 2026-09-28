"""Private upload staging and administrator-controlled publication."""
import json
import os
from pathlib import Path
import secrets
import time

from ..storage import db, new_files
from . import date_edit, scanner


def stage(conn, cfg, upload, filename, root, scope, user_id, *,
          overrides=None, place=None, mtime=None):
    """Hold a file for an administrator to review before it joins the library.

    ``overrides`` replaces fields in the record built from the staged file, and
    ``place`` names the folder inside the library the file belongs in, relative
    to its root. Both exist for a derivative -- an edit saved back from the
    Playground -- which already knows where it goes and what it may be seen by,
    because the photograph it came from settled that. An ordinary upload knows
    neither and is filed by date on approval.

    ``mtime`` is the file's own modification time where the sender knows it —
    a phone backup does — so a photograph with no date inside it is dated
    from when it was taken rather than when it arrived.
    """
    storage_key = secrets.token_hex(20)
    base = Path(cfg.state_dir) / "pending-uploads"
    if base.is_symlink() or not base.resolve().is_relative_to(Path(cfg.state_dir).resolve()):
        raise ValueError("Upload storage must stay inside Ninaivu's state directory.")
    folder = base / storage_key
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / filename
    try:
        if hasattr(upload, "link_into"):
            # A file already on this disk (a finished phone backup) is linked
            # into place rather than copied: a four-gigabyte video need not be
            # written twice.
            upload.link_into(target)
        else:
            with target.open("xb") as output:
                upload.save(output)
        if mtime:
            os.utime(target, (mtime, mtime))
        record = scanner.build_record(folder, filename, target.stat(), cfg)
        if record["kind"] == "unknown":
            raise ValueError("This file is not recognised as supported media.")
        if overrides:
            record.update(overrides)
        if place is not None:
            # Underscored keys are staging instructions for approve(); they are
            # not asset columns and db._normalise drops them on the way in.
            record["_place"] = place
        with db._write_lock:
            cur = conn.execute(
                "INSERT INTO pending_uploads(storage_key, filename, root, scope, uploaded_by, uploaded_at, record) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (storage_key, filename, root, scope or "", user_id, time.time(), json.dumps(record)),
            )
            conn.commit()
        return {"id": cur.lastrowid, "filename": filename, "status": "pending",
                "creation_date": record["date_key"]}
    except BaseException:
        target.unlink(missing_ok=True)
        folder.rmdir()
        raise


def get(conn, upload_id):
    row = conn.execute("SELECT * FROM pending_uploads WHERE id=?", (upload_id,)).fetchone()
    return dict(row) if row else None


def source_path(cfg, upload):
    base = (Path(cfg.state_dir) / "pending-uploads").resolve()
    path = base / upload["storage_key"] / upload["filename"]
    if not path.resolve().is_relative_to(base) or path.is_symlink():
        raise ValueError("Upload storage is unavailable.")
    return path


def _same_device(path, folder):
    """Whether a link from *path* into *folder* can work (same filesystem)."""
    return path.stat().st_dev == folder.stat().st_dev


def _library_for(cfg, upload) -> Path:
    """The library folder an approved upload is written into: its own, or the
    new-files folder when its own cannot be written (see new_files.py)."""
    return Path(new_files.destination(cfg, upload["root"])).resolve()


def _pre_copy(conn, cfg, upload_id, creation_date):
    """Copy a staged file onto the library's drive before any lock is taken.

    When Ninaivu's state directory and the library are on different drives the
    move is a full copy, and doing that inside the database write lock stalled
    every other writer — and, through ``BEGIN IMMEDIATE``, every other
    connection — for as long as a multi-gigabyte video took to copy. The copy
    goes to a hidden temporary name in the destination folder first; approval
    then only has to link it into place. ``None`` when no copy is needed or the
    upload cannot be approved anyway (approve() says why).
    """
    upload = get(conn, upload_id)
    if not upload or upload["status"] != "pending":
        return None
    try:
        record = json.loads(upload["record"])
        root = _library_for(cfg, upload)
        place = record.get("_place")
        key = creation_date or record.get("date_key") or ""
        folder = place if place is not None else "/".join(
            filter(None, [upload["scope"], key.replace("-", "/")]))
        dest_dir = root / folder
        if not dest_dir.resolve().is_relative_to(root):
            return None
        source = source_path(cfg, upload)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if _same_device(source, dest_dir):
            return None
        staged = dest_dir / f".ninaivu-approve-{secrets.token_hex(8)}.part"
        date_edit.copy_exclusive(source, staged)
        return staged
    except (OSError, ValueError):
        return None


def approve(conn, cfg, upload_id, reviewer, creation_date=None):
    staged = _pre_copy(conn, cfg, upload_id, creation_date)
    try:
        return _approve(conn, cfg, upload_id, reviewer, creation_date, staged)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _approve(conn, cfg, upload_id, reviewer, creation_date, staged):
    moved = None
    with db._write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            upload = get(conn, upload_id)
            if not upload:
                raise ValueError("Upload not found.")
            if upload["status"] == "approved":
                conn.rollback()
                return db.get_asset(conn, upload["asset_id"])
            if upload["status"] != "pending":
                raise ValueError("This upload has already been reviewed.")
            if upload["root"] not in (cfg.libraries or [cfg.active_root]):
                raise ValueError("The upload's library is no longer configured.")
            if not Path(upload["root"]).is_dir():
                raise ValueError("The upload's library is unavailable.")
            # Its own library folder, or the new-files folder when that one
            # is read-only — an NTFS drive on a Mac.
            root = _library_for(cfg, upload)
            home = str(root)
            record = json.loads(upload["record"])
            # Staging instructions rather than asset columns; see stage().
            place = record.pop("_place", None)
            pinned = record.pop("_pin_visibility", False)
            key = creation_date or record.get("date_key")
            # A derivative keeps its source's date, and a library photograph is
            # allowed to have no date at all -- so only parse one when it is
            # actually going to be used, or an undated source could not be
            # edited and approved at all.
            # A derivative keeps the exact moment its source was taken, down to
            # the time of day. Re-confirming the same date in the review queue
            # must not quietly flatten that to midnight; only genuinely changing
            # the date re-dates the copy.
            dated = place is None or (bool(creation_date)
                                      and creation_date != record.get("date_key"))
            when = date_edit.parse_date(key) if dated else None
            # A derivative goes back beside the photograph it came from. Only an
            # ordinary upload, which has no place of its own yet, is filed by date.
            folder = place if place is not None else "/".join(
                filter(None, [upload["scope"], key.replace("-", "/")]))
            dest_dir = root / folder
            if not dest_dir.resolve().is_relative_to(root):
                raise ValueError("The destination must be inside the upload's library.")
            source = source_path(cfg, upload)
            target = dest_dir / upload["filename"]
            # Keep repeated filenames without replacing an earlier upload.
            while target.exists() or target.is_symlink() or conn.execute(
                "SELECT 1 FROM assets WHERE root=? AND rel_path=?",
                (home, target.relative_to(root).as_posix()),
            ).fetchone():
                target = dest_dir / f"{Path(upload['filename']).stem}_{secrets.token_hex(6)}{Path(upload['filename']).suffix}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            if staged is not None and staged.parent == dest_dir and staged.exists():
                # Already on this drive, so publishing is a rename rather than
                # a copy. The staged original is removed only once the row is
                # committed.
                date_edit.publish_exclusive(staged, target)
                moved = None, target
            else:
                date_edit._move(source, target)
                moved = source, target
            record.update(root=home, rel_path=target.relative_to(root).as_posix(),
                          folder=folder, filename=target.name)
            if dated:
                record.update(captured_at=when.timestamp(), date_key=key, date_source="manual")
            # The rules of the library it was meant for: that is the folder the
            # household set them on, wherever the file had to be written.
            rule = db.visibility_for_folder(db.folder_rules(conn, upload["root"]), folder)
            # A derivative already carries the visibility its source had, which
            # is narrower than the folder rule whenever the source was hidden by
            # hand or for explicit content. The folder must not widen it back.
            if rule is not None and not pinned:
                record.update(visibility=max(int(rule), 2 if record["kind"] in db.ADMIN_ONLY_KINDS else 0),
                              vis_source="folder")
            payload = db._normalise(record)
            columns = ', '.join(db._ASSET_FIELDS)
            values = ', '.join(f':{f}' for f in db._ASSET_FIELDS)
            cur = conn.execute(f"INSERT INTO assets ({columns}) VALUES ({values})", payload)
            asset_id = cur.lastrowid
            conn.execute("UPDATE pending_uploads SET status='approved', reviewed_by=?, reviewed_at=?, asset_id=? WHERE id=?",
                         (reviewer, time.time(), asset_id, upload_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            if moved:
                if moved[0] is None:
                    moved[1].unlink(missing_ok=True)
                else:
                    date_edit._move(moved[1], moved[0])
            raise
    if moved and moved[0] is None:
        # Published and committed; the staged original is no longer needed.
        try:
            source.unlink()
        except OSError:
            pass
    if moved:
        # Renamed or moved, the file is out of its staging folder now, and the
        # folder was being left behind, empty, one per upload.
        try:
            source.parent.rmdir()
        except OSError:
            pass
    return db.get_asset(conn, asset_id)


def reject(conn, cfg, upload_id, reviewer):
    """Refuse an upload: it never joins the library, and the file is deleted.

    The staged file goes, with the folder it was staged in and the preview
    thumbnails made for the queue. The record stays, marked rejected with who
    decided and when, so the audit trail says what happened to it. An upload
    already approved, or already refused, is left as it is.
    """
    from . import media                                    # noqa: PLC0415

    upload = get(conn, upload_id)
    if not upload:
        raise LookupError("There is no such upload.")
    if upload["status"] != "pending":
        raise ValueError(f"This upload was already {upload['status']}.")
    with db._write_lock:
        changed = conn.execute(
            "UPDATE pending_uploads SET status='rejected', reviewed_by=?, reviewed_at=? "
            "WHERE id=? AND status='pending'", (reviewer, time.time(), upload_id)).rowcount
        conn.commit()
    if not changed:
        raise ValueError("This upload was reviewed a moment ago.")
    path = source_path(cfg, upload)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass
    thumb = json.loads(upload["record"] or "{}").get("thumb")
    if thumb:
        media.remove_thumbnails(cfg.thumbs_dir, thumb, cfg.thumb_sizes, cfg.thumb_format)
    return {"id": upload_id, "filename": upload["filename"], "status": "rejected"}
