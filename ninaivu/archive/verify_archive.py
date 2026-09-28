#!/usr/bin/env python3
"""Verify a Ninaivu archive using only its portable recovery report.

Ninaivu copies this file to the root of every archive it writes, beside
ninaivu-recovery.json, so the archive can be checked on any computer with
Python 3, years from now, without Ninaivu installed:

    python verify-archive.py

It uses only the standard library and must stay that way: it has to keep
working on its own, long after the version of Ninaivu that wrote it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


#: The report Ninaivu writes to the root of an archive.
DEFAULT_REPORT = 'ninaivu-recovery.json'


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify(report_path: Path, archive_root: Path) -> dict:
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if report.get('format') != 'ninaivu-recovery-v1':
        raise ValueError('not a Ninaivu recovery report')
    root = archive_root.resolve()
    result = {'checked': 0, 'matched': 0, 'missing': [], 'changed': [],
              'unsafe': []}
    for item in report.get('files', []):
        relative = Path(str(item.get('path') or ''))
        label = relative.as_posix()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            result['unsafe'].append(label)
            continue
        result['checked'] += 1
        if not candidate.is_file():
            result['missing'].append(label)
        elif file_hash(candidate) != item.get('sha256'):
            result['changed'].append(label)
        else:
            result['matched'] += 1
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Verify an archive without installing or running Ninaivu.')
    parser.add_argument('report', type=Path, nargs='?',
                        help='ninaivu-recovery-*.json (default: ninaivu-recovery.json '
                             'beside this script)')
    parser.add_argument('archive', type=Path, nargs='?',
                        help='archive root (default: folder containing report)')
    args = parser.parse_args(argv)
    if args.report is None:
        # Run from the root of an archive with no arguments: check the report
        # Ninaivu left beside this script.
        args.report = Path(__file__).resolve().parent / DEFAULT_REPORT
    root = args.archive or args.report.resolve().parent
    try:
        result = verify(args.report, root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(f"Checking every file against {args.report.name}. Large archives take a while.")
    print(f"Checked {result['checked']}: {result['matched']} matched, "
          f"{len(result['missing'])} missing, {len(result['changed'])} changed, "
          f"{len(result['unsafe'])} unsafe paths.")
    for kind in ('missing', 'changed', 'unsafe'):
        for path in result[kind]:
            print(f'{kind.upper()}: {path}')
    return 1 if any(result[k] for k in ('missing', 'changed', 'unsafe')) else 0


if __name__ == '__main__':
    raise SystemExit(main())
