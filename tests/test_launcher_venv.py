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
