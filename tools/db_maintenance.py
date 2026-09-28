#!/usr/bin/env python3
"""Ninaivu SQLite Database & Storage Maintenance Utility.

Performs production database health checks, WAL flushing, page defragmentation,
FTS index rebuilding, and orphan thumbnail pruning.

Usage:
    python tools/db_maintenance.py --check
    python tools/db_maintenance.py --stats
    python tools/db_maintenance.py --vacuum
    python tools/db_maintenance.py --prune-thumbs
    python tools/db_maintenance.py --all
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ninaivu.server.config import Config


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


def format_bytes(bytes_val: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"


def check_integrity(db_path: Path) -> bool:
    """Run PRAGMA integrity_check, quick_check, and foreign_key_check."""
    say(f"Checking integrity for {db_path.name}...")
    if not db_path.is_file():
        warn(f"Database not found at {db_path}")
        return True

    healthy = True
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row

        # Quick check
        qc = conn.execute("PRAGMA quick_check").fetchall()
        if qc and qc[0][0] != "ok":
            fail(f"Quick check failed on {db_path.name}: {qc}")
            healthy = False

        # Full integrity check
        ic = conn.execute("PRAGMA integrity_check").fetchall()
        if ic and ic[0][0] != "ok":
            fail(f"Integrity check failed on {db_path.name}: {ic}")
            healthy = False

        # Foreign key check
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            warn(f"Foreign key violations found in {db_path.name}: {len(fk)} violations")
            healthy = False

        conn.close()
        if healthy:
            ok(f"{db_path.name}: 100% healthy, no corruption detected.")
    except Exception as exc:
        fail(f"Integrity check error on {db_path.name}: {exc}")
        return False
    return healthy


def checkpoint_wal(db_path: Path) -> None:
    """Flush and truncate WAL journal logs to main database file."""
    say(f"Flushing WAL journal for {db_path.name}...")
    if not db_path.is_file():
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        res = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.close()
        ok(f"Checkpoint completed on {db_path.name} (busy={res[0]}, log={res[1]}, checkpointed={res[2]})")
    except Exception as exc:
        fail(f"WAL checkpoint error on {db_path.name}: {exc}")


def vacuum_db(db_path: Path) -> None:
    """Reclaim unallocated pages and compact database."""
    say(f"Vacuuming {db_path.name} (compacting storage)...")
    if not db_path.is_file():
        return
    before_size = db_path.stat().st_size
    try:
        conn = sqlite3.connect(str(db_path), timeout=60.0)
        conn.execute("VACUUM")
        conn.close()
        after_size = db_path.stat().st_size
        saved = before_size - after_size
        ok(f"{db_path.name} compacted: {format_bytes(before_size)} → {format_bytes(after_size)} (reclaimed {format_bytes(max(0, saved))})")
    except Exception as exc:
        fail(f"Vacuum error on {db_path.name}: {exc}")


def reindex_db(db_path: Path) -> None:
    """Rebuild database indexes."""
    say(f"Rebuilding indexes for {db_path.name}...")
    if not db_path.is_file():
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=60.0)
        conn.execute("REINDEX")
        conn.close()
        ok(f"Reindexed {db_path.name} successfully.")
    except Exception as exc:
        fail(f"Reindex error on {db_path.name}: {exc}")


def prune_orphan_thumbnails(cfg: Config) -> tuple[int, int]:
    """Find and delete thumbnail files on disk that have no asset in index.db."""
    say("Auditing thumbnail cache for orphaned files...")
    thumbs_dir = cfg.thumbs_dir
    if not thumbs_dir.is_dir() or not cfg.db_path.is_file():
        ok("No thumbnail directory or database to audit.")
        return 0, 0

    conn = sqlite3.connect(str(cfg.db_path), timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM assets")
    asset_ids = {str(row[0]) for row in cursor.fetchall()}
    conn.close()

    removed_count = 0
    reclaimed_bytes = 0

    for root, _, files in os.walk(thumbs_dir):
        for f in files:
            # Filenames look like "123_256.webp" or "123_640.webp"
            asset_id = f.split("_")[0]
            if asset_id.isdigit() and asset_id not in asset_ids:
                file_path = Path(root) / f
                try:
                    size = file_path.stat().st_size
                    file_path.unlink()
                    removed_count += 1
                    reclaimed_bytes += size
                except OSError:
                    pass

    if removed_count > 0:
        ok(f"Pruned {removed_count} orphan thumbnails, reclaimed {format_bytes(reclaimed_bytes)}.")
    else:
        ok("Thumbnail cache is clean. No orphan thumbnails found.")
    return removed_count, reclaimed_bytes


def print_stats(cfg: Config) -> None:
    """Print comprehensive database, index, and cache statistics."""
    print(f"\n{paint('=== Ninaivu Database & Cache Telemetry ===', '1;36')}")
    print(f"State Directory: {cfg.state_dir}")

    db_path = cfg.db_path
    if not db_path.is_file():
        warn("Main database (index.db) not found.")
        return

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row

    # Assets breakdown
    total_assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    pictures = conn.execute("SELECT COUNT(*) FROM assets WHERE kind = 'picture'").fetchone()[0]
    videos = conn.execute("SELECT COUNT(*) FROM assets WHERE kind = 'video'").fetchone()[0]
    audio = conn.execute("SELECT COUNT(*) FROM assets WHERE kind = 'audio'").fetchone()[0]
    total_media_size = conn.execute("SELECT COALESCE(SUM(size), 0) FROM assets").fetchone()[0]
    hidden_assets = conn.execute("SELECT COUNT(*) FROM assets WHERE visibility = 2").fetchone()[0]

    # Users
    users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    sessions_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    active_roots = conn.execute("SELECT COUNT(DISTINCT root) FROM assets").fetchone()[0]

    conn.close()

    db_size = db_path.stat().st_size
    wal_size = (cfg.state_dir / "index.db-wal").stat().st_size if (cfg.state_dir / "index.db-wal").exists() else 0
    archive_db = cfg.state_dir / "archive.db"
    archive_db_size = archive_db.stat().st_size if archive_db.is_file() else 0

    # Thumbnail dir size
    thumb_count = 0
    thumb_bytes = 0
    if cfg.thumbs_dir.is_dir():
        for root, _, files in os.walk(cfg.thumbs_dir):
            thumb_count += len(files)
            thumb_bytes += sum((Path(root) / f).stat().st_size for f in files)

    print(f"\n  {paint('Media Library Index:', '1;37')}")
    print(f"    • Total Indexed Items:   {total_assets:,} items")
    print(f"    • Pictures:             {pictures:,}")
    print(f"    • Videos:               {videos:,}")
    print(f"    • Audio Files:          {audio:,}")
    print(f"    • Total Media Size:     {format_bytes(total_media_size)}")
    print(f"    • Hidden (Admin-only):  {hidden_assets:,}")
    print(f"    • Library Roots:        {active_roots}")

    print(f"\n  {paint('Profiles & Access:', '1;37')}")
    print(f"    • Profiles:             {users_count}")
    print(f"    • Active Sessions:      {sessions_count}")

    print(f"\n  {paint('Storage & Databases:', '1;37')}")
    print(f"    • index.db:             {format_bytes(db_size)}")
    print(f"    • index.db-wal:         {format_bytes(wal_size)}")
    print(f"    • archive.db:           {format_bytes(archive_db_size)}")
    print(f"    • Thumbnail Cache:      {thumb_count:,} files ({format_bytes(thumb_bytes)})")
    print()


def main() -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description="Ninaivu Production Database & Storage Maintenance")
    parser.add_argument("--check", action="store_true", help="Perform integrity check on all databases")
    parser.add_argument("--checkpoint", action="store_true", help="Flush and truncate WAL journal")
    parser.add_argument("--vacuum", action="store_true", help="Compact and defragment database files")
    parser.add_argument("--reindex", action="store_true", help="Rebuild database indexes")
    parser.add_argument("--prune-thumbs", action="store_true", help="Remove orphaned thumbnails")
    parser.add_argument("--stats", action="store_true", help="Display storage and database statistics")
    parser.add_argument("--all", action="store_true", help="Run full maintenance sequence")

    args = parser.parse_args()

    cfg = Config.load()
    dbs = [cfg.db_path, cfg.state_dir / "archive.db"]

    if not any([args.check, args.checkpoint, args.vacuum, args.reindex, args.prune_thumbs, args.stats, args.all]):
        parser.print_help()
        return 0

    print(f"\n{paint('=== Ninaivu Database Maintenance ===', '1;36')}")
    start_time = time.time()

    if args.all or args.checkpoint:
        for db in dbs:
            checkpoint_wal(db)

    if args.all or args.check:
        for db in dbs:
            check_integrity(db)

    if args.all or args.prune_thumbs:
        prune_orphan_thumbnails(cfg)

    if args.all or args.vacuum:
        for db in dbs:
            vacuum_db(db)

    if args.all or args.reindex:
        for db in dbs:
            reindex_db(db)

    if args.all or args.stats:
        print_stats(cfg)

    elapsed = time.time() - start_time
    ok(f"Maintenance finished in {elapsed:.2f}s.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
