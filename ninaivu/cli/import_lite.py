"""Bring a Ninaivu Lite household into Ninaivu.

    python -m ninaivu import-lite lite-export.json

Make the file in Ninaivu Lite first (``python -m ninaivu_lite --export
lite-export.json``), add the same photo folders to Ninaivu and let its first
scan finish, then stop Ninaivu and run this. People (with their passwords and
PINs), who sees which folder and photograph, favourites, albums and share
links come across; nothing already in Ninaivu is overwritten. The work is in
``ninaivu/storage/lite_import.py``.

The file holds password and PIN hashes: delete it once the move is done.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ninaivu import-lite",
        description="Bring the people, visibility, favourites, albums and share "
                    "links from a Ninaivu Lite export into Ninaivu.")
    parser.add_argument("file", help="the file Ninaivu Lite's --export wrote")
    args = parser.parse_args(argv)

    from ..server import auth, runfile                            # noqa: PLC0415
    from ..server.config import Config                            # noqa: PLC0415
    from ..storage import db, lite_import                         # noqa: PLC0415

    try:
        data = lite_import.load(args.file)
    except lite_import.LiteImportError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 2

    cfg = Config.load()
    try:
        # Not while the server runs: its scan may be writing the same rows.
        with runfile.server_lock(cfg.state_dir):
            from ..storage.upgrade import before_opening_the_index        # noqa: PLC0415
            if not before_opening_the_index(cfg.state_dir):
                return 1
            conn = db.init_db(cfg.db_path)
            auth.init_auth_schema(conn)
            if not conn.execute("SELECT 1 FROM assets LIMIT 1").fetchone():
                print("  Ninaivu has not indexed any photographs yet. Add the same "
                      "folders Ninaivu Lite used, let the first scan finish, then "
                      "run this again.", file=sys.stderr)
                return 2
            report = lite_import.import_export(conn, list(cfg.roots), data)
    except runfile.AlreadyRunning:
        print("  Stop Ninaivu first, then run this again.", file=sys.stderr)
        return 1

    def names(key: str) -> str:
        return ", ".join(report[key]) or "none"

    print(f"  People added: {names('people_added')}")
    if report["people_already_here"]:
        print(f"  Already in Ninaivu, left as they are: {names('people_already_here')}")
    if report["new_password_needed"]:
        print(f"  Will choose a new password at sign-in: {names('new_password_needed')}")
    if report["pins_left_off"]:
        print(f"  PIN not carried (set it again in the console): {names('pins_left_off')}")
    print(f"  Folder rules: {report['folder_rules']} added, "
          f"{report['folder_rules_kept']} kept as Ninaivu had them")
    print(f"  Photographs with their own visibility: {report['visibility']}")
    print(f"  Favourites: {report['favourites']}")
    print(f"  Albums: {report['albums']}, with {report['album_photos']} photographs")
    print(f"  Share links: {report['shares']} kept working, "
          f"{report['shares_skipped']} skipped")
    if report["folders_not_in_library"]:
        print("  Lite folders that are not Ninaivu library folders (their "
              "photographs could not be matched):")
        for folder in report["folders_not_in_library"]:
            print(f"    {folder}")
    if report["photos_not_found"]:
        print(f"  Photographs not found in Ninaivu's index: {report['photos_not_found']}")
        for label in report["photos_not_found_examples"]:
            print(f"    {label}")
    print("  The export file holds password hashes: delete it now the move is done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
