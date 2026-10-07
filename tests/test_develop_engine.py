"""The editing engines' own tests, run with the rest.

`tests/develop_engine.mjs` (light, tone, colour, curves, detail and looks) and
`tests/studio_engine.mjs` (skin and hair) are Node test files: the engines are
JavaScript, and they are tested where they run. Until this file they ran only
when somebody remembered to, so a change that broke the promise about skin
could pass every check the project actually ran.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("suite", ["develop_engine.mjs", "studio_engine.mjs"])
def test_the_engine_keeps_its_promises(suite):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "--test", str(ROOT / "tests" / suite)],
                          capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]


#: The other Node suites that need no server and no browser: the gallery's
#: actions, the AI playground's commands and recolouring, the slideshow and the
#: timeline's layout. The browser runner takes only `*_ui.mjs`, so until they
#: were named here nothing ran them, and action_logic.mjs had quietly stopped
#: passing as the viewer and app.js grew.
@pytest.mark.parametrize("suite", ["action_logic.mjs", "ai_playground.mjs", "recolor.mjs",
                                   "slideshow.mjs", "layout_pack.mjs"])
def test_the_serverless_front_end_suites_pass(suite):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "--test", str(ROOT / "tests" / suite)],
                          capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
