"""Get the repository ready for a release, or check that it is.

    python tools/release_prep.py 5.0.0            # bump, and fold Unreleased
    python tools/release_prep.py 5.0.0 --date "24 November 2026"
    python tools/release_prep.py --check          # every place says one version

The version lives in ``ninaivu/__init__.py``; the places that repeat it are
the two extensions, the README's status line and its .deb example. Folding
turns ``## Unreleased`` in docs/CHANGELOG.md into ``## <version> — <date>``
and leaves a new, empty ``## Unreleased`` above it. Nothing is committed, and
nothing is tagged or published: the release workflow does that on a green
main, and a person starts it.
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "ninaivu" / "__init__.py"
README = ROOT / "README.md"
CHANGELOG = ROOT / "docs" / "CHANGELOG.md"
EXTENSIONS = sorted((ROOT / "extensions").glob("*/pyproject.toml"))

VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_INIT = re.compile(r'^(__version__\s*=\s*")([^"]+)(")', re.M)
_EXT = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.M)
_STATUS = re.compile(r"(\*\*Status: )(\d+\.\d+\.\d+)(\.\*\*)")
_DEB = re.compile(r"(Ninaivu-)(\d+\.\d+\.\d+)(-linux-x64\.deb)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def versions() -> dict[str, list[str]]:
    """Every version the repository states, by where it is stated."""
    found: dict[str, list[str]] = {}
    found["ninaivu/__init__.py"] = [m.group(2) for m in _INIT.finditer(_read(INIT))]
    for path in EXTENSIONS:
        found[str(path.relative_to(ROOT))] = [m.group(2) for m in _EXT.finditer(_read(path))][:1]
    text = _read(README)
    found["README.md status"] = [m.group(2) for m in _STATUS.finditer(text)]
    found["README.md .deb"] = [m.group(2) for m in _DEB.finditer(text)]
    return found


def problems() -> list[str]:
    """What stops the repository saying one version, in plain words."""
    found = versions()
    current = (found["ninaivu/__init__.py"] or ["?"])[0]
    bad = []
    for where, values in found.items():
        if not values:
            bad.append(f"{where}: no version found")
        elif any(v != current for v in values):
            bad.append(f"{where}: says {', '.join(values)}, ninaivu/__init__.py says {current}")
    return bad


def fold(changelog: str, version: str, date: str) -> str:
    """``## Unreleased`` becomes the release's section, and a new one is opened."""
    marker = "## Unreleased\n"
    if marker not in changelog:
        raise SystemExit("docs/CHANGELOG.md has no '## Unreleased' section to fold")
    if re.search(rf"^## {re.escape(version)} ", changelog, re.M):
        raise SystemExit(f"docs/CHANGELOG.md already has a section for {version}")
    return changelog.replace(marker, f"## Unreleased\n\n## {version} — {date}\n", 1)


def prepare(version: str, date: str) -> None:
    if not VERSION.match(version):
        raise SystemExit(f"{version!r} is not a version like 5.0.0")
    INIT.write_text(_INIT.sub(rf"\g<1>{version}\g<3>", _read(INIT), count=1), encoding="utf-8")
    for path in EXTENSIONS:
        path.write_text(_EXT.sub(rf"\g<1>{version}\g<3>", _read(path), count=1), encoding="utf-8")
    text = _STATUS.sub(rf"\g<1>{version}\g<3>", _read(README))
    README.write_text(_DEB.sub(rf"\g<1>{version}\g<3>", text), encoding="utf-8")
    CHANGELOG.write_text(fold(_read(CHANGELOG), version, date), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("version", nargs="?")
    parser.add_argument("--date", help='for example "24 November 2026" (default: today)')
    parser.add_argument("--check", action="store_true", help="only say whether the files agree")
    args = parser.parse_args(argv)
    if args.check or not args.version:
        bad = problems()
        for line in bad:
            print(line, file=sys.stderr)
        print("every place says one version" if not bad else "the versions disagree")
        return 1 if bad else 0
    today = datetime.date.today()
    date = args.date or f"{today.day} {today.strftime('%B %Y')}"
    prepare(args.version, date)
    bad = problems()
    for line in bad:
        print(line, file=sys.stderr)
    print(f"prepared {args.version} ({date}); review `git diff`, then run the full suite")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
