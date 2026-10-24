"""Where the library's bytes are, and what a light library would save.

``ninaivu space`` prints what the console's ``/api/admin/space`` answers:
the library by kind of file, year and largest folders, the light copies'
estimated size (storage/space.py says how the estimate is made), duplicates,
and Ninaivu's own state folder. It opens the index read-only and changes
nothing, so it can run while Ninaivu is serving.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .replica import use_utf8_output

#: How each class is named in the printed report.
LABELS = {
    "jpeg": "JPEG photos (to AVIF)",
    "raw": "RAW photos (to an AVIF rendition)",
    "png": "PNG and screenshots (to lossless WebP)",
    "heic": "iPhone HEIC photos (kept)",
    "other_picture": "Other pictures (kept)",
    "video": "Videos, mostly H.264 (to HEVC)",
    "legacy_video": "Older videos: AVI, MKV, WMV, MTS (to HEVC)",
    "phone_video": "iPhone videos (kept)",
    "audio": "Sound (kept)",
    "other": "Other files (kept)",
}


def size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} bytes" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024
    return ""                                      # pragma: no cover


def report(cfg, *, fresh: bool = False) -> dict:
    from ..storage import space                                   # noqa: PLC0415
    path = Path(cfg.db_path)
    if not path.exists():
        raise SystemExit(f"No index at {path}: has Ninaivu scanned a library here?")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        libraries = list(cfg.roots or ([cfg.active_root] if cfg.active_root else []))
        library = space.library_report(conn, libraries)
        duplicates = space.duplicates_report(conn, libraries)
    finally:
        conn.close()
    state = space.cached_state_report(
        cfg.state_dir, elsewhere=(("backups", cfg.backup_dir), ("ai_models", cfg.ai_models_dir)),
        fresh=fresh)
    return {"library": library, "duplicates": duplicates, "state": state}


def print_report(data: dict) -> None:
    lib = data["library"]
    print(f"Library: {lib['files']:,} files, {size(lib['bytes'])}")
    print()
    print(f"  {'Kind':<44} {'Files':>9} {'Now':>11} {'Light copy (estimate)':>25}")
    for row in lib["classes"]:
        if not row["files"]:
            continue
        light = ("same" if row["kept_as_is"] else
                 f"{size(row['light_bytes_low'])} to {size(row['light_bytes_high'])}")
        print(f"  {LABELS.get(row['class'], row['class']):<44} {row['files']:>9,} "
              f"{size(row['bytes']):>11} {light:>25}")
    print()
    print(f"  A light library would take {size(lib['light_bytes_low'])} to "
          f"{size(lib['light_bytes_high'])}, saving {size(lib['saving_bytes_low'])} to "
          f"{size(lib['saving_bytes_high'])}.")
    print("  These are estimates from the kind of each file; no file was opened.")
    print()
    print("By year:")
    for row in lib["years"]:
        print(f"  {row['year'] or 'no date':<8} {row['files']:>9,} files  {size(row['bytes']):>11}")
    print()
    print("Largest folders:")
    for row in lib["folders"][:10]:
        print(f"  {size(row['bytes']):>11}  {row['files']:>7,} files  {row['folder'] or '(top)'}")
    print()
    dup = data["duplicates"]
    exact = dup["exact"]
    if exact["available"]:
        print(f"Exact copies: {exact['spare_files']:,} spare files in {exact['groups']:,} groups, "
              f"{size(exact['spare_bytes'])}")
    else:
        print("Exact copies: not known yet (turn on content fingerprints in Advanced settings)")
    print(f"Similar photos: {dup['similar']['files']:,} files in {dup['similar']['groups']:,} groups")
    print()
    state = data["state"]
    print(f"Ninaivu's own files ({state['path']}): {size(state['bytes'])}")
    for row in state["entries"][:12]:
        print(f"  {size(row['bytes']):>11}  {row['name']}")
    for row in state["elsewhere"]:
        print(f"  {size(row['bytes']):>11}  {row['what']} at {row['path']}")


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(prog="ninaivu space", description=__doc__.split("\n")[0])
    parser.add_argument("--state-dir", default=None, help="Ninaivu's state folder")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    from ..server.config import Config                           # noqa: PLC0415
    if args.state_dir:
        os.environ["NINAIVU_STATE_DIR"] = str(Path(args.state_dir).expanduser())
    data = report(Config.load(), fresh=True)
    if args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
    else:
        print_report(data)
    return 0


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
