"""The portable builds' inputs are consistent with the code they package: the
entry points exist, the icons are there, the launchers set the folders.
The builds themselves run on the release workflow's Windows, macOS and Linux
runners; this is what can be checked on any machine."""

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


def test_the_windows_build_carries_the_same_python_as_the_others_with_pip_and_tk():
    """The embeddable Python the zip carried until 2.2 had no pip, so every
    install from the AI models page and Extras failed on Windows."""
    ps1 = (WINDOWS / "build.ps1").read_text(encoding="utf-8")
    assert "python-version.txt" in ps1 and "python-build-standalone.sha256" in ps1
    assert "x86_64-pc-windows-msvc-install_only.tar.gz" in ps1
    assert "--no-index --no-deps" in ps1, "installed offline from the wheels just built"
    assert "import ninaivu, tkinter" in ps1, "the build proves Ninaivu and Tk import"
    assert "nsist" not in ps1 and not (WINDOWS / "installer.cfg").exists()
    portable = (WINDOWS / "build-portable.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $here "build\\python"' in portable
    assert "Lib\\site-packages\\pip" in portable, "the zip is refused without pip"
    assert "tcl8*" in portable, "and without the Tcl library"
    assert "._pth" not in portable and "pkgs" not in portable


def test_the_windows_build_stops_on_a_failed_command():
    ps1 = (WINDOWS / "build.ps1").read_text(encoding="utf-8")
    assert ps1.count('Check "') >= 4
    portable = (WINDOWS / "build-portable.ps1").read_text(encoding="utf-8")
    assert "Windows Kits" in portable, "signtool is found in the SDK, not assumed on PATH"


def test_the_mac_app_carries_a_relocatable_python_not_a_venv():
    sh = (MACOS / "build.sh").read_text(encoding="utf-8")
    assert "python-build-standalone" in sh and "install_only" in sh
    assert "venv" not in sh.split("# 1.")[1].split("# 2.")[0].replace("a venv would not do", "")


def test_signing_is_decided_at_job_level():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "if: env.PFX != ''" not in text and "if: env.P12 != ''" not in text
    assert "HAS_PFX: ${{ secrets.WINDOWS_SIGN_PFX_BASE64 != '' }}" in text
    assert "macos-13" not in text


def test_the_build_scripts_fill_in_the_version_from_the_package():
    from ninaivu import __version__
    ps1 = (WINDOWS / "build.ps1").read_text(encoding="utf-8")
    assert "__version__" in ps1 and "ninaivu\\__init__.py" in ps1
    for sh in (MACOS / "build.sh", ROOT / "installers" / "linux" / "build.sh"):
        text = sh.read_text(encoding="utf-8")
        assert "__version__" in text and "ninaivu/__init__.py" in text
    assert re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "ninaivu" / "__init__.py").read_text()).group(1) == __version__


def test_the_mac_app_opens_the_control_panel_and_keeps_its_files_in_its_folder_or_application_support():
    sh = (MACOS / "build.sh").read_text(encoding="utf-8")
    launcher = (MACOS / "launcher.sh").read_text(encoding="utf-8")
    assert "<key>LSUIElement</key><false/>" in sh, "the Control Panel is a window: in the Dock while open"
    assert '"$here/launcher.sh" "$app/Contents/MacOS/ninaivu"' in sh
    assert 'NINAIVU_HOME="$HOME/Library/Application Support/Ninaivu"' in launcher, "on its own, as an app"
    assert '"$folder/.ninaivu-portable"' in launcher and 'NINAIVU_STATE_DIR="$folder/data"' in launcher
    assert "ninaivu.desktop.app" in launcher and "ninaivu.desktop.tray" in launcher
    assert "notarytool submit" in sh and "stapler staple" in sh
    assert "ditto -c -k" in sh, "zipped with the bundle's links and attributes kept"
    assert (MACOS / "entitlements.plist").is_file()
    assert (MACOS / "ninaivu.iconset" / "icon_512x512@2x.png").is_file()
    assert (MACOS / "portable" / "README-PORTABLE.txt").is_file()


