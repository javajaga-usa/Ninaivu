#!/usr/bin/env python3
"""List connected MTP/USB devices or inspect device media from the CLI.

    python tools/import_device.py
    python tools/import_device.py "This PC\\Apple iPhone\\Internal Storage"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ninaivu.utils import devices


def main() -> int:
    reason = devices.unavailable_reason()
    if reason:
        print(f"Device support unavailable: {reason}")
        return 1

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        found = devices.list_devices()
        if not found:
            print("No devices detected. If your phone is connected, unlock it and tap Trust.")
            return 0
        print(f"Found {len(found)} connected device(s):")
        for d in found:
            print(f"  - {d['name']} -> {d['path']}")
        return 0

    print(f"Inspecting: {target}")
    try:
        entries = devices.list_folder(target)
    except Exception as exc:
        print(f"Error reading {target}: {exc}")
        return 1

    folders = [e for e in entries if e.get("is_folder")]
    files = [e for e in entries if not e.get("is_folder")]
    print(f"Found {len(folders)} folder(s) and {len(files)} file(s):")
    for f in folders[:10]:
        print(f"  [DIR]  {f['name']}")
    if len(folders) > 10:
        print(f"  ... and {len(folders) - 10} more folders")
    for f in files[:10]:
        print(f"  [FILE] {f['name']} ({f.get('size', 0)} bytes)")
    if len(files) > 10:
        print(f"  ... and {len(files) - 10} more files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
