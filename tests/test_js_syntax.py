"""Every shipped script parses.

The browser is the only thing that runs Ninaivu's JavaScript, and a syntax
error in one module takes down every page that imports it — the whole gallery,
or the whole console — with nothing but a line in the browser's console to say
why. Python's own tests never load these files, so this asks Node to parse each
one. Parsing only: nothing is executed.

Skipped where Node is not installed; the CI runners have it.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "ninaivu" / "static"
SCRIPTS = sorted(p for p in STATIC.rglob("*") if p.suffix in (".js", ".mjs"))
NODE = shutil.which("node")
_MODULE = re.compile(r"^\s*(import|export)\s", re.MULTILINE)


def test_there_are_scripts_to_check():
    assert len(SCRIPTS) >= 30, [p.name for p in SCRIPTS]


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.relative_to(STATIC).as_posix())
def test_the_script_parses(script, tmp_path):
    source = script.read_text(encoding="utf-8")
    # `node --check` reads a .js file as CommonJS, where `import` is a syntax
    # error. The extension is what tells it otherwise, so check a copy.
    suffix = ".mjs" if script.suffix == ".mjs" or _MODULE.search(source) else ".cjs"
    copy = tmp_path / f"{script.stem}{suffix}"
    copy.write_text(source, encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(copy)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"{script.relative_to(STATIC)}:\n{result.stderr}"
