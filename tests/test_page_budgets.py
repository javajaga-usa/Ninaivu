"""Speed budgets: every page of both apps answers within a time, on a made-up library.

Runs ``tools/bench_app.py`` on 20,000 rows (about fifteen seconds) and reads
its table. A page that grew from milliseconds to a second shows up here, by
name, in the pull request that did it, rather than on the Mac a month later.

The budgets are loose on purpose, about five times what a slow runner shows
today, so only a real regression fails them; tighten them as the pages get
faster. First answer: the first time a page is asked, with nothing cached.
Best answer: the quickest of the repeats.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
#: Milliseconds.
FIRST_BUDGET = 3500.0
BEST_BUDGET = 400.0
ROWS = 20_000
_ROW = re.compile(r"^\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+(family app, admin|"
                  r"family app, family member|console)\s+(\S+)")


@pytest.fixture(scope="module")
def table():
    env = {**os.environ, "BENCH_REPEAT": "2"}
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "bench_app.py"), str(ROWS)],
                          capture_output=True, text=True, env=env, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    rows = [m.groups() for m in map(_ROW.match, done.stdout.splitlines()) if m]
    assert len(rows) > 150, done.stdout[-1500:]
    return rows


def test_no_page_is_slow_the_first_time(table):
    slow = [(float(first), route) for first, _, _, _, _, _, route in table
            if float(first) > FIRST_BUDGET]
    assert not slow, f"over {FIRST_BUDGET:.0f} ms the first time: {slow}"


def test_no_page_is_slow_when_asked_again(table):
    slow = [(float(best), route) for _, best, _, _, _, _, route in table
            if float(best) > BEST_BUDGET]
    assert not slow, f"over {BEST_BUDGET:.0f} ms when warm: {slow}"


def test_no_page_fails_with_a_server_error(table):
    failed = [(status, route) for _, _, _, status, _, _, route in table if status == "500"]
    assert not failed, failed
