#!/usr/bin/env python3
"""Put the photographs back from a Ninaivu off-site copy, on any computer.

Copy the ``ninaivu-offsite`` folder down from wherever it is kept (a bucket,
a friend's disk, a share), then:

    ninaivu offsite-restore --recovery ninaivu-recovery-XXXX.json COPY OUTPUT

or with the passphrase, which uses the ``ninaivu-encryption.json`` kept in
COPY beside the manifest (or one given with --params, from the Drive folder
or kept with the recovery papers):

    ninaivu offsite-restore COPY OUTPUT
    ninaivu offsite-restore --params ninaivu-encryption.json COPY OUTPUT

Without Ninaivu installed, Python with only the ``cryptography`` package is
enough, from a copy of Ninaivu's source:

    python ninaivu/cli/offsite_restore.py --recovery ninaivu-recovery-XXXX.json COPY OUTPUT

The manifest in COPY says which encrypted object is which file; each is
decrypted to its own name and folder under OUTPUT, with its time put back,
and checked against the SHA-256 recorded when it was sent. A file that
changed is kept in the copy in every version sent; the newest is restored,
or with --all-versions every one (the older ones beside it, marked
"(restored)"). Nothing in COPY is changed, and an existing file in OUTPUT is
never replaced: one with the same bytes counts as already restored; a
different one, a damaged copy included, is left alone and the restored file
goes beside it as "name (restored).ext".

Exit codes: 0 every file restored or already there, 1 some failed, 2 could
not start.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

if not __package__:
    # Run as a file: load the few modules a restore needs (cloud/offsite.py,
    # restore.py, crypto.py, keyring.py and what they use) without
    # ninaivu/__init__.py, which brings in Flask, numpy, OpenCV and the rest
    # of the app. Empty stand-ins for the packages point at their folders.
    import types

    _package = Path(__file__).resolve().parents[1]
    if "ninaivu" not in sys.modules:
        for _name, _folder in (("ninaivu", _package), ("ninaivu.cli", _package / "cli"),
                               ("ninaivu.cloud", _package / "cloud"),
                               ("ninaivu.utils", _package / "utils")):
            _stand_in = types.ModuleType(_name)
            _stand_in.__path__ = [str(_folder)]
            sys.modules[_name] = _stand_in
    # This folder is first on the path for a script; its modules are not
    # meant to be imported by bare name.
    if sys.path and Path(sys.path[0] or ".").resolve() == Path(__file__).resolve().parent:
        del sys.path[0]
    __package__ = "ninaivu.cli"

from ..cloud import keyring  # noqa: E402
from ..cloud.offsite import (  # noqa: E402
    ALREADY,
    BESIDE,
    MANIFEST,
    FolderTarget,
    by_age,
    fetch_one,
    latest,
    read_manifest,
    restore_path,
)


def _params_in(copy: Path) -> list[dict]:
    """The key settings kept beside the manifest: the one named for the
    manifest's key first."""
    from ..cloud import crypto                               # noqa: PLC0415
    try:
        wanted = (crypto.read_key_id(copy / MANIFEST) or b"").hex()
    except OSError:
        wanted = ""
    found = []
    for path in sorted(copy.glob("ninaivu-encryption*.json")):
        try:
            params = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(params, dict):
            found.append(params)
    return sorted(found, key=lambda p: str(p.get("key_id") or "") != wanted)


def _key(args: argparse.Namespace, copy: Path) -> bytes:
    if args.recovery:
        return keyring.key_from(recovery=json.loads(Path(args.recovery).read_text(encoding="utf-8")))
    if args.params:
        candidates = [json.loads(Path(args.params).read_text(encoding="utf-8"))]
    else:
        candidates = _params_in(copy)
        if not candidates:
            raise ValueError("there is no ninaivu-encryption.json in the copy; give the "
                             "recovery file (--recovery) or the key settings (--params)")
    passphrase = args.passphrase if args.passphrase is not None else getpass.getpass("Passphrase: ")
    problem: Exception | None = None
    for params in candidates:
        try:
            return keyring.key_from(passphrase=passphrase, params=params)
        except (ValueError, KeyError, TypeError) as exc:
            problem = problem or exc
    raise ValueError(str(problem))


class _Here(FolderTarget):
    """The copy exactly where it is: COPY is the ninaivu-offsite folder itself."""

    def __init__(self, folder: Path):
        self.base = folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ninaivu offsite-restore", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--recovery", help="the recovery file from Ninaivu's console")
    which.add_argument("--params", help="ninaivu-encryption.json, with the passphrase "
                                        "(by default, the one in COPY)")
    parser.add_argument("--passphrase", help=argparse.SUPPRESS)
    parser.add_argument("--all-versions", action="store_true",
                        help="every version of a changed file, not only the newest")
    parser.add_argument("copy", type=Path, help="the ninaivu-offsite folder")
    parser.add_argument("output", type=Path, help="where to put the photographs")
    args = parser.parse_args(argv)
    try:
        copy = args.copy if (args.copy / MANIFEST).exists() else args.copy / "ninaivu-offsite"
        key = _key(args, copy)
        target = _Here(copy)
        listed = read_manifest(target, key, args.output / ".ninaivu-restore")
    except (OSError, ValueError, KeyError) as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 2
    entries = by_age(listed) if args.all_versions else latest(listed)
    libraries = {e["library"] for e in entries}
    done = already = beside = failed = 0
    for entry in entries:
        try:
            # An existing file is checked against the copy, not skipped on
            # sight: a damaged one is what a restore is for.
            outcome = fetch_one(target, entry, key,
                                restore_path(args.output, entry, libraries), base=args.output)
        except (OSError, ValueError) as exc:
            failed += 1
            print(f"{entry.get('path')}: {exc}", file=sys.stderr)
            continue
        if outcome == ALREADY:
            already += 1
        else:
            done += 1
            beside += outcome == BESIDE
    try:
        (args.output / ".ninaivu-restore").rmdir()
    except OSError:
        pass
    print(f"Restored {done} of {len(entries)} files into {args.output}.")
    if already:
        print(f"{already} were already there, unchanged.")
    if beside:
        print(f"{beside} went beside a different file of the same name, marked (restored).")
    if failed:
        print(f"{failed} could not be restored.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
