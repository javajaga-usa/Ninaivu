#!/usr/bin/env python3
"""Preview or remove untracked project caches; never delete source or user data."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CACHE_NAMES = {'__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache'}
BUILD_NAMES = {'build', 'dist'}
SKIP_NAMES = {'.git', '.venv', 'venv', 'node_modules', '.ai-models', '.ninaivu-control', '.test-deps'}


def is_link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def inside(root, path):
    try:
        path.resolve().relative_to(root.resolve())
        return path.resolve() != root.resolve()
    except (ValueError, OSError):
        return False


def find_cache_dirs(root, include_build=False):
    result = []
    for parent, directories, _ in os.walk(root, followlinks=False):
        for name in list(directories):
            path = Path(parent) / name
            if name in SKIP_NAMES or is_link(path) or not inside(root, path):
                directories.remove(name)
            elif name in CACHE_NAMES or (include_build and (name in BUILD_NAMES or name.endswith('.egg-info'))):
                result.append(path)
                directories.remove(name)
    return sorted(result)


def clean(root, apply=False, include_build=False):
    root = root.resolve()
    # Fail closed: an unreadable Git index is not permission to delete files.
    output = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True).stdout
    tracked = set(output.decode('utf-8').split('\0'))
    removed = 0
    for path in find_cache_dirs(root, include_build=include_build):
        relative = path.relative_to(root).as_posix()
        if any(item == relative or item.startswith(relative + '/') for item in tracked):
            print(f'Keeping tracked cache: {relative}')
            continue
        # Resolve again immediately before deleting, including Windows junctions.
        if not inside(root, path) or is_link(path):
            raise RuntimeError(f'Cache target changed or leaves the checkout: {path}')
        print(f'{"Removing" if apply else "Would remove"}: {relative}')
        if apply:
            shutil.rmtree(path)
        removed += 1
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Remove the listed untracked caches')
    parser.add_argument('--build', action='store_true', help='Include build, dist, and egg-info directories')
    parser.add_argument('--all', action='store_true', help='Include both caches and build/packaging outputs')
    args = parser.parse_args()
    include_build = args.build or args.all
    count = clean(ROOT, args.apply, include_build=include_build)
    kind = 'target' if include_build else 'cache'
    print(f'{count} {kind} directories. ' + ('Cleanup complete.' if args.apply else 'Dry run; nothing changed.'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