def test_the_release_workflow_builds_four_portable_downloads_and_signs_only_with_secrets():
    yaml = pytest.importorskip("yaml")
    flow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    assert set(flow["jobs"]) == {"gate", "windows", "macos", "linux", "linux-arm64", "pair-across",
                                 "release", "docker"}
    assert flow["jobs"]["release"]["needs"] == ["gate", "windows", "macos", "linux", "linux-arm64",
                                                "pair-across"]
    # GitHub's runners unless a repository variable names one at home (the
    # Mac mini, Apple silicon too; its Linux runners are arm64 as well).
    assert flow["jobs"]["macos"]["runs-on"] == "${{ vars.MAC_RUNNER || 'macos-14' }}", "Apple silicon"
    assert flow["jobs"]["linux-arm64"]["runs-on"] == "${{ vars.LINUX_ARM64_RUNNER || 'ubuntu-24.04-arm' }}", \
        "built natively, not emulated"
    for job in ("windows", "macos", "linux", "linux-arm64"):
        assert "strategy" not in flow["jobs"][job], f"{job}: one build, no matrix"
    files = next(s for s in flow["jobs"]["release"]["steps"] if s.get("uses", "").startswith("softprops/"))["with"]["files"]
    for wanted in ("out/windows/*.zip", "out/macos/*.zip", "out/linux/*.tar.gz",
                   "out/linux-arm64/*.tar.gz"):
        assert wanted in files
    for gone in (".exe", ".dmg", ".sh", "ninaivu.rb", "x86_64"):
        assert gone not in files
    signing = [s for job in ("windows", "macos") for s in flow["jobs"][job]["steps"]
               if "signing certificate" in s.get("name", "")]
    assert len(signing) == 2 and all(s.get("if") for s in signing), "signing is skipped without the secret"


# -- no source inside -----------------------------------------------------------

def test_every_installer_compiles_the_source_away():
    """Each build ships Ninaivu as a wheel with the Python compiled to bytecode."""
    for script in (WINDOWS / "build.ps1", MACOS / "build.sh", ROOT / "installers" / "linux" / "build.sh"):
        assert "strip_sources.py" in script.read_text(encoding="utf-8"), script.name


