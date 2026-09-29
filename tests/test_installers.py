"""The installers' inputs are consistent with the code they package: the
entry points exist, the icons are there, the manifests are well formed.
The builds themselves run on the release workflow's Windows and macOS
runners; this is what can be checked on any machine."""

import configparser
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "installers" / "windows"
MACOS = ROOT / "installers" / "macos"


def _win(path: str) -> Path:
    """A path written for Windows, on whatever this test runs on."""
    return (WINDOWS / path.replace("\\", "/")).resolve()


def _entry_point(spec: str):
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr)


def test_the_windows_installer_points_at_real_entry_points():
    cfg = configparser.ConfigParser()
    cfg.read(WINDOWS / "installer.cfg")
    assert cfg["Application"]["name"] == "Ninaivu"
    assert callable(_entry_point(cfg["Application"]["entry_point"]))
    assert callable(_entry_point(cfg["Command ninaivu"]["entry_point"]))
    assert cfg["Application"]["console"] == "false", "the tray must not open a console window"
    assert _win(cfg["Application"]["icon"]).is_file()
    assert _win(cfg["Application"]["license_file"]).is_file()
    assert cfg["Python"]["version"].startswith("3.12"), "the floor the package declares"
    for line in cfg["Include"]["files"].splitlines():
        source = line.split(">")[0].strip()
        if source:
            assert _win(source).exists(), source
    assert _win(cfg["Build"]["nsi_template"]).is_file()


def test_the_nsis_template_sets_the_root_the_tray_reads():
    text = (WINDOWS / "ninaivu.nsi").read_text(encoding="utf-8")
    assert 'extends "pyapp.nsi"' in text
    assert '"NINAIVU_ROOT" "$INSTDIR"' in text
    assert "ninaivu.desktop.autostart --start" in text, "start at sign-in, the same way the tray does it"
    assert 'DeleteRegValue HKCU "Environment" "NINAIVU_ROOT"' in text, "and the uninstaller undoes it"


def test_the_winget_manifests_agree_with_each_other():
    yaml = pytest.importorskip("yaml")
    docs = {p.name: yaml.safe_load(p.read_text(encoding="utf-8")) for p in (WINDOWS / "winget").glob("*.yaml")}
    assert len(docs) == 3
    kinds = {d["ManifestType"] for d in docs.values()}
    assert kinds == {"version", "installer", "defaultLocale"}
    assert {d["PackageIdentifier"] for d in docs.values()} == {"Ninaivu.Ninaivu"}
    installer = docs["Ninaivu.Ninaivu.installer.yaml"]
    assert installer["Installers"][0]["InstallerSha256"] == "__SHA256__"
    assert "Ninaivu-__VERSION__-windows-x64.exe" in installer["Installers"][0]["InstallerUrl"]
    assert installer["InstallerSwitches"]["Silent"] == "/S", "NSIS's silent switch"


def test_the_build_scripts_fill_in_the_version_from_the_package():
    from ninaivu import __version__
    ps1 = (WINDOWS / "build.ps1").read_text(encoding="utf-8")
    sh = (MACOS / "build.sh").read_text(encoding="utf-8")
    for text in (ps1, sh):
        assert "__version__" in text and "ninaivu/__init__.py" in text
        assert "__VERSION__" in text, "the manifests' placeholder is filled"
    assert re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "ninaivu" / "__init__.py").read_text()).group(1) == __version__


def test_the_mac_app_is_a_menu_bar_app_that_keeps_its_files_in_application_support():
    sh = (MACOS / "build.sh").read_text(encoding="utf-8")
    assert "<key>LSUIElement</key><true/>" in sh, "a tray, not a Dock icon"
    assert 'NINAIVU_ROOT="$HOME/Library/Application Support/Ninaivu"' in sh
    assert "ninaivu.desktop.tray" in sh
    assert "notarytool submit" in sh and "stapler staple" in sh
    assert (MACOS / "entitlements.plist").is_file()
    assert (MACOS / "ninaivu.iconset" / "icon_512x512@2x.png").is_file()


def test_the_cask_has_a_hash_per_architecture():
    rb = (MACOS / "homebrew" / "ninaivu.rb").read_text(encoding="utf-8")
    assert "__SHA256_ARM64__" in rb and "__SHA256_X86_64__" in rb
    assert 'app "Ninaivu.app"' in rb
    assert "local.ninaivu.start" in rb, "the launch agent the tray writes is what uninstall unloads"


def test_the_release_workflow_builds_all_three_and_signs_only_with_secrets():
    yaml = pytest.importorskip("yaml")
    flow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    assert set(flow["jobs"]) == {"windows", "macos", "release"}
    assert flow["jobs"]["release"]["needs"] == ["windows", "macos"]
    archs = [m["arch"] for m in flow["jobs"]["macos"]["strategy"]["matrix"]["include"]]
    assert sorted(archs) == ["arm64", "x86_64"]
    signing = [s for job in ("windows", "macos") for s in flow["jobs"][job]["steps"]
               if "signing certificate" in s.get("name", "")]
    assert len(signing) == 2 and all(s.get("if") for s in signing), "signing is skipped without the secret"
