"""Optional parts install without moving what Ninaivu already runs on, from the
same ranges the requirements files give (audit C-23)."""

from __future__ import annotations

import re
from pathlib import Path

from ninaivu.media import components

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements"


def _key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _split(line: str) -> tuple[str, str]:
    line = line.split(";")[0].split("#")[0].strip()
    name = re.match(r"[A-Za-z0-9_.\-]+", line).group(0)
    spec = ",".join(sorted(part.strip() for part in line[len(name):].split(",") if part.strip()))
    return _key(name), spec


def _files() -> dict[str, set[str]]:
    """Every range the requirements files give each package."""
    ranges: dict[str, set[str]] = {}
    for path in REQUIREMENTS.glob("requirements*.txt"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith(("#", "-")):
                name, spec = _split(line)
                ranges.setdefault(name, set()).add(spec)
    # The Windows build answers to the same import, with the same range.
    for spec in ranges.pop("onnxruntime-directml", set()):
        ranges.setdefault("onnxruntime", set()).add(spec)
    return ranges


def test_every_range_is_the_one_the_requirements_files_give():
    files = _files()
    for component_id, entry in components.CATALOGUE.items():
        if entry["kind"] != "package":
            continue
        asked = [entry.get("requirement")] + [
            word for word in entry.get("also", [])
            if not word.startswith(("-", "http"))]
        for requirement in filter(None, asked):
            name, spec = _split(requirement)
            if name in files:
                assert spec in files[name], (component_id, requirement, files[name])


def test_one_range_for_the_onnx_runtime_everywhere():
    assert len(_files()["onnxruntime"]) == 1


def test_what_is_installed_is_held_and_what_is_asked_for_is_free():
    held = {_key(line.split("==")[0]): line for line in components.held_versions(["Flask"])}
    assert "werkzeug" in held and "==" in held["werkzeug"]
    assert "flask" not in held
    assert "pip" not in held and "setuptools" not in held


def test_an_install_passes_the_held_list(tmp_path, monkeypatch):
    monkeypatch.setattr(components.tempfile, "gettempdir", lambda: str(tmp_path))
    steps = components._hold_installed(components.install_steps("tokenizers"), "tokenizers")
    assert len(steps) == 2
    for step in steps:
        assert step[1:6][:3] == ["-m", "pip", "install"] and step[4] == "-c"
        listed = Path(step[5]).read_text(encoding="utf-8")
        assert "werkzeug==" in listed.lower()
        assert "tokenizers==" not in listed.lower()
        assert "onnxruntime==" not in listed.lower()
    assert steps[-1][-1] == components.CATALOGUE["tokenizers"]["requirement"]


def test_the_page_still_shows_the_plain_command():
    """Somebody running it by hand has no use for a temporary file's name."""
    assert "-c" not in components.describe("llama_cpp")["command"].split()


def test_what_a_step_asks_for():
    assert components._requested(
        ["py", "-m", "pip", "install", "--index-url", "https://x", "torch>=2,<3"]) == ["torch"]
    assert components._requested(
        ["py", "-m", "pip", "install", "--no-deps", "rapidocr>=3,<4"]) == ["rapidocr"]


def test_the_held_list_has_a_name_nobody_can_guess(tmp_path, monkeypatch):
    """D-32: a fixed name in the shared temporary folder could be put there
    first by another account on the computer."""
    monkeypatch.setattr(components.tempfile, "gettempdir", lambda: str(tmp_path))
    first = components._hold_installed(components.install_steps("tokenizers"), "tokenizers")
    second = components._hold_installed(components.install_steps("tokenizers"), "tokenizers")
    assert first[0][5] != second[0][5]
    assert Path(first[0][5]).parent == tmp_path
