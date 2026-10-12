"""Behavioral checks for the adaptive gallery preload policy."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_adaptive_preload_policy():
    done = subprocess.run(
        [NODE, str(ROOT / "tests" / "adaptive_preload.mjs")],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr
