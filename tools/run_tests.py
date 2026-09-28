#!/usr/bin/env python3
"""Run Ninaivu's tests — both kinds, one command.

There are two suites and they want different things. The Python tests run
anywhere and need nothing: they build a library in a temporary folder and take
about six minutes. The browser tests drive the real thing, so they want an
instance already running on this machine, and they are skipped rather than
failed when there is not one.

    python tools/run_tests.py                # everything that can run here
    python tools/run_tests.py --python       # the pytest suite alone
    python tools/run_tests.py --browser      # the browser suites alone
    python tools/run_tests.py -k straighten  # whatever matches, either kind

The addresses come from the environment, so this works against a Ninaivu on
another port or another machine:

    NINAIVU_HOME=http://127.0.0.1:5000  NINAIVU_ADMIN=http://127.0.0.1:3000
    NINAIVU_USER=dad  NINAIVU_PASS=...
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

HOME = os.environ.get("NINAIVU_HOME", "http://127.0.0.1:5000")
ADMIN = os.environ.get("NINAIVU_ADMIN", "http://127.0.0.1:3000")

#: Suites that want something this script cannot arrange — a phone plugged in,
#: a drive to fill. Named rather than guessed at, so the list is honest.
NEEDS_A_HUMAN = {"perf.mjs"}


def paint(text: str, code: str) -> str:
    return text if os.environ.get("NO_COLOR") else f"\033[{code}m{text}\033[0m"


def answering(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code in (401, 403)          # up, and asking who we are
    except Exception:
        return False


def run_python(pattern: str | None, extra: list[str]) -> int:
    print(paint("\n  Python suite", "1;36"))
    command = [sys.executable, "-m", "pytest", "tests", "-q"]
    if pattern:
        command += ["-k", pattern]
    command += extra
    return subprocess.call(command, cwd=ROOT)


def browser_suites(pattern: str | None) -> list[Path]:
    found = sorted(p for p in TESTS.glob("*.mjs")
                   if p.name != "harness.mjs" and p.name not in NEEDS_A_HUMAN)
    if pattern:
        found = [p for p in found if pattern in p.name]
    return found


def run_browser(pattern: str | None) -> int:
    node = shutil.which("node")
    if not node:
        print(paint("  Node is not installed — skipping the browser suites", "33"))
        return 0

    suites = browser_suites(pattern)
    if not suites:
        return 0

    if not (answering(HOME) and answering(ADMIN)):
        print(paint(f"\n  No Ninaivu answering on {HOME} and {ADMIN} —"
                    " skipping the browser suites", "33"))
        print("  Start one first, or point NINAIVU_HOME / NINAIVU_ADMIN at it.")
        return 0

    print(paint(f"\n  Browser suites ({len(suites)})", "1;36"))
    failures = []
    for suite in suites:
        started = time.time()
        print(f"\n  {paint('▸', '36')} {suite.name}")
        code = subprocess.call([node, str(suite)], cwd=ROOT)
        took = time.time() - started
        if code != 0:
            failures.append(suite.name)
            print(paint(f"    failed in {took:.0f}s", "31"))
        else:
            print(paint(f"    passed in {took:.0f}s", "32"))

    if failures:
        print(paint(f"\n  {len(failures)} browser suite(s) failed: "
                    + ", ".join(failures), "31"))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Ninaivu's tests")
    parser.add_argument("--python", action="store_true", help="the pytest suite alone")
    parser.add_argument("--browser", action="store_true", help="the browser suites alone")
    parser.add_argument("-k", dest="pattern", help="only what matches this")
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="anything else is passed on to pytest")
    args = parser.parse_args()

    both = not (args.python or args.browser)
    failed = 0

    if args.python or both:
        failed |= 1 if run_python(args.pattern, args.rest) else 0
    if args.browser or both:
        failed |= 1 if run_browser(args.pattern) else 0

    print(paint("\n  Everything passed\n" if not failed else "\n  Something failed\n",
                "32" if not failed else "31"))
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
