"""The launcher says why it cannot make its .venv, instead of limping on.

On MX Linux a checkout on the live USB's storage partition (FAT or exFAT)
could not take the lib64 -> lib link venv makes on 64-bit Linux. The launcher
fell back to the system python3, which on Debian has no pip, and ended on
"Ninaivu needs Flask, Pillow and NumPy to run" with the cause scrolled away.
"""

import errno
import os
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))     # start.py lives there

import start  # noqa: E402

posix_only = pytest.mark.skipif(os.name == "nt", reason="venv makes no links on Windows")


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """A checkout with no .venv yet, run by a python3 that is not in one."""
    monkeypatch.setattr(start, "VENV_DIR", tmp_path / ".venv")
    monkeypatch.setattr(start, "HERE", tmp_path)
    monkeypatch.setattr(start, "in_virtualenv", lambda: False)
    monkeypatch.delenv("NINAIVU_NO_VENV", raising=False)
    return tmp_path


@posix_only
def test_a_drive_without_links_names_the_fix(fresh, monkeypatch, capsys):
    def refuse(self, env_dir):
        Path(env_dir, "lib").mkdir(parents=True)
        raise PermissionError(errno.EPERM, "Operation not permitted", "lib")
    monkeypatch.setattr(venv.EnvBuilder, "create", refuse)

    with pytest.raises(SystemExit):
        start.ensure_venv()
    assert "~/Ninaivu" in capsys.readouterr().err
    assert not (fresh / ".venv").exists(), "the half-made .venv was left behind"


def test_a_python_without_venv_support_names_the_package(fresh, monkeypatch, capsys):
    real = start.importlib.util.find_spec
    monkeypatch.setattr(start.importlib.util, "find_spec",
                        lambda name, *a: None if name == "ensurepip" else real(name, *a))

    with pytest.raises(SystemExit):
        start.ensure_venv()
    assert "sudo apt install python3-venv" in capsys.readouterr().err


def test_other_failures_still_fall_back(fresh, monkeypatch):
    def broken(self, env_dir):
        raise RuntimeError("something else")
    monkeypatch.setattr(venv.EnvBuilder, "create", broken)

    assert start.ensure_venv() == Path(sys.executable)


def _fake_venv(root, script):
    """A .venv whose python is a script, so it can claim any version."""
    python = start.venv_python(root / ".venv")
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n" + script)
    python.chmod(0o755)
    (root / ".venv" / "lib").mkdir()
    (root / ".venv" / "lib" / "old-package").write_text("x")
    return python


@posix_only
def test_a_venv_from_an_older_python_is_made_again(fresh, monkeypatch, capsys):
    """4.1 needs Python 3.14. A checkout updated from 4.0 still has the .venv
    its Python 3.12 made, and the launcher went on to run Ninaivu in it."""
    _fake_venv(fresh, "echo 1\n")          # answers the question "too old?" with yes
    made = []

    def create(self, env_dir):
        made.append(Path(env_dir))
        python = start.venv_python(Path(env_dir))
        python.parent.mkdir(parents=True)
        python.write_text("")
    monkeypatch.setattr(venv.EnvBuilder, "create", create)

    assert start.ensure_venv() == start.venv_python(fresh / ".venv")
    assert made == [fresh / ".venv"]
    assert not (fresh / ".venv" / "lib" / "old-package").exists()
    assert "older" in capsys.readouterr().out


@posix_only
@pytest.mark.parametrize("script", ["echo 0\n", "exit 3\n"])
def test_a_venv_that_is_new_enough_or_broken_is_left_alone(fresh, monkeypatch, script):
    python = _fake_venv(fresh, script)
    monkeypatch.setattr(venv.EnvBuilder, "create",
                        lambda self, env_dir: pytest.fail("the .venv was made again"))

    assert start.ensure_venv() == python
    assert (fresh / ".venv" / "lib" / "old-package").exists()


def test_the_floor_is_the_same_everywhere():
    """One number, written in six places that cannot import each other."""
    import re
    floor = start.MIN_PYTHON
    assert floor == (3, 14)
    want = f">={floor[0]}.{floor[1]}"
    for path in ("pyproject.toml", "extensions/gemini/pyproject.toml",
                 "extensions/creative-studio/pyproject.toml"):
        assert f'requires-python = "{want}"' in (ROOT / path).read_text(), path
    for path in ("launcher/install-python-mac.sh", "launcher/start.cmd", "launcher/Ninaivu.command"):
        found = re.findall(r"version_info >= \((\d+), (\d+)\)", (ROOT / path).read_text())
        assert found and all((int(a), int(b)) == floor for a, b in found), path
    docker = (ROOT / "installers" / "docker" / "Dockerfile").read_text()
    images = set(re.findall(r"^FROM python:(\d+)\.(\d+)-slim@", docker, re.M))
    assert images == {(str(floor[0]), str(floor[1]))}, "the Docker image runs another Python"