def test_a_stripped_wheel_installs_and_imports_without_its_source(tmp_path):
    import subprocess
    import sys
    import zipfile
    sys.path.insert(0, str(ROOT / "installers"))
    import strip_sources

    # A small package with a subpackage, a data file and a module that imports a sibling.
    pkg = tmp_path / "src" / "tinypkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .core import answer\n__version__ = '1.0'\n")
    (pkg / "core.py").write_text("from pathlib import Path\ndef answer():\n    return (Path(__file__).parent / 'data.txt').read_text().strip()\n")
    (pkg / "sub" / "__init__.py").write_text("WHO = 'sub'\n")
    (pkg / "data.txt").write_text("42\n")
    (tmp_path / "src" / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='tinypkg'\nversion='1.0'\n[tool.setuptools.package-data]\ntinypkg=['*.txt']\n")
    wheels = tmp_path / "wheels"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps",
                    "--wheel-dir", str(wheels), str(tmp_path / "src")], check=True)
    wheel = next(wheels.glob("tinypkg-*.whl"))

    compiled, kept = strip_sources.strip(wheel)
    assert compiled == 3 and kept >= 1
    with zipfile.ZipFile(wheel) as inside:
        names = inside.namelist()
        assert not [n for n in names if n.endswith(".py")], "source left inside"
        assert "tinypkg/core.pyc" in names and "tinypkg/sub/__init__.pyc" in names and "tinypkg/data.txt" in names
        record = inside.read(next(n for n in names if n.endswith("RECORD"))).decode()
        assert "tinypkg/core.pyc,sha256=" in record and "core.py," not in record

    target = tmp_path / "site"
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-index",
                    "--target", str(target), str(wheel)], check=True)
    assert not list(target.rglob("*.py")), "pip wrote source back"
    out = subprocess.run([sys.executable, "-c", "import tinypkg, tinypkg.sub; print(tinypkg.answer(), tinypkg.sub.WHO)"],
                         cwd=target, env={"PYTHONPATH": str(target), "PATH": ""}, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["42", "sub"]


def test_the_linux_build_is_a_folder_that_needs_no_python_on_the_machine():
    linux = ROOT / "installers" / "linux"
    build = (linux / "build.sh").read_text(encoding="utf-8")
    launcher = (linux / "portable" / "ninaivu").read_text(encoding="utf-8")
    assert "python-build-standalone" in build and "install_only" in build, "the Python is bundled"
    assert "--only-binary=:all:" in build and "--platform" in build, "wheels are fetched for the target"
    assert "Linux-x86_64)" in build and "Linux-aarch64)" in build, "x64 and arm64, each on its own kind"
    assert "manylinux_2_28_aarch64" in build and "aarch64-unknown-linux-gnu" in build
    assert "--no-index" in build and "pip install" in build, "installed offline from the fetched wheels"
    assert 'top" != "Open Ninaivu,app,logs,ninaivu,"' in build, "four things at the top, and nothing else"
    assert "linux-$arch-portable" in build
    assert not (linux / "install.sh").exists() and not (linux / "header.sh").exists()
    # The machine's own Python is never called: only the bundled one, by path.
    assert 'python="$app/python/bin/python3"' in launcher
    assert not re.search(r"(?<![/\w.$-])python3?\b", launcher.replace('python="$app/python/bin/python3"', "")
                         .replace("$python", "")), "a bare python on the PATH"


def test_the_author_is_the_same_everywhere():
    import tomllib
    assert tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["authors"] == [{"name": "Jagadeesh Rajendran"}]
    assert "Jagadeesh Rajendran" in (WINDOWS / "build-portable.ps1").read_text(encoding="utf-8")
    assert "Jagadeesh Rajendran" in (MACOS / "build.sh").read_text(encoding="utf-8")


# -- the Control Panel is what opens ----------------------------------------------

def test_every_build_opens_the_control_panel():
    exe = (WINDOWS / "portable" / "Ninaivu.cs").read_text(encoding="utf-8")
    assert "-m ninaivu.desktop.app" in exe
    mac = (MACOS / "launcher.sh").read_text(encoding="utf-8")
    assert "-m ninaivu.desktop.app" in mac and "--tray" in mac
    assert "<key>LSUIElement</key><false/>" in (MACOS / "build.sh").read_text(encoding="utf-8"), \
        "a window app shows in the Dock"
    linux = (ROOT / "installers" / "linux" / "portable" / "ninaivu").read_text(encoding="utf-8")
    assert "-m ninaivu.desktop.app" in linux and "--tray" in linux and "--server" in linux


def test_the_control_panel_falls_back_to_the_tray_without_tk(monkeypatch):
    from ninaivu.desktop import app
    monkeypatch.setattr(app, "tk", None)
    called = []
    import ninaivu.desktop.tray as tray
    monkeypatch.setattr(tray, "main", lambda: called.append(True) or 7)
    assert app.main() == 7 and called == [True]


def test_every_download_is_used_as_a_household_would_before_a_release():
    """tools/check_portable.py, on each build's own runner, and Mugil's two
    computers across Mac, Windows and Linux."""
    yaml = pytest.importorskip("yaml")
    flow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    for job in ("windows", "macos", "linux", "linux-arm64"):
        runs = " ".join(s.get("run", "") for s in flow["jobs"][job]["steps"])
        assert "tools/check_portable.py check" in runs, job
        assert "tools/check_portable.py pair" in runs, job
    across = flow["jobs"]["pair-across"]
    pairs = {(e["pair"], e["role"]): e["os"] for e in across["strategy"]["matrix"]["include"]}
    assert pairs == {("mac-linux", "primary"): "macos-14", ("mac-linux", "secondary"): "ubuntu-latest",
                     ("windows-linux", "primary"): "windows-2022",
                     ("windows-linux", "secondary"): "ubuntu-latest",
                     ("mac-windows", "primary"): "macos-14", ("mac-windows", "secondary"): "windows-2022"}
    assert across["strategy"]["fail-fast"] is False, "both halves of a pair run to the end"

