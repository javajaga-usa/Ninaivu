"""The launcher must read the saved resource mode without Flask.

On a first run the launcher has no `.venv` yet, so `start.py` runs under
the system python3, which has no Flask. The launcher read the resource budget
with `from ninaivu.utils.resources import …`, and that runs
`ninaivu/__init__.py`, which imports Flask — so it failed, printed "Could not
apply the saved resource mode", and started the server with neither the mode's
`--workers` nor its thread limits. Later launches ran under `.venv` and never
showed it.
"""

import json
import subprocess
import sys
from pathlib import Path

from ninaivu.utils import resources

ROOT = Path(__file__).resolve().parents[1]


def without_flask(code):
    """Use a fresh interpreter: evicting Ninaivu from pytest's sys.modules
    can leave cached submodules attached to a different package instance.
    That broke later string-based monkeypatches on Windows.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['flask'] = None; " + code],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_ninaivu_package_cannot_be_imported_without_flask():
    """What the launcher used to do, so the simulation is known to bite."""
    without_flask('''
try:
    from ninaivu.utils.resources import budget
except ImportError:
    pass
else:
    raise AssertionError("Ninaivu imported without Flask")
''')


def test_the_launcher_reads_the_budget_without_flask():
    output = without_flask('''
import json
sys.path.insert(0, 'launcher')
import start
loaded = start.load_resources()
assert "ninaivu" not in sys.modules, "the launcher imported the ninaivu package"
print(json.dumps({mode: [loaded.budget(mode), loaded.environment(mode)]
                  for mode in loaded.MODES}))
''')
    expected = {mode: [resources.budget(mode), resources.environment(mode)]
                for mode in resources.MODES}
    assert json.loads(output) == json.loads(json.dumps(expected))
