#!/usr/bin/env python3
"""Decrypt Ninaivu's encrypted Google Drive backups.

Download the Ninaivu folder from Drive (or any part of it), then:

    python tools/cloud_decrypt.py --recovery ninaivu-recovery-XXXX.json DOWNLOADED OUTPUT

or, without the recovery file, with the passphrase and the
``ninaivu-encryption.json`` file from the same Drive folder:

    python tools/cloud_decrypt.py --params ninaivu-encryption.json DOWNLOADED OUTPUT
    (the passphrase is asked for, not typed on the command line)

Every ``*.ninaivu`` file under DOWNLOADED is decrypted into the same relative
place under OUTPUT, without the ``.ninaivu`` suffix. Nothing under DOWNLOADED is
changed or deleted, an existing output file is never overwritten, and a file
that fails its authentication check is reported and not written.

Exit codes: 0 everything decrypted, 1 some files failed, 2 could not start.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ninaivu.cloud import crypto, keyring  # noqa: E402


def _key(args: argparse.Namespace) -> bytes:
    if args.recovery:
        recovery = json.loads(Path(args.recovery).read_text(encoding="utf-8"))
        return keyring.key_from(recovery=recovery)
    params = json.loads(Path(args.params).read_text(encoding="utf-8"))
    passphrase = args.passphrase if args.passphrase is not None else getpass.getpass("Passphrase: ")
    return keyring.key_from(passphrase=passphrase, params=params)


def decrypt_tree(source: Path, output: Path, key: bytes) -> tuple[int, list[str]]:
    """Decrypt every .ninaivu file under *source* into *output*. Returns (done, problems)."""
    files = [source] if source.is_file() else sorted(source.rglob("*.ninaivu"))
    wanted_id = keyring.key_id(key)
    done, problems = 0, []
    for encrypted in files:
        relative = Path(encrypted.name) if source.is_file() else encrypted.relative_to(source)
        target = output / relative.with_suffix("")
        if target.exists():
            problems.append(f"{relative}: {target} already exists, left alone")
            continue
        found_id = crypto.read_key_id(encrypted)
        if found_id is None:
            problems.append(f"{relative}: not a Ninaivu encrypted file")
            continue
        if found_id != wanted_id:
            problems.append(f"{relative}: made with a different key ({found_id.hex()})")
            continue
        try:
            crypto.decrypt_file_v2(encrypted, target, key)
            done += 1
        except (OSError, ValueError) as error:
            problems.append(f"{relative}: {error}")
    return done, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--recovery", help="the recovery file downloaded from the console")
    which.add_argument("--params", help="ninaivu-encryption.json from the Drive folder (asks for the passphrase)")
    parser.add_argument("--passphrase", help=argparse.SUPPRESS)   # for tests; prefer the prompt
    parser.add_argument("source", help="a downloaded .ninaivu file, or a folder of them")
    parser.add_argument("output", help="folder to write the decrypted files into")
    args = parser.parse_args(argv)

    source, output = Path(args.source), Path(args.output)
    if not source.exists():
        print(f"Not found: {source}", file=sys.stderr)
        return 2
    try:
        key = _key(args)
    except (OSError, ValueError, KeyError) as error:
        print(f"Could not get the key: {error}", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    done, problems = decrypt_tree(source, output, key)
    print(f"Decrypted {done} file{'s' if done != 1 else ''} into {output}")
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
