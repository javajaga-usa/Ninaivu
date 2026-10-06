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
    assert cfg["Application"]["entry_point"] == "ninaivu.desktop.app:main", "the Control Panel is what opens"
    assert callable(_entry_point(cfg["Application"]["entry_point"]))
    assert callable(_entry_point(cfg["Shortcut Ninaivu tray"]["entry_point"]))
    assert callable(_entry_point(cfg["Command ninaivu"]["entry_point"]))
    assert cfg["Application"]["console"] == "false" and cfg["Shortcut Ninaivu tray"]["console"] == "false", \
        "neither window may open a console"
    assert _win(cfg["Application"]["icon"]).is_file()
    assert _win(cfg["Application"]["license_file"]).is_file()
    assert cfg["Python"]["version"].startswith("3.12"), "the floor the package declares"
    for line in cfg["Include"]["files"].splitlines():
        source = line.split(">")[0].strip()
        if source and source != "tcl":                    # tcl\ is copied in by build.ps1
            assert _win(source).exists(), source
    assert "tcl > $INSTDIR\\Python" in cfg["Include"]["files"], "the Tcl library goes where _tkinter looks"
    ps1 = (WINDOWS / "build.ps1").read_text(encoding="utf-8")
    for piece in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "pynsist_pkgs", 'Lib\\tkinter'):
        assert piece in ps1, f"build.ps1 bundles {piece} for the Control Panel"
    assert _win(cfg["Build"]["nsi_template"]).is_file()


def test_the_version_is_filled_into_the_application_line_only():
    """A blanket "version=" replace also rewrote [Python] version=3.12.10, and
    pynsist went looking for Python 0.1.0."""
    cfg = (WINDOWS / "installer.cfg").read_text(encoding="utf-8")
    ps1 = (WINDOWS / "build.ps1").read_text(encoding="utf-8")
    assert "version=__VERSION__" in cfg and "installer_name=Ninaivu-__VERSION__" in cfg
    assert '.Replace("__VERSION__", $version)' in ps1
    assert "(?m)^version=" not in ps1
    assert "packages=ninaivu" not in cfg, "Ninaivu goes in as a wheel, built by build.ps1"
    assert "--no-deps $root" in ps1


def test_the_windows_build_stops_on_a_failed_command():
    ps1 = (WINDOWS / "build.ps1").read_text(encoding="utf-8")
    assert ps1.count('Check "') >= 4
    assert "Windows Kits" in ps1, "signtool is found in the SDK, not assumed on PATH"


def test_the_mac_app_carries_a_relocatable_python_not_a_venv():
    sh = (MACOS / "build.sh").read_text(encoding="utf-8")
    assert "python-build-standalone" in sh and "install_only" in sh
    assert "venv" not in sh.split("# 1.")[1].split("# 2.")[0].replace("a venv would not do", "")


def test_signing_is_decided_at_job_level():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "if: env.PFX != ''" not in text and "if: env.P12 != ''" not in text
    assert "HAS_PFX: ${{ secrets.WINDOWS_SIGN_PFX_BASE64 != '' }}" in text
    assert "macos-13" not in text


def test_the_nsis_template_sets_the_root_the_tray_reads():
    text = (WINDOWS / "ninaivu.nsi").read_text(encoding="utf-8")
    assert 'extends "pyapp.nsi"' in text
    assert '"NINAIVU_HOME" "$INSTDIR"' in text
    assert "ninaivu.desktop.autostart --start" in text, "start at sign-in, the same way the tray does it"
    assert 'DeleteRegValue HKCU "Environment" "NINAIVU_HOME"' in text, "and the uninstaller undoes it"


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
    assert "__version__" in ps1 and "ninaivu\\__init__.py" in ps1
    assert "__version__" in sh and "ninaivu/__init__.py" in sh
    for text in (ps1, sh):
        assert "__VERSION__" in text, "the manifests' placeholder is filled"
    assert re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "ninaivu" / "__init__.py").read_text()).group(1) == __version__


def test_the_mac_app_opens_the_control_panel_and_keeps_its_files_in_application_support():
    sh = (MACOS / "build.sh").read_text(encoding="utf-8")
    assert "<key>LSUIElement</key><false/>" in sh, "the Control Panel is a window: in the Dock while open"
    assert 'NINAIVU_HOME="$HOME/Library/Application Support/Ninaivu"' in sh
    assert "ninaivu.desktop.app" in sh and "ninaivu.desktop.tray" in sh, "the panel opens; the tray is still there"
    assert "notarytool submit" in sh and "stapler staple" in sh
    assert (MACOS / "entitlements.plist").is_file()
    assert (MACOS / "ninaivu.iconset" / "icon_512x512@2x.png").is_file()


