"""The launcher must read the saved resource mode without Flask.

On a first run the launcher has no `.venv` yet, so `start.py` runs under
the system python3, which has no Flask. The launcher read the resource budget
with `from ninaivu.utils.resources import …`, and that runs
`ninaivu/__init__.py`, which imports Flask — so it failed, printed "Could not
apply the saved resource mode", and started the server with neither the mode's
`--workers` nor its thread limits. Later launches ran under `.venv` and never
showed it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "launcher"))     # start.py lives there

import start  # noqa: E402
from ninaivu.utils import resources  # noqa: E402


@pytest.fixture
def no_flask(monkeypatch):
    """The system python3 of a first run: no Flask, and Ninaivu never imported."""
    for name in [n for n in sys.modules if n == "ninaivu" or n.startswith("ninaivu.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "flask", None)


def test_the_ninaivu_package_cannot_be_imported_without_flask(no_flask):
    """What the launcher used to do, so the fixture is known to bite."""
    with pytest.raises(ImportError):
        from ninaivu.utils.resources import budget  # noqa: F401


def test_the_launcher_reads_the_budget_without_flask(no_flask):
    loaded = start.load_resources()
    for mode in resources.MODES:
        assert loaded.budget(mode) == resources.budget(mode)
        assert loaded.environment(mode) == resources.environment(mode)
    assert "ninaivu" not in sys.modules, "the launcher imported the ninaivu package"
