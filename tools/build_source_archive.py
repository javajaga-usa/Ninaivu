"""Build a source ZIP from a committed Git revision, never from local model/state folders."""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build_archive(root, revision='HEAD', destination=None):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
    commit = git('rev-parse', '--verify', '--end-of-options', revision + '^{commit}')
    config = git('show', f'{commit}:pyproject.toml')
    match = re.search(r'^version\s*=\s*"([0-9A-Za-z.+-]+)"', config, re.MULTILINE)
    if not match:
        raise ValueError('The committed package version could not be read.')
    version = match.group(1)
    destination = destination or root / 'dist' / f'Ninaivu-{version}-{commit[:7]}-source.zip'
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'archive', '--format=zip', f'--prefix=Ninaivu-{version}/',
                    f'--output={destination.resolve()}', commit], cwd=root, check=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', default='HEAD', help='Committed branch, tag or commit (default HEAD)')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print(build_archive(ROOT, args.ref, args.output))


if __name__ == '__main__':
    main()
