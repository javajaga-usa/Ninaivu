#!/usr/bin/env python3
"""Put the photographs back from a Ninaivu off-site copy, on any computer.

Copy the ``ninaivu-offsite`` folder down from wherever it is kept (a bucket,
a friend's disk, a share), then:

    ninaivu offsite-restore --recovery ninaivu-recovery-XXXX.json COPY OUTPUT

or with the passphrase and ``ninaivu-encryption.json`` (from the Drive folder,
or kept with the recovery papers):

    ninaivu offsite-restore --params ninaivu-encryption.json COPY OUTPUT

The manifest in COPY says which encrypted object is which file; each is
decrypted to its own name and folder under OUTPUT, with its time put back.
Nothing in COPY is changed, and an existing file in OUTPUT is never replaced.

Exit codes: 0 every file restored, 1 some failed, 2 could not start.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from ..cloud import keyring
from ..cloud.offsite import MANIFEST, FolderTarget, fetch_one, read_manifest


def _key(args: argparse.Namespace) -> bytes:
    if args.recovery:
        return keyring.key_from(recovery=json.loads(Path(args.recovery).read_text(encoding="utf-8")))
    params = json.loads(Path(args.params).read_text(encoding="utf-8"))
    passphrase = args.passphrase if args.passphrase is not None else getpass.getpass("Passphrase: ")
    return keyring.key_from(passphrase=passphrase, params=params)


class _Here(FolderTarget):
    """The copy exactly where it is: COPY is the ninaivu-offsite folder itself."""

    def __init__(self, folder: Path):
        self.base = folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ninaivu offsite-restore", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--recovery", help="the recovery file from Ninaivu's console")
    which.add_argument("--params", help="ninaivu-encryption.json, with the passphrase")
    parser.add_argument("--passphrase", help=argparse.SUPPRESS)
    parser.add_argument("copy", type=Path, help="the ninaivu-offsite folder")
    parser.add_argument("output", type=Path, help="where to put the photographs")
    args = parser.parse_args(argv)
    try:
        key = _key(args)
        copy = args.copy if (args.copy / MANIFEST).exists() else args.copy / "ninaivu-offsite"
        target = _Here(copy)
        entries = read_manifest(target, key, args.output / ".ninaivu-restore")
    except (OSError, ValueError, KeyError) as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 2
    libraries = {e["library"] for e in entries}
    done, failed = 0, 0
    for entry in entries:
        parts = ([entry["library"]] if len(libraries) > 1 else []) + entry["path"].split("/")
        out = args.output.joinpath(*parts)
        if out.exists():
            continue
        try:
            fetch_one(target, entry, key, out)
            done += 1
        except (OSError, ValueError) as exc:
            failed += 1
            print(f"{entry['path']}: {exc}", file=sys.stderr)
    try:
        (args.output / ".ninaivu-restore").rmdir()
    except OSError:
        pass
    print(f"Restored {done} of {len(entries)} files into {args.output}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
