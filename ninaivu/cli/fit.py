"""Would a copy of the library onto another disk keep every file?

``ninaivu fit <folder>`` prints what ``/api/admin/fit`` answers: the kind of
disk the folder is on, and each file whose name, case, Unicode spelling or
size that kind of disk could not keep, with a name that would work
(storage/fit.py). It opens the index read-only and copies nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .replica import use_utf8_output
from .space import size

#: How each problem is named in the printed report.
LABELS = {
    "too_large": "too large for this disk (FAT32 holds up to 4 GB a file)",
    "forbidden_character": 'a character this disk refuses (< > : " \\ | ? *)',
    "trailing_dot_or_space": "a name ending in a dot or a space",
    "reserved_name": "a name Windows keeps for a device (CON, NUL, COM1...)",
    "name_too_long": "a name longer than this disk allows",
    "same_name_different_case": "another file with the same name in different case",
    "same_name_different_spelling": "another file with the same name spelled the other Unicode way",
}


def report(cfg, target: str, *, limit: int = 1000) -> dict:
    from ..storage import fit                                     # noqa: PLC0415
    path = Path(cfg.db_path)
    if not path.exists():
        raise SystemExit(f"No index at {path}: has Ninaivu scanned a library here?")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        roots = list(cfg.roots or ([cfg.active_root] if cfg.active_root else []))
        return fit.check_library(conn, roots, target, limit=limit)
    finally:
        conn.close()


def print_report(data: dict) -> None:
    kind = data["filesystem"] or "a disk whose kind could not be told"
    print(f"Copying {data['files']:,} files ({size(data['bytes'])}) to {data['target']}")
    print(f"That folder is on {kind}; checked against the {data['family']} rules.")
    if data["coarse_times"]:
        print("This disk keeps modified times to 2 seconds only; Ninaivu allows for that.")
    if data["fits"]:
        print("Every file can be copied there as it is.")
        return
    print()
    for problem, count in data["problems"].items():
        print(f"  {count:>7,}  {LABELS.get(problem, problem)}")
    print()
    for item in data["listed"]:
        line = f"  {item['path']}: {LABELS.get(item['problem'], item['problem'])}"
        if item.get("suggestion") and item["suggestion"] != item["path"]:
            line += f"\n      would be copied as {item['suggestion']}"
        if item.get("clashes_with"):
            line += f"\n      clashes with {', '.join(item['clashes_with'])}"
        print(line)
    if not data["complete"]:
        print(f"  ... and more; the first {len(data['listed']):,} are listed (--limit for more)")


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(prog="ninaivu fit", description=__doc__.split("\n")[0])
    parser.add_argument("target", help="the folder a copy of the library would go to")
    parser.add_argument("--state-dir", default=None, help="Ninaivu's state folder")
    parser.add_argument("--limit", type=int, default=1000, help="files to list at most")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    from ..server.config import Config                           # noqa: PLC0415
    if args.state_dir:
        os.environ["NINAIVU_STATE_DIR"] = str(Path(args.state_dir).expanduser())
    data = report(Config.load(), args.target, limit=max(1, args.limit))
    if args.json:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        print_report(data)
    return 0 if data["fits"] else 1


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
