"""The retouching engine's arithmetic, run under Node.

``tests/studio_engine.mjs`` holds the tests; they need no browser, no detector and
no server, so this only has to run them and show what they say when one fails. The
promises they keep are the ones a person would hold the tools to: only the face
that was asked for, a bindi untouched to the last bit, nothing that lightens skin
but light on a face in shadow.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_the_retouching_engine_keeps_its_promises():
    done = subprocess.run([NODE, "--test", str(ROOT / "tests" / "studio_engine.mjs")],
                          capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert done.returncode == 0, done.stdout[-6000:] + done.stderr[-2000:]