def test_the_cask_has_a_hash_per_architecture():
    rb = (MACOS / "homebrew" / "ninaivu.rb").read_text(encoding="utf-8")
    assert "__SHA256_ARM64__" in rb and "__SHA256_X86_64__" in rb
    assert 'app "Ninaivu.app"' in rb
    assert "local.ninaivu.start" in rb, "the launch agent the tray writes is what uninstall unloads"


def test_the_release_workflow_builds_all_four_and_signs_only_with_secrets():
    yaml = pytest.importorskip("yaml")
    flow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    assert set(flow["jobs"]) == {"gate", "windows", "macos", "linux", "release", "docker"}
    assert flow["jobs"]["release"]["needs"] == ["gate", "windows", "macos", "linux"]
    archs = [m["arch"] for m in flow["jobs"]["macos"]["strategy"]["matrix"]["include"]]
    assert sorted(archs) == ["arm64", "x86_64"]
    assert flow["jobs"]["linux"]["strategy"]["matrix"]["arch"] == ["amd64", "arm64"]
    files = next(s for s in flow["jobs"]["release"]["steps"] if s.get("uses", "").startswith("softprops/"))["with"]["files"]
    assert "out/linux-amd64/*.sh" in files and "out/linux-arm64/*.sh" in files
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


def test_the_linux_installer_is_one_file_that_needs_no_python_on_the_machine():
    linux = ROOT / "installers" / "linux"
    build = (linux / "build.sh").read_text(encoding="utf-8")
    header = (linux / "header.sh").read_text(encoding="utf-8")
    install = (linux / "install.sh").read_text(encoding="utf-8")
    assert "python-build-standalone" in build and "install_only" in build, "the Python is bundled"
    assert "--only-binary=:all:" in build and "--platform" in build, "wheels are fetched for the target"
    for arch in ("amd64", "arm64"):
        assert f"{arch})" in build
    assert header.rstrip().endswith("__PAYLOAD_BELOW__"), "the tarball follows the marker"
    assert "tail -n" in header and "tar -xzf" in header
    assert "--no-index" in install and "pip install" in install, "installed offline from the bundled wheels"
    assert "NINAIVU_HOME" in install and "systemctl" in install and "ninaivu.desktop" in install
    # The machine's own Python is never called: only the bundled one, by path.
    assert not re.search(r"(?<![/\w.-])python3?\b(?!\.new)", install.replace("$prefix/python", "")), \
        "a bare python on the PATH"
    assert "Jagadeesh Rajendran" in header


def test_the_author_is_the_same_everywhere():
    import tomllib
    assert tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["authors"] == [{"name": "Jagadeesh Rajendran"}]
    assert "publisher=Jagadeesh Rajendran" in (WINDOWS / "installer.cfg").read_text(encoding="utf-8")
    assert "Publisher: Jagadeesh Rajendran" in (WINDOWS / "winget" / "Ninaivu.Ninaivu.locale.en-US.yaml").read_text(encoding="utf-8")
    assert "Jagadeesh Rajendran" in (MACOS / "build.sh").read_text(encoding="utf-8")


# -- the Control Panel is what opens ----------------------------------------------

def test_every_installer_opens_the_control_panel_and_puts_it_on_the_desktop():
    nsi = (WINDOWS / "ninaivu.nsi").read_text(encoding="utf-8")
    assert '$DESKTOP\\Ninaivu Control Panel.lnk' in nsi, "a Desktop shortcut on Windows"
    assert nsi.count("Ninaivu Control Panel.lnk") >= 2, "and it is removed on uninstall"
    mac = (MACOS / "build.sh").read_text(encoding="utf-8")
    assert "-m ninaivu.desktop.app" in mac and "--tray" in mac
    assert "<key>LSUIElement</key><false/>" in mac, "a window app shows in the Dock"
    linux = (ROOT / "installers" / "linux" / "install.sh").read_text(encoding="utf-8")
    assert 'Exec=$(desktop_quote "$prefix/ninaivu-panel")' in linux
    assert "Name=Ninaivu Control Panel" in linux
    assert '$HOME/Desktop/ninaivu.desktop' in linux


def test_the_control_panel_falls_back_to_the_tray_without_tk(monkeypatch):
    from ninaivu.desktop import app
    monkeypatch.setattr(app, "tk", None)
    called = []
    import ninaivu.desktop.tray as tray
    monkeypatch.setattr(tray, "main", lambda: called.append(True) or 7)
    assert app.main() == 7 and called == [True]
