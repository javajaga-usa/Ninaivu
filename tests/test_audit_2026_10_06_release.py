"""Audit 2026-10-06: installers, launcher, tooling and the release workflows.

The Linux installer is run in tests/test_linux_installer_runs.py and the Mac
launchers in tests/test_mac_launcher.py; these cover the rest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ninaivu.media import model_catalog

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def leave_the_catalogue_as_it_was():
    before = model_catalog._root
    yield
    model_catalog.configure(before)


# -- A-23: an upgrade deleted every downloaded AI model ----------------------------------

@pytest.mark.parametrize("folder", [
    "/opt/ninaivu/python/lib/python3.12/site-packages",
    "/usr/lib/python3/dist-packages",
    r"C:\Users\amma\AppData\Local\Programs\Ninaivu\pkgs",
    "/Applications/Ninaivu.app/Contents/Resources/python/lib/python3.12/site-packages",
])
def test_an_installed_copy_is_told_apart_from_a_checkout(folder):
    path = Path(folder.replace("\\", "/"))
    assert model_catalog.is_installed_copy(path)


def test_a_checkout_is_not_an_installed_copy():
    assert not model_catalog.is_installed_copy(ROOT)
    assert not model_catalog.is_installed_copy(Path("/home/amma/src/ninaivu"))


def test_a_checkout_keeps_its_models_beside_it(monkeypatch):
    monkeypatch.delenv("NINAIVU_AI_MODELS_DIR", raising=False)
    model_catalog.configure(None)
    assert model_catalog.models_root() == ROOT / ".ai-models"


def test_an_installed_copy_keeps_its_models_in_a_folder_of_the_persons_own(tmp_path, monkeypatch):
    """Two levels up from the package is site-packages, pkgs or the inside of
    the Mac app in an installed copy, and every upgrade replaces that whole."""
    monkeypatch.delenv("NINAIVU_AI_MODELS_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(model_catalog, "is_installed_copy", lambda folder: True)
    model_catalog.configure(None)
    root = model_catalog.models_root()
    if sys.platform.startswith("linux"):
        assert root == tmp_path / "state" / "ninaivu" / "ai-models"
    assert "site-packages" not in root.parts and ".ai-models" not in root.parts


def test_the_setting_and_the_environment_still_win_over_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "is_installed_copy", lambda folder: True)
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(tmp_path / "env"))
    model_catalog.configure(None)
    assert model_catalog.models_root() == tmp_path / "env"
    model_catalog.configure(tmp_path / "chosen")
    assert model_catalog.models_root() == tmp_path / "chosen"


def test_the_per_user_folder_on_each_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert model_catalog.user_models_dir("win32") == tmp_path / "Local" / "Ninaivu" / "ai-models"
    assert model_catalog.user_models_dir("darwin") == (
        Path.home() / "Library" / "Application Support" / "Ninaivu" / "ai-models")
    assert model_catalog.user_models_dir("linux") == tmp_path / "xdg" / "ninaivu" / "ai-models"


def test_the_windows_installer_moves_the_models_before_removing_pkgs():
    nsi = (ROOT / "installers" / "windows" / "ninaivu.nsi").read_text(encoding="ascii")
    moved = nsi.index(r'Rename "$INSTDIR\pkgs\.ai-models"')
    assert moved < nsi.index(r'RMDir /r "$INSTDIR\pkgs"')
    assert r"\Ninaivu\ai-models" in nsi and "ReadEnvStr $1 LOCALAPPDATA" in nsi
    assert "Abort" in nsi[moved:nsi.index(r'RMDir /r "$INSTDIR\pkgs"')], \
        "models that cannot be moved stop the upgrade before anything is removed"


# -- A-54: macOS hardening ------------------------------------------------------------------

def test_the_mac_app_does_not_allow_dyld_injection():
    import plistlib
    entitlements = plistlib.loads((ROOT / "installers" / "macos" / "entitlements.plist").read_bytes())
    assert "com.apple.security.cs.allow-dyld-environment-variables" not in entitlements
    assert entitlements["com.apple.security.cs.disable-library-validation"] is True
