"""The eighth audit's D-15: the tested pins must install together.

sympy 1.14 (pulled in by torch) requires mpmath below 1.4. A routine
dependency bump moved mpmath to 1.4.1 twice, which made the AI search
packages impossible to install with the pins; CI now resolves the whole set
(tests.yml, "The pinned packages install together"). This catches the known
pair without a network.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements" / "constraints-tested.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.strip().lower()] = version.strip()
    return pins


def _major_minor(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def test_sympy_and_mpmath_pins_agree():
    pins = _pins()
    if _major_minor(pins["sympy"]) <= (1, 14):
        assert _major_minor(pins["mpmath"]) < (1, 4), pins["mpmath"]


def test_dependabot_holds_mpmath_back():
    text = (ROOT / ".github" / "dependabot.yml").read_text()
    assert "dependency-name: mpmath" in text


def test_ci_resolves_the_pinned_set_with_both_extensions():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    step = text.split("The pinned packages install together", 1)[1].split("- name:", 1)[0]
    assert "--dry-run" in step and "constraints-tested.txt" in step
    assert "./extensions/gemini" in step and "./extensions/creative-studio" in step
