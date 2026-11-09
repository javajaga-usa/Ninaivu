"""Run the test suite in parts, one fresh Python process each, one after another.

    python tools/run_in_parts.py 4 -q -m "not tier_full"

The Basic-tier job holds the suite to 2 GB of memory, as the small machine it
stands in for is held. That limit is meant for Ninaivu: what one test needs.
Run as one process, the suite also carries everything the thousands of tests
before it left behind, and by the end that alone reaches the limit (main's
run peaked at 2 GB and lived; one more slice of tests and the kernel ended
it). In parts, each test still runs under the same 2 GB, and nothing one part
left behind is carried into the next.

The test files are split in order into *parts* runs of about the same number
of files. The exit status is the first failing part's (pytest's 5, "no tests
ran", counts as passing: a part can be all deselected).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parts_of(files: list[str], parts: int) -> list[list[str]]:
    """*files* in order, cut into *parts* runs of near-equal length."""
    parts = max(1, min(parts, len(files) or 1))
    size, extra = divmod(len(files), parts)
    out, at = [], 0
    for i in range(parts):
        take = size + (1 if i < extra else 0)
        out.append(files[at:at + take])
        at += take
    return [p for p in out if p]


def main(argv: list[str]) -> int:
    if not argv or not argv[0].isdigit():
        print(__doc__)
        return 2
    parts, pytest_args = int(argv[0]), argv[1:]
    files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("test_*.py"))
    status = 0
    chunks = parts_of(files, parts)
    for number, chunk in enumerate(chunks, 1):
        print(f"\n=== part {number} of {len(chunks)}: {chunk[0]} … {chunk[-1]} ({len(chunk)} files)",
              flush=True)
        code = subprocess.call([sys.executable, "-m", "pytest", *pytest_args, *chunk], cwd=ROOT)
        if code not in (0, 5) and not status:
            status = code
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
