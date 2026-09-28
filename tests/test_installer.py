"""The installer must offer everything requirements.txt promises.

`start.py` carries its own list of packages so it can check them one by one
and keep going when an optional one fails. That list drifted out of step with
requirements.txt: zeroconf, exifread and opencv were added to the file and
never to the installer, so a fresh install had no name on the network, no
second EXIF reader, and — on any machine without ffmpeg, which is every stock
Mac — no video poster frames at all. Nothing said so.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import start  # noqa: E402


def requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[><=!\[]", line)[0].strip().lower())
    return names


def listed_names(specs) -> set[str]:
    return {re.split(r"[><=!\[]", s)[0].strip().lower() for s in specs}


def test_the_installer_covers_every_requirement():
    required = requirement_names(ROOT / "requirements.txt")
    offered = listed_names(start.CORE) | listed_names(start.EXTRAS)
    missing = required - offered
    assert not missing, (
        f"requirements.txt promises {sorted(missing)}, and `python start.py` "
        f"would never install them")


def test_the_installer_does_not_invent_packages():
    required = requirement_names(ROOT / "requirements.txt")
    offered = listed_names(start.CORE) | listed_names(start.EXTRAS)
    assert not offered - required, sorted(offered - required)


@pytest.mark.parametrize("spec", ["zeroconf>=0.130", "exifread>=3.0",
                                  "opencv-python-headless>=4.8,<5"])
def test_the_ones_that_went_missing_are_named_explicitly(spec):
    assert spec in start.EXTRAS


def test_every_package_maps_to_an_importable_module_name():
    """`missing()` decides by import, so a wrong module name reinstalls forever."""
    for spec in start.CORE + start.EXTRAS + start.AI:
        name = re.split(r"[><=!\[]", spec)[0].strip()
        module = start.MODULE_FOR.get(name, name.replace("-", "_"))
        assert module.isidentifier(), f"{name} → {module!r} is not importable"


def test_the_installed_ones_are_actually_detected():
    """The mapping is only right if it says yes for what is really here."""
    here = Path(sys.executable)
    for spec in start.CORE:
        assert not start.missing(here, [spec]), (
            f"{spec} is installed in this environment but reported as missing")
