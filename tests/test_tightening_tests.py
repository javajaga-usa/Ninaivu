"""The test runs and the pinned versions say what they claim to.

Each check here guards a list somebody keeps by hand, which had drifted from
the thing it describes: the files the extension step runs, the ruff the lint
gate installs, the versions the installers ship, and which .mjs files are
browser tests.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
REQUIREMENTS = ROOT / "requirements"
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")


def _name(requirement: str) -> str:
    """A requirement's project name, compared as pip compares them."""
    return re.sub(r"[-_.]+", "-", re.match(r"\s*([A-Za-z0-9_.-]+)", requirement).group(1)).lower()


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[_name(name)] = version.strip()
    return pins


def _requirements(path: Path) -> list[str]:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.append(_name(line))
    return names


def _job(name: str) -> str:
    """The text of one job in tests.yml, up to the next job."""
    match = re.search(rf"\n  {name}:\n(.*?)(?=\n  [a-z-]+:\n|\Z)", WORKFLOW, re.S)
    assert match, f"no {name} job in tests.yml"
    return match.group(1)


def test_every_test_that_needs_an_extension_runs_in_ci():
    """A file that skips itself without Gemini or the creative studio runs in
    CI only if the extension step names it. Two audit files were left off
    that hand-written list, so their tests had never run anywhere."""
    step = re.search(r"name: Run the extension tests\n\s+run: (.+)", WORKFLOW)
    assert step, "the extension step is gone from tests.yml"
    named = set(re.findall(r"tests/(test_\w+\.py)", step.group(1)))
    needs = {p.name for p in TESTS.glob("test_*.py")
             if re.search(r"""importorskip\(["']ninaivu_(gemini|studio)""",
                          p.read_text(encoding="utf-8"))}
    assert needs, "no test asks for an extension: the check above has nothing to find"
    assert needs - named == set(), f"skipped everywhere in CI: {sorted(needs - named)}"


def test_the_lint_gate_runs_the_ruff_developers_run():
    dev = _pins(REQUIREMENTS / "requirements-dev.txt")["ruff"]
    tested = _pins(REQUIREMENTS / "constraints-tested.txt")["ruff"]
    assert dev == tested
    lint = _job("lint")
    inline = re.findall(r"ruff==([\d.]+)", lint)
    assert inline in ([], [dev]), f"the lint job pins ruff {inline}, developers run {dev}"
    if not inline:
        assert "-c requirements/constraints-tested.txt" in lint


@pytest.mark.parametrize("listing", ["requirements.txt", "requirements-desktop.txt"])
def test_everything_the_installers_ship_has_a_tested_version(listing):
    """The installers install these with constraints-tested.txt; a name it
    does not pin is whatever the index serves on the day of the build."""
    pinned = _pins(REQUIREMENTS / "constraints-tested.txt")
    loose = [n for n in _requirements(REQUIREMENTS / listing) if n not in pinned]
    assert loose == [], f"{listing}: no tested version for {loose}"


def test_the_tray_brings_its_own_pinned_dependencies():
    pinned = _pins(REQUIREMENTS / "constraints-tested.txt")
    for name in ("pystray", "six", "python-xlib", "pyobjc-framework-quartz"):
        assert name in pinned, name


@pytest.mark.parametrize("job", ["test", "basic-tier", "browser"])
def test_the_jobs_on_the_shipped_python_install_the_shipped_versions(job):
    assert "requirements/constraints-tested.txt" in _job(job), job


def test_the_local_runner_takes_the_same_browser_tests_as_ci():
    def load(path: Path, name: str):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    local = load(ROOT / "tools" / "run_tests.py", "_run_tests_under_test")
    ci = load(TESTS / "run_browser_tests.py", "_run_browser_tests_under_test")
    assert local.browser_suites(None) == ci.every_browser_test()
    assert not any(p.name in ("harness.mjs", "perf.mjs", "synthetic_portrait.mjs")
                   for p in local.browser_suites(None))


def test_no_fixture_script_kills_every_running_ninaivu():
    """tests/archive_ui.sh and selection_ui.sh began with `pkill -9` on any
    `python3 -m ninaivu`, a household's own server included. They were
    replaced by run_browser_tests.py and are gone; nothing may bring the
    habit back."""
    for script in TESTS.glob("*.sh"):
        assert "pkill" not in script.read_text(encoding="utf-8"), script.name
