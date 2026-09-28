"""Verify the built wheel includes web assets and excludes local runtime data."""
import argparse
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', nargs='?', type=Path)
    args = parser.parse_args()
    wheels = sorted((ROOT / 'dist').glob('*.whl'), key=lambda p: p.stat().st_mtime)
    path = args.wheel or (wheels[-1] if wheels else None)
    if path is None:
        parser.error('Build a wheel first: python -m build --wheel')
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        expected = {'ninaivu/__init__.py', 'ninaivu/__main__.py'}
        for folder in ('static', 'templates'):
            expected.update(p.relative_to(ROOT).as_posix() for p in (ROOT / 'ninaivu' / folder).rglob('*') if p.is_file())
        missing = expected - names
        unwanted = {name for name in names if name.endswith(('.db', '.log', '.key', '.pyc'))
                    or any(part in name.split('/') for part in ('.ai-models', '.ninaivu-control', '__pycache__', 'tests', 'docs'))}
        if missing or unwanted:
            raise SystemExit(f'Invalid distribution. Missing: {sorted(missing)}; unwanted: {sorted(unwanted)}')
        metadata = next(name for name in names if name.endswith('.dist-info/METADATA'))
        if 'requires-dist: flask' not in archive.read(metadata).decode().lower():
            raise SystemExit('Core Flask dependency is missing from package metadata.')
    print(f'Distribution verified: {path.name}; {len(expected)} required application files.')


if __name__ == '__main__':
    main()
