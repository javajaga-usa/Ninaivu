"""A full export from the Mac app ran out of open files.

A Mac gives an app opened from Finder 256 open files. Ninaivu's own threads
keep the index open, and a full export of 961,507 files on top failed
"Too many open files" 700,000 files in (the console's pages failing
alongside it). Ninaivu now asks for more when it starts, and the export
itself holds only a handful at a time.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ninaivu.utils import files

resource = pytest.importorskip("resource")

ROOT = Path(__file__).resolve().parents[1]


def _in_a_process(code: str) -> str:
    """Run *code* in a Python of its own, so limits set there stay there."""
    done = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_the_limit_is_raised_from_what_a_mac_gives_an_app():
    out = _in_a_process("""
        import resource
        from ninaivu.utils.files import open_file_limit
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        print(open_file_limit(1024), resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    """)
    assert out == "(256, 1024) 1024"


def test_the_limit_stops_at_the_hard_limit():
    out = _in_a_process("""
        import resource
        from ninaivu.utils.files import open_file_limit
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 512))
        print(open_file_limit(10240), resource.getrlimit(resource.RLIMIT_NOFILE))
    """)
    assert out == "(64, 512) (512, 512)"


def test_the_limit_is_never_lowered():
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert files.open_file_limit(8) == (soft, soft)
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == soft


def test_a_refused_limit_is_asked_again_for_less(monkeypatch):
    soft, hard = 256, resource.RLIM_INFINITY
    asked = []

    def setrlimit(which, limits):
        asked.append(limits[0])
        if limits[0] > 3000:            # a Mac's kern.maxfilesperproc
            raise ValueError("not allowed")

    monkeypatch.setattr(resource, "getrlimit", lambda which: (soft, hard))
    monkeypatch.setattr(resource, "setrlimit", setrlimit)
    assert files.open_file_limit(10240) == (256, 2560)
    assert asked == [10240, 5120, 2560]


def test_a_full_export_needs_only_a_few_open_files(tmp_path):
    # 12,000 thumbnails and face crops through an export allowed 48 open
    # files in all: the export holds a handful at a time, and none after.
    out = _in_a_process(f"""
        import os, resource, sqlite3
        from pathlib import Path
        from ninaivu.storage import backup
        state, out = Path({str(tmp_path / 'state')!r}), Path({str(tmp_path / 'out')!r})
        state.mkdir(); out.mkdir()
        for name in ("index.db", "archive.db"):
            conn = sqlite3.connect(state / name)
            conn.execute("CREATE TABLE t (x)")
            conn.commit()
            conn.close()
        (state / "config.json").write_text("{{}}")
        for extra in backup.FULL_EXTRAS:
            folder = state / extra / "a"
            folder.mkdir(parents=True)
            for i in range(1500):
                (folder / f"{{i}}.jpg").write_bytes(os.urandom(64))
        resource.setrlimit(resource.RLIMIT_NOFILE,
                           (48, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
        before = len(os.listdir("/dev/fd"))
        made = backup.snapshot(state, out, full=True)
        print(made is not None, len(os.listdir("/dev/fd")) - before)
    """)
    assert out == "True 0"
