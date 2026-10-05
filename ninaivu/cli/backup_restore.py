#!/usr/bin/env python3
"""Ninaivu Production Hot Backup & Restore Utility.

Performs live, crash-safe SQLite online backups using SQLite's native backup API,
packaging index databases, archive logs, configuration, avatars, and certificates
into verified backup bundles.

Usage:
    # Create a hot backup
    python tools/backup_restore.py backup --out /mnt/backups/ninaivu

    # List backups in a directory
    python tools/backup_restore.py list /mnt/backups/ninaivu

    # Restore from a backup bundle
    python tools/backup_restore.py restore /mnt/backups/ninaivu/ninaivu_backup_20260830_120000.tar.gz
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import uuid
from datetime import datetime
from pathlib import Path


from ..storage import backup as backup_lib
from ..server import runfile
from ..server.config import Config
from ..utils.files import sha256_file


def use_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def say(msg: str) -> None:
    print(f"  {paint('•', '36')} {msg}")


def ok(msg: str) -> None:
    print(f"  {paint('✓', '32')} {msg}")


def warn(msg: str) -> None:
    print(f"  {paint('!', '33')} {msg}")


def fail(msg: str) -> None:
    print(f"  {paint('✗', '31')} {msg}", file=sys.stderr)


def hot_backup_sqlite(src_path: Path, dst_path: Path) -> bool:
    """Kept as a name the rest of this script uses; the work lives in
    :mod:`ninaivu.backup` so the application and this tool cannot drift."""
    return backup_lib.hot_copy(src_path, dst_path)


def create_backup(out_dir: Path) -> Path | None:
    """Create a complete, verified snapshot of Ninaivu's state directory.

    This is the same bundle the application writes on its schedule — same
    databases, same manifest, same checksums — because it is the same code.
    """
    cfg = Config.load()
    state_dir = cfg.state_dir
    if not state_dir.is_dir():
        fail(f"State directory not found at {state_dir}")
        return None

    say(f"Starting hot backup from: {state_dir}")
    say(f"Target folder:            {out_dir}")

    archive_path = backup_lib.snapshot(state_dir, out_dir)
    if archive_path is None:
        fail("Backup failed — see the log for what went wrong")
        return None

    size_mb = archive_path.stat().st_size / (1024 * 1024)
    ok(f"Backup created successfully: {archive_path.name} ({size_mb:.2f} MB)")
    say(f"SHA-256: {sha256_file(archive_path)}")
    return archive_path


#: The checks a restore makes before it replaces anything. They live in
#: ninaivu.storage.backup so Ninaivu can run the same ones on its own backups on a
#: schedule; one implementation means "verified" there means "restorable" here.
_valid_state_path = backup_lib.valid_state_path
_safe_members = backup_lib.safe_members
_verify_state = backup_lib.verify_state


def _require_stopped(state_dir: Path) -> None:
    running = runfile.read(state_dir)
    if running:
        try:
            pid = int(running["pid"])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Invalid run file; verify Ninaivu has stopped before restoring") from None
        if runfile.is_running(pid):
            raise ValueError("Stop Ninaivu before restoring its state")


def _bundle_kind(extracted: Path) -> str:
    """The ``kind`` a bundle's manifest gives, or '' for a full local backup."""
    try:
        manifest = json.loads((extracted / "backup_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(manifest.get("kind") or "") if isinstance(manifest, dict) else ""


def _prepare_replacement(existing: Path, extracted: Path, candidate: Path) -> None:
    """Preserve local caches/credentials, but replace the backed-up state fully."""
    from ..cloud import index_copy                           # noqa: PLC0415

    partial = _bundle_kind(extracted) == index_copy.KIND
    replaced = set(backup_lib.DATABASES + backup_lib.EXTRAS)
    if partial:
        # The copy of the index kept in Drive leaves out, on purpose, the
        # sign-in, the keys, the certificates and the uploads still waiting
        # (ninaivu/cloud/index_copy.py). Its not carrying them says nothing
        # about whether this machine should keep its own, and replacing
        # "everything a backup holds" took them all away — the older TLS key
        # and certificate files included. Only what it does carry is replaced.
        replaced.difference_update(name for name in backup_lib.EXTRAS
                                   if not (extracted / name).exists())
    else:
        # A bundle made before the certificate authority and cloud key were
        # collected must not take away the ones this machine still has.
        replaced.difference_update(name for name in backup_lib.REPLACED_ONLY_IF_PRESENT
                                   if not (extracted / name).exists())
    replaced.add(runfile.RUN_FILE)
    replaced.update(f"{name}{suffix}" for name in backup_lib.DATABASES
                    for suffix in ("-wal", "-shm", "-journal"))
    replaced = {name.casefold() for name in replaced}
    candidate.mkdir()
    if existing.exists():
        for child in existing.iterdir():
            if child.name.casefold() in replaced:
                continue
            target = candidate / child.name
            if child.is_dir() and not child.is_symlink():
                shutil.copytree(child, target, symlinks=True)
            else:
                shutil.copy2(child, target, follow_symlinks=False)
    shutil.copytree(extracted, candidate, dirs_exist_ok=True,
                    ignore=lambda directory, names: (
                        ["backup_manifest.json"] if Path(directory) == extracted else []))
    if partial:
        _keep_local_secrets(existing / "config.json", extracted / "config.json",
                            candidate / "config.json")


def _keep_local_secrets(local: Path, copied: Path, target: Path) -> None:
    """Settings from the index copy, with this machine's secrets kept.

    The copy's settings have every password, token and webhook blanked; put
    over this machine's they emptied them. See index_copy.keep_local_secrets.
    """
    from ..cloud import index_copy                           # noqa: PLC0415

    try:
        mine = json.loads(local.read_text(encoding="utf-8"))
        theirs = json.loads(copied.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return                      # nothing here to keep, or nothing carried
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return
    target.write_text(json.dumps(index_copy.keep_local_secrets(theirs, mine), indent=2),
                      encoding="utf-8")


def restore_backup(archive_path: Path) -> bool:
    """Prepare verified state, then swap directories with rollback on failure."""
    if not archive_path.is_file():
        fail(f"Backup archive not found: {archive_path}")
        return False

    cfg = Config.load()
    state_dir = cfg.state_dir.expanduser().absolute()
    try:
        if state_dir.is_symlink() or (
                getattr(state_dir, "is_junction", lambda: False)()):
            raise ValueError("State directory must be a regular, non-root directory")
        state_dir = state_dir.resolve()
        if state_dir == state_dir.parent:
            raise ValueError("Cannot restore over a filesystem root")
        _require_stopped(state_dir)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return False

    say(f"Preparing to restore from: {archive_path}")
    say(f"Target state directory:     {state_dir}")

    with tempfile.TemporaryDirectory(prefix="ninaivu_restore_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)

        # 1. Extract archive
        say("Extracting and verifying archive...")
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                members, rejected = _safe_members(tar, tmp_dir)
                if rejected:
                    fail(f"Backup archive contains {len(rejected)} unsafe or "
                         f"unexpected entries (outside the restore directory, "
                         f"or not a plain file) -- refusing to extract it.")
                    return False
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(tmp_dir, members=members, filter="data")
                else:
                    tar.extractall(tmp_dir, members=members)
        except Exception as exc:
            fail(f"Failed to extract tar archive: {exc}")
            return False

        extracted_state = tmp_dir / "state"
        try:
            _verify_state(extracted_state)
            ok("All archived files and databases verified.")
            state_dir.parent.mkdir(parents=True, exist_ok=True)
            # Staging is on the same filesystem as the destination. A failed
            # copy leaves the original untouched; publication uses renames.
            with tempfile.TemporaryDirectory(
                    prefix=".ninaivu_restore_", dir=state_dir.parent) as staging:
                candidate = Path(staging) / "state"
                _prepare_replacement(state_dir, extracted_state, candidate)
                _require_stopped(state_dir)
                pre_restore = None
                if state_dir.exists():
                    pre_restore = state_dir.with_name(
                        f".ninaivu_pre_restore_{state_dir.name}_{uuid.uuid4().hex}")
                    say(f"Original state will remain at: {pre_restore}")
                    sys.stdout.flush()
                    state_dir.rename(pre_restore)
                try:
                    candidate.rename(state_dir)
                except BaseException:
                    if pre_restore is not None:
                        try:
                            pre_restore.rename(state_dir)
                        except OSError as rollback_error:
                            fail(f"Automatic rollback failed: {rollback_error}. "
                                 f"Original state is preserved at {pre_restore}; "
                                 "restore that directory before restarting Ninaivu.")
                    raise
                if pre_restore is not None:
                    ok(f"Original state preserved at {pre_restore}")
        except (OSError, ValueError, sqlite3.Error) as exc:
            fail(f"Restore failed: {exc}")
            return False

    ok("Restore completed successfully! Please restart Ninaivu to load restored state.")
    return True


def list_backups(backup_dir: Path) -> None:
    """List all available backup archives in directory."""
    if not backup_dir.is_dir():
        warn(f"Directory not found: {backup_dir}")
        return

    archives = sorted(backup_dir.glob("ninaivu_backup_*.tar.gz"), reverse=True)
    if not archives:
        say(f"No backup archives found in {backup_dir}")
        return

    print(f"\n{paint('=== Ninaivu Backup Archives ===', '1;36')}")
    print(f"Directory: {backup_dir}\n")
    print(f"  {'Filename':<40} {'Size':<12} {'Modified'}")
    print(f"  {'-'*40} {'-'*12} {'-'*20}")

    for arch in archives:
        size_mb = arch.stat().st_size / (1024 * 1024)
        mtime = datetime.fromtimestamp(arch.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {arch.name:<40} {size_mb:>8.2f} MB   {mtime}")
    print()


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description="Ninaivu Hot Backup & Disaster Recovery")
    sub = parser.add_subparsers(dest="command", help="Command to execute")

    b_parser = sub.add_parser("backup", help="Create a live hot backup")
    b_parser.add_argument("--out", type=Path, default=Path.home() / "ninaivu-backups",
                          help="Output directory for backup archives (default: ~/ninaivu-backups)")

    r_parser = sub.add_parser("restore", help="Restore state from backup archive")
    r_parser.add_argument("archive", type=Path, help="Path to .tar.gz backup archive")

    l_parser = sub.add_parser("list", help="List backups in directory")
    l_parser.add_argument("dir", type=Path, nargs="?", default=Path.home() / "ninaivu-backups",
                          help="Directory containing backup archives")

    args = parser.parse_args(argv)

    if args.command == "backup":
        res = create_backup(args.out)
        return 0 if res else 1
    elif args.command == "restore":
        res = restore_backup(args.archive)
        return 0 if res else 1
    elif args.command == "list":
        list_backups(args.dir)
        return 0
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
