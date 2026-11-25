"""tools/release_prep.py: one version everywhere, and the changelog fold."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "release_prep.py"


@pytest.fixture(scope="module")
def prep():
    spec = importlib.util.spec_from_file_location("release_prep", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_place_says_the_one_version(prep):
    assert prep.problems() == []


def test_folding_leaves_the_release_section_newest(prep):
    text = "# Changelog\n\n## Unreleased\n\n- a thing\n\n## 4.4.0 — 15 November 2026\n"
    out = prep.fold(text, "5.0.0", "1 December 2026")
    assert out.index("## 5.0.0 — 1 December 2026") < out.index("- a thing")
    assert "## Unreleased" not in out
    assert out.splitlines()[2].startswith("## 5.0.0")


def test_a_version_already_in_the_changelog_is_refused(prep):
    text = "## Unreleased\n\n## 5.0.0 — 1 December 2026\n"
    with pytest.raises(SystemExit):
        prep.fold(text, "5.0.0", "2 December 2026")


def test_a_malformed_version_is_refused(prep):
    with pytest.raises(SystemExit):
        prep.prepare("5.0", "1 December 2026")
