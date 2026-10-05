"""Moving a library to a new path without throwing away the work done on it.

A first scan of a large library is most of a day: reading every file, writing
two thumbnails for each, then describing them with a model. All of that is
kept, and all of it is reusable on another machine — as long as the library
lands at the same absolute path.

It usually does not. A drive that was ``E:`` here comes up as ``D:`` there, and
Windows hands out letters by the order things are plugged in. Then:

  * every row in ``assets`` points at a root that no longer exists, so the scan
    finds an empty library and prunes it, and

  * every thumbnail is orphaned. A thumbnail's filename is a hash of
    ``"<absolute root>|<relative path>"`` (see ``media.thumb_base``), so
    changing the root changes all of them. On a library of 196,000 items that
    is 366,000 files and 8 GB, and regenerating them is the expensive half of
    a first scan.

Nothing about the pictures has changed, though, and neither has any relative
path — so the new name of every thumbnail can be worked out from the old one.
This renames them and rewrites the paths, which takes minutes where a rescan
takes hours.

The logic lives here rather than in ``tools/reroot_library.py`` because it is
now reachable two ways — that script, and Migration in the console — and two
copies of something that rewrites every path in the index is one copy too
many.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..media.media import thumb_base

log = logging.getLogger(__name__)

#: Every table that records which library folder a row belongs to. Missing one
#: does not fail loudly — it leaves rows pointing at a path that is not there
#: any more, which reads as data quietly disappearing.
ROOT_TABLES = (
    "assets", "occasions", "visibility_batches", "recycled", "scan_runs",
    "bitrot_records", "folder_rules", "pending_uploads", "cloud_uploads",
)

#: Columns in ``archive.db`` that hold a path *inside the library*.
#:
#: The archive engine keeps its own database beside the index, and the library
#: is where it puts things — its ``destination``. Everything else it records is
#: on the source disks somebody is copying *from*, which have nothing to do
#: with where the library lives and must not be touched: rewriting
#: ``files.source_path`` would tell the archive it had already copied files from
#: a disk it has never seen.
ARCHIVE_PATHS = (
    ("files", "destination_path"),
    ("jobs", "destination"),
    ("job_folders", "folder"),
)


class Refused(Exception):
    """Raised instead of rewriting when the request does not make sense."""


class NothingToDo(Refused):
    """The library is already where it is being moved to.

    Its own class because the two callers want opposite things from it. The
    command line has always treated this as success — you asked for a state
    the library is already in, and it is in it — while the console wants to
    say so rather than report a move that did not happen.
    """


def _separator(path: str) -> str:
    """``\\`` for a Windows path — a drive letter or a share — and ``/`` for any
    other. Decided by the path, not by the computer running this: a library
    moved from Windows to a Mac has one of each."""
    windows = (len(path) > 1 and path[1] == ":" and path[0].isalpha()) or path.startswith("\\\\")
    return "\\" if windows else "/"


def moved(value: str | None, old_root: str, new_root: str) -> str | None:
    """*value* respelled under *new_root*, or None if it is not *old_root* or
    inside it.

    Inside means under a separator: ``E:\\Lib-old`` is a sibling of
    ``E:\\Lib``, not part of it. The rest of the path takes the new root's
    separator, so ``E:\\Lib\\2019\\a.jpg`` moved to a Mac becomes
    ``/Volumes/Lib/2019/a.jpg`` and not a file named with backslashes.
    """
    if not value:
        return None
    old, new = old_root.rstrip("\\/"), new_root.rstrip("\\/")
    if value.rstrip("\\/") == old:
        return new
    old_sep, new_sep = _separator(old_root), _separator(new_root)
    if not value.startswith(old + old_sep):
        return None
    rest = value[len(old) + 1:]
    if old_sep != new_sep:
        rest = rest.replace(old_sep, new_sep)
    return new + new_sep + rest


def _respell(conn: sqlite3.Connection, table: str, column: str, key: str,
             old_root: str, new_root: str) -> int:
    """Move every *column* of *table* that is *old_root* or inside it. Rows are
    found by prefix in SQL and respelled here, where the separators are known."""
    old = old_root.rstrip("\\/")
    inside = old + _separator(old_root)
    rows = conn.execute(
        f"SELECT {key}, {column} FROM {table} "
        f"WHERE {column} = ? OR {column} = ? OR substr({column}, 1, ?) = ?",
        (old, inside, len(inside), inside)).fetchall()
    changes = [(new, row_id) for row_id, value in rows
               if (new := moved(value, old_root, new_root)) is not None and new != value]
    if changes:
        conn.executemany(f"UPDATE {table} SET {column}=? WHERE {key}=?", changes)
    return len(changes)


def normalise(path: str | Path) -> str:
    """The spelling the scanner would have stored for this folder."""
    return str(Path(path).expanduser())


def known_roots(conn: sqlite3.Connection) -> list[str]:
    """Every library folder the index has rows for, as it spells them."""
    return [row[0] for row in conn.execute("SELECT DISTINCT root FROM assets")
            if row[0]]


def resolve(conn: sqlite3.Connection, old_root: str) -> str:
    """The index's own spelling of *old_root*, or Refused.

    Matched without regard to case, and then the index's spelling is used from
    here on. Windows treats ``e:\\photos`` and ``E:\\Photos`` as the same
    folder, so somebody typing the drive letter in lower case means the library
    they have; every UPDATE below matches on this string exactly, and SQLite
    does not share the filesystem's indifference.
    """
    known = known_roots(conn)
    exact = {root.casefold(): root for root in known}.get(normalise(old_root).casefold())
    if exact is None:
        raise Refused(
            f"Nothing in the index is under {old_root}. "
            f"It records: {', '.join(known) or '(nothing)'}")
    return exact


@dataclass
class Report:
    """What a reroot did, or would do."""

    old_root: str = ""
    new_root: str = ""
    items: int = 0
    thumbnails: int = 0
    renamed: int = 0
    #: Items that never had a thumbnail, or whose thumbnail has already moved.
    without_thumbnails: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    config_updated: bool = False
    dry_run: bool = True
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_root": self.old_root, "new_root": self.new_root,
            "items": self.items, "thumbnails": self.thumbnails,
            "renamed": self.renamed,
            "without_thumbnails": self.without_thumbnails,
            "rows": self.rows, "config_updated": self.config_updated,
            "dry_run": self.dry_run, "warnings": self.warnings,
        }


def thumb_files(thumbs: Path) -> dict[str, list[str]]:
    """Hash -> the thumbnail filenames made from it, read in one pass.

    Asking the filesystem per asset would be 184,000 directory lookups. The
    whole tree is 256 folders, so one walk answers for everything.
    """
    found: dict[str, list[str]] = {}
    for shard in thumbs.iterdir() if thumbs.is_dir() else []:
        if not shard.is_dir():
            continue
        try:
            names = os.listdir(shard)
        except OSError:
            continue
        for name in names:
            digest, _, rest = name.partition("_")
            if rest:
                found.setdefault(digest, []).append(name)
    return found


def plan(conn: sqlite3.Connection, old_root: str, new_root: str
         ) -> tuple[list[tuple[int, str, str]], int]:
    """(asset id, old thumb base, new thumb base) for everything that moves."""
    rows = conn.execute(
        "SELECT id, rel_path, thumb FROM assets WHERE root=?", (old_root,),
    ).fetchall()
    moves = []
    for asset_id, rel_path, old_base in rows:
        # Nothing to move for an item that never had a thumbnail — a sound
        # file, a damaged photograph. Naming one for it here left the gallery
        # asking for a picture that does not exist: a blank tile, where the
        # item used to show the sign for its kind.
        if not old_base:
            continue
        new_base = thumb_base(new_root, rel_path)
        if new_base != old_base:
            moves.append((asset_id, old_base, new_base))
    return moves, len(rows)


def rename_thumbs(thumbs: Path, moves: list[tuple[int, str, str]],
                  dry_run: bool,
                  on_progress: Callable[[int, int], None] | None = None
                  ) -> tuple[int, int]:
    """Rename every derivative. Safe to run again after an interruption."""
    by_digest = thumb_files(thumbs)
    renamed = missing = 0
    for index, (_, old_base, new_base) in enumerate(moves, start=1):
        if on_progress and index % 2000 == 0:
            on_progress(index, len(moves))
        if not old_base:
            continue
        old_digest = old_base.rpartition("/")[2]
        new_shard, _, new_digest = new_base.rpartition("/")
        names = by_digest.get(old_digest)
        if not names:
            missing += 1          # never had one, or already moved
            continue
        target_dir = thumbs / new_shard
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            suffix = name.partition("_")[2]
            source = thumbs / old_base.rpartition("/")[0] / name
            target = target_dir / f"{new_digest}_{suffix}"
            if dry_run:
                continue
            try:
                os.replace(source, target)
            except FileNotFoundError:
                pass
        renamed += 1
    return renamed, missing


def rewrite_paths(conn: sqlite3.Connection, old_root: str, new_root: str,
                  moves: list[tuple[int, str, str]]) -> dict[str, int]:
    """Point the index at the new folder, in one transaction."""
    counts: dict[str, int] = {}
    with conn:
        conn.executemany("UPDATE assets SET thumb=? WHERE id=?",
                         [(new, asset_id) for asset_id, _, new in moves])
        counts["assets.thumb"] = len(moves)
        for table in ROOT_TABLES:
            try:
                cursor = conn.execute(
                    f"UPDATE {table} SET root=? WHERE root=?",
                    (new_root, old_root))
            except sqlite3.OperationalError:
                continue          # a table this database does not have
            if cursor.rowcount:
                counts[f"{table}.root"] = cursor.rowcount
        # A member confined to a folder inside the library is confined by its
        # absolute path, so it moves with everything else. Guarded like the
        # rest: `users` belongs to the sign-in schema, and an index that has
        # never had anybody sign in does not have the table at all — which
        # must not roll back everything above it.
        try:
            changed = _respell(conn, "users", "library", "id", old_root, new_root)
            if changed:
                counts["users.library"] = changed
        except sqlite3.OperationalError:
            pass
    return counts


def rewrite_archive(path: Path, old_root: str, new_root: str,
                    dry_run: bool) -> dict[str, int]:
    """Point the archive engine at the moved library.

    ``archive.db`` was left out of the move. After one, it still named the old
    destination in every row it had written — so the archive believed 9,800
    verified files were at a path that no longer existed, and the next run
    would have looked for its own leftovers in a folder that was not there.

    Paths are matched exactly, or as a parent of, the old root; nothing on the
    source side is touched. Matching is case-sensitive, like the index's own
    rewrite: the root a row carries was written by the same code that spelled
    it into the index.
    """
    counts: dict[str, int] = {}
    if not path.is_file() or dry_run:
        return counts

    old, new = old_root.rstrip("\\/"), new_root.rstrip("\\/")

    conn = sqlite3.connect(path)
    try:
        with conn:
            for table, column in ARCHIVE_PATHS:
                try:
                    changed = _respell(conn, table, column, "rowid",
                                       old_root, new_root)
                except sqlite3.OperationalError:
                    continue              # a table this archive has not made
                if changed:
                    counts[f"{table}.{column}"] = changed
            # What the console offers next time somebody opens the archive page.
            try:
                cursor = conn.execute(
                    "UPDATE config SET value=? "
                    "WHERE key='last_destination' AND value=?", (new, old))
                if cursor.rowcount:
                    counts["config.last_destination"] = cursor.rowcount
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()
    return counts


def rewrite_config(path: Path, old_root: str, new_root: str,
                   dry_run: bool) -> bool:
    """The library folders the console remembers."""
    if not path.is_file():
        return False
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(stored, dict):
        return False
    before = json.dumps(stored, sort_keys=True)
    roots = stored.get("roots")
    if isinstance(roots, list):
        stored["roots"] = [new_root if normalise(r) == old_root else r
                           for r in roots]
    if normalise(stored.get("active_root") or "") == old_root:
        stored["active_root"] = new_root
    if json.dumps(stored, sort_keys=True) == before:
        return False
    if not dry_run:
        # Written beside it and renamed over it, never rewritten in place: an
        # interrupted write left a truncated config.json, which the next start
        # sets aside as broken — taking the library folders, the backup and
        # the mail settings with it. The same way Config.save writes it,
        # including the owner-only permissions (it holds the SMTP password).
        # A name of its own, so it never shares a temporary with a save the
        # running server makes at the same moment.
        tmp = path.with_name(f"{path.name}.reroot.tmp")
        with open(tmp, "w", encoding="utf-8") as out:
            out.write(json.dumps(stored, indent=2))
            out.flush()
            os.fsync(out.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:                                 # Windows, and that is fine
            pass
        os.replace(tmp, path)
    return True


def reroot(state_dir: Path | str, old_root: str, new_root: str, *,
           dry_run: bool = True,
           on_progress: Callable[[int, int], None] | None = None) -> Report:
    """Move a library's recorded path, thumbnails and all.

    With *dry_run* nothing is written: the report says what would change,
    which is the only honest way to offer this to somebody who is about to
    rewrite every path in their index.
    """
    state = Path(state_dir)
    old, new = normalise(old_root), normalise(new_root)
    # On Windows the same folder can be spelled several ways, and re-rooting a
    # library onto itself would rename every thumbnail to the name it already
    # has — an expensive way to achieve nothing.
    if old.casefold() == new.casefold():
        raise NothingToDo("Those are the same folder; there is nothing to do.")
    if not new:
        raise Refused("Give the folder the library is at now.")

    index = state / "index.db"
    if not index.is_file():
        raise Refused(f"There is no index at {index}.")

    report = Report(new_root=new, dry_run=dry_run)
    if not Path(new).is_dir():
        report.warnings.append(
            f"{new} is not a folder on this machine. The index will point at "
            f"it, but nothing will be found there until it is.")

    conn = sqlite3.connect(index)
    try:
        old = resolve(conn, old)
        report.old_root = old
        if old != normalise(old_root):
            report.warnings.append(
                f"Read as {old}, which is how the index spells it.")

        moves, total = plan(conn, old, new)
        report.items, report.thumbnails = total, len(moves)

        renamed, missing = rename_thumbs(
            state / "thumbs", moves, dry_run, on_progress)
        report.renamed, report.without_thumbnails = renamed, missing
        if dry_run:
            return report

        try:
            report.rows = rewrite_paths(conn, old, new, moves)
        except Exception:
            # The rewrite is one transaction and has rolled back, so the index
            # still names the old thumbnails; put them back under those names,
            # or every tile of the library goes blank until a second attempt.
            rename_thumbs(state / "thumbs",
                          [(i, new_base, old_base) for i, old_base, new_base in moves],
                          False)
            raise
        report.rows.update(rewrite_archive(
            state / "archive.db", old, new, False))
        report.config_updated = rewrite_config(
            state / "config.json", old, new, False)
    finally:
        conn.close()

    log.info("rerooted %s to %s: %s items, %s thumbnails renamed",
             old, new, f"{report.items:,}", f"{report.renamed:,}")
    return report
