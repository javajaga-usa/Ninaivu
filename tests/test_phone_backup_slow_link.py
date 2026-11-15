"""Phone backup on a slow, patchy link (static/js/phone-backup.js): pieces
sized to the link, stalled and dropped pieces sent again from the server's
count, one refused file not ending the run, unfinished names remembered."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_the_backup_bends_on_a_slow_link():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for this behavioural test")
    subprocess.run([node, str(Path(__file__).with_name("phone_backup_slow_link.mjs"))],
                   check=True, timeout=60)
