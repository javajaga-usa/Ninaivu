#!/usr/bin/env python3
"""Move a library to a new path without throwing away the work done on it.

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
    changing the root changes all of them. On the library this was written for
    that is 343,170 files and 8.1 GB, and regenerating them is the expensive
    half of a first scan.

Nothing about the pictures has changed, though, and neither has any relative
path — so the new name of every thumbnail can be worked out from the old one.
This renames them and rewrites the paths, which takes minutes where a rescan
takes hours.

    python -m ninaivu reroot --from "E:\\MasterArchive" --to "D:\\MasterArchive"
    python -m ninaivu reroot --from ... --to ... --dry-run

Stop Ninaivu first, or use **Migration** in the console, which does the same
job and stands the background work down for you. The work itself lives in
``ninaivu/storage/reroot.py``, because both of those reach it and two copies of
something that rewrites every path in the index is one copy too many.

What this does not touch: renditions and faces are keyed by asset id and do
not care where the library lives, and the Archive's own database
(``archive.db``) records where it copied things from and is a separate
question from the library index.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


from ..server import runfile
from ..server.config import Config
from ..storage import reroot as reroot_kit


def use_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def refuse_if_running(state_dir: Path) -> None:
    """Stop here if a server holds the state folder.

    The lock file stays behind after every clean stop, so its being there says
    nothing; whether its OS lock can be taken is what says a server is up.
    """
    if not Path(state_dir).is_dir():
        return                  # nothing has run here; the reroot says what is missing
    try:
        with runfile.server_lock(state_dir):
            pass
    except runfile.AlreadyRunning:
        raise SystemExit(
            f"Ninaivu looks like it is running ({state_dir / 'ninaivu-server.lock'} "
            f"is locked). Stop it first, or use Migration in the console, which "
            f"stands the background jobs down for you.") from None


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--from", dest="old", required=True,
                        help="the library folder as it is recorded now")
    parser.add_argument("--to", dest="new", required=True,
                        help="the library folder on this machine")
    parser.add_argument("--state-dir", default=None,
                        help="Ninaivu's state folder (default: the usual one)")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would change and change nothing")
    args = parser.parse_args(argv)

    state_dir = (Path(args.state_dir).expanduser() if args.state_dir
                 else Config().state_dir)
    if not args.dry_run:
        refuse_if_running(state_dir)

    def said(done: int, total: int) -> None:
        print(f"  {done:,} of {total:,} thumbnails…", flush=True)

    try:
        report = reroot_kit.reroot(state_dir, args.old, args.new,
                                   dry_run=args.dry_run, on_progress=said)
    except reroot_kit.NothingToDo as settled:
        # Asked for a state the library is already in. That is not a failure.
        print(str(settled))
        return 0
    except reroot_kit.Refused as refusal:
        raise SystemExit(str(refusal)) from None

    for warning in report.warnings:
        print(warning)
    print(f"{report.items:,} items under {report.old_root}")
    print(f"{report.thumbnails:,} thumbnails to rename")
    verb = "would rename" if args.dry_run else "renamed"
    print(f"{verb} {report.renamed:,} files"
          + (f", {report.without_thumbnails:,} had none to rename"
             if report.without_thumbnails else ""))

    if args.dry_run:
        print("\nDry run: the index and config.json were not touched.")
        return 0

    for name, count in report.rows.items():
        print(f"  {name}: {count:,}")
    if report.config_updated:
        print("  config.json: the library folder was updated")
    print(f"\nDone. Start Ninaivu; it should find {report.items:,} items "
          f"already indexed and rebuild nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
