"""The macOS double-click launchers: Ninaivu.command, "Setup Ninaivu.command",
and the Python finder and installer they share, launcher/install-python-mac.sh.

Finder behaviour cannot be exercised from here, so what these tests hold is
everything that *is* mechanical: the files exist and are executable, the shell
parses, the app bundles are well-formed, the argument handling does not
mistake a flag for a folder — which it did, and which then poisoned the
remembered path for every launch afterwards — and a Python installer is run
only when it is signed by the Python Software Foundation.

Every dialog is answered by a stand-in osascript, and every package manager
and download by a stand-in too, so nothing here installs anything or puts a
window on the screen of whoever runs it.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "Ninaivu.command"
INSTALLER = ROOT / "Setup Ninaivu.command"
HELPER = ROOT / "launcher" / "install-python-mac.sh"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")

#: The tests that run the launchers give them a bare POSIX PATH. Git Bash on
#: Windows is not the shell these are for; their syntax is still checked there.
runs_the_launcher = pytest.mark.skipif(
    sys.platform == "win32", reason="runs the macOS launcher against a POSIX shell")


def _working_bash() -> bool:
    """A bash that actually runs — not merely one on PATH.

    A bare Windows machine answers `which bash` with the WSL stub, which then
    fails every invocation with "no installed distributions".
    """
    bash = shutil.which("bash")
    if bash is None:
        return False
    try:
        return subprocess.run([bash, "-c", "true"], capture_output=True,
                              timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _working_bash(), reason="needs bash")


def _tool(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _no_dialogs(tmp_path: Path) -> tuple[str, Path]:
    """A PATH whose osascript answers every dialog as Cancel, and the file it
    writes each request to."""
    tools = tmp_path / "no-dialogs"
    asked = tmp_path / "dialogs.log"
    _tool(tools, "osascript", f'printf \'%s\\n\' "$*" >> "{asked}"\nexit 1\n')
    return f"{tools}:/usr/bin:/bin", asked


def _stage(tmp_path: Path, *files: Path) -> Path:
    """A copy of the Ninaivu folder with only what the launcher touches: the
    given files, the Python helper, and a .venv whose python is this one."""
    stage = tmp_path / "ninaivu"
    (stage / "launcher").mkdir(parents=True)
    for path in files:
        text = path.read_text(encoding="utf-8").replace(LSREGISTER, "true")
        (stage / path.name).write_text(text, encoding="utf-8")
        (stage / path.name).chmod(0o755)
    shutil.copy(HELPER, stage / "launcher" / HELPER.name)
    (stage / ".venv").mkdir()
    _tool(stage / ".venv" / "bin", "python", f'exec "{sys.executable}" "$@"\n')
    return stage


# ---------------------------------------------------------------------------
# The files themselves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [LAUNCHER, INSTALLER, HELPER])
def test_the_launchers_are_shipped(path):
    assert path.is_file(), f"{path.name} is missing"


@pytest.mark.parametrize("path", [LAUNCHER, INSTALLER])
@pytest.mark.skipif(os.name == "nt", reason="Windows has no executable bit")
def test_they_are_executable(path):
    assert path.stat().st_mode & 0o111, (
        f"{path.name} is not executable, so double-clicking it does nothing")


@pytest.mark.parametrize("path", [LAUNCHER, INSTALLER])
def test_they_start_with_a_shebang(path):
    assert path.read_text(encoding="utf-8").startswith("#!/bin/bash")


@pytest.mark.parametrize("path", [LAUNCHER, INSTALLER, HELPER, ROOT / "launcher" / "start.sh"])
@pytest.mark.skipif(os.name == "nt", reason="bash on Windows is WSL's, which may have no Linux to run")
def test_the_shell_parses(path):
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", [LAUNCHER, INSTALLER, HELPER])
def test_no_carriage_returns(path):
    """A CR on the shebang line and macOS looks for an interpreter called
    "bash\\r"; .gitattributes keeps them LF, and this says so if it did not."""
    assert b"\r" not in path.read_bytes()


def test_the_installer_clears_the_quarantine_flag():
    """Without this the apps it builds are blocked by Gatekeeper on arrival."""
    assert "com.apple.quarantine" in INSTALLER.read_text(encoding="utf-8")


def test_the_installer_builds_the_apps_locally():
    """An app created on the Mac is not quarantined; a downloaded one is."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert "Contents/MacOS" in text and "Info.plist" in text
    assert "$HOME/Applications" in text
    assert "Ninaivu Control Panel" in text and "ninaivu_control.pyw" in text
    assert "installers/macos/ninaivu.iconset" in text, "Ninaivu's own icon, not a drawn one"


def test_the_generated_plist_is_valid():
    """Extract the heredoc the installer writes and parse it as Finder would."""
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index("<?xml", text.index("Info.plist"))
    end = text.index("</plist>", start) + len("</plist>")
    plist = (text[start:end]
             .replace("$APP_NAME", "Ninaivu")
             .replace("$BUNDLE_ID", "local.ninaivu.launcher"))
    parsed = plistlib.loads(plist.encode())
    assert parsed["CFBundleExecutable"] == "Ninaivu"
    assert parsed["CFBundlePackageType"] == "APPL"
    assert parsed["CFBundleIdentifier"]


@pytest.mark.parametrize("path", [LAUNCHER, INSTALLER, HELPER, ROOT / "launcher" / "start.sh"])
def test_python_is_never_asked_of_the_command_line_tools(path):
    """They bring Python 3.9, which Ninaivu refuses (it needs 3.12)."""
    assert "xcode-select" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [LAUNCHER, INSTALLER, ROOT / "launcher" / "start.sh"])
def test_they_share_one_way_of_finding_python(path):
    assert "install-python-mac.sh" in path.read_text(encoding="utf-8")


def test_start_py_offers_a_setup_only_mode():
    """The installer needs to install without starting a server."""
    result = subprocess.run([sys.executable, str(ROOT / "launcher" / "start.py"), "--help"],
                            capture_output=True, text=True, timeout=60)
    assert "--setup-only" in result.stdout


# ---------------------------------------------------------------------------
# Ninaivu.command
# ---------------------------------------------------------------------------

@runs_the_launcher
def test_a_flag_is_not_taken_for_the_library_folder(tmp_path):
    """`Ninaivu.command --rescan` must not remember "--rescan" as the library."""
    home = tmp_path / "home"
    (home / ".ninaivu").mkdir(parents=True)
    remembered = home / ".ninaivu" / "library-path"
    library = tmp_path / "Pictures"
    library.mkdir()
    remembered.write_text(str(library))

    stage = _stage(tmp_path, LAUNCHER)
    (stage / "launcher" / "start.py").write_text("import sys; print('ARGS', sys.argv[1:])\n")

    result = subprocess.run(
        ["bash", str(stage / LAUNCHER.name), "--rescan"],
        capture_output=True, text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        timeout=60)

    assert f"ARGS ['{library}', '--rescan']" in result.stdout, result.stdout + result.stderr
    assert remembered.read_text() == str(library), "the flag was written to the remembered path"


@runs_the_launcher
def test_the_library_is_remembered_in_the_state_folder_it_is_given(tmp_path):
    """NINAIVU_STATE_DIR moves the server's folder; the remembered library goes
    with it, and so into its backups."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    library = tmp_path / "Pictures"
    library.mkdir()
    stage = _stage(tmp_path, LAUNCHER)
    (stage / "launcher" / "start.py").write_text("print('STARTED')\n")
    result = subprocess.run(
        ["bash", str(stage / LAUNCHER.name), str(library)],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "NINAIVU_STATE_DIR": str(state)})
    assert "STARTED" in result.stdout, result.stdout + result.stderr
    assert (state / "library-path").read_text() == str(library)
    assert not (home / ".ninaivu").exists()


@runs_the_launcher
def test_a_folder_that_no_longer_exists_is_not_used(tmp_path):
    home = tmp_path / "home"
    (home / ".ninaivu").mkdir(parents=True)
    (home / ".ninaivu" / "library-path").write_text(str(tmp_path / "gone"))

    stage = _stage(tmp_path, LAUNCHER)
    (stage / "launcher" / "start.py").write_text("print('SHOULD NOT RUN')\n")
    # The folder chooser is cancelled, so the launcher must stop rather than
    # run against a missing folder.
    path, asked = _no_dialogs(tmp_path)
    result = subprocess.run(
        ["bash", str(stage / LAUNCHER.name)],
        capture_output=True, text=True, env={"HOME": str(home), "PATH": path},
        input="", timeout=60)
    assert "SHOULD NOT RUN" not in result.stdout
    assert "not there any more" in result.stdout
    assert "choose folder" in asked.read_text(), "it never asked for a new folder"


@runs_the_launcher
def test_the_photos_library_package_is_refused(tmp_path):
    home = tmp_path / "home"
    (home / ".ninaivu").mkdir(parents=True)
    package = tmp_path / "Photos Library.photoslibrary"
    package.mkdir()
    (home / ".ninaivu" / "library-path").write_text(str(package))

    stage = _stage(tmp_path, LAUNCHER)
    (stage / "launcher" / "start.py").write_text("print('SHOULD NOT RUN')\n")
    path, asked = _no_dialogs(tmp_path)
    result = subprocess.run(
        ["bash", str(stage / LAUNCHER.name)],
        capture_output=True, text=True, env={"HOME": str(home), "PATH": path},
        input="", timeout=60)
    assert "SHOULD NOT RUN" not in result.stdout
    assert "photoslibrary" in result.stdout
    assert "Photos library" in asked.read_text(), "the refusal was never shown"


# ---------------------------------------------------------------------------
# Setup Ninaivu.command
# ---------------------------------------------------------------------------

@runs_the_launcher
def test_apps_only_builds_both_apps_and_installs_nothing(tmp_path):
    """`--apps-only` makes Ninaivu and Ninaivu Control Panel, and neither looks
    for Python nor runs start.py. The panel's app opens the panel itself, not
    a Terminal."""
    home = tmp_path / "home"
    home.mkdir()
    stage = _stage(tmp_path, INSTALLER)
    (stage / "launcher" / "start.py").write_text("raise SystemExit('start.py ran')\n")
    path, asked = _no_dialogs(tmp_path)

    system_apps = tmp_path / "Applications"      # stands in for /Applications
    system_apps.mkdir()
    result = subprocess.run(
        ["bash", str(stage / INSTALLER.name), "--apps-only"],
        capture_output=True, text=True, timeout=120,
        env={"HOME": str(home), "PATH": path, "NINAIVU_SYSTEM_APPS_DIR": str(system_apps)})

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "start.py ran" not in output
    assert not asked.exists(), "it asked something while only rebuilding the apps"
    apps = home / "Applications"
    identifiers = set()
    for name in ("Ninaivu", "Ninaivu Control Panel"):
        contents = apps / f"{name}.app" / "Contents"
        plist = plistlib.loads((contents / "Info.plist").read_bytes())
        assert plist["CFBundleExecutable"] == name
        identifiers.add(plist["CFBundleIdentifier"])
        assert (contents / "MacOS" / name).stat().st_mode & 0o111, name
    assert len(identifiers) == 2, "two apps sharing an identifier are one app to macOS"
    assert "org.ninaivu.app" not in identifiers, "the installer's app has that one"
    panel = (apps / "Ninaivu Control Panel.app" / "Contents" / "MacOS"
             / "Ninaivu Control Panel").read_text(encoding="utf-8")
    assert f"here='{stage}'" in panel
    assert '"$here/.venv/bin/python" "$here/ninaivu_control.pyw"' in panel
    assert "Terminal" not in panel
    launcher = (apps / "Ninaivu.app" / "Contents" / "MacOS" / "Ninaivu").read_text(encoding="utf-8")
    assert f"here='{stage}'" in launcher and '"$here/Ninaivu.command"' in launcher
    # The same panel beside the setup script and in the main Applications folder.
    for folder in (stage, system_apps):
        copy = folder / "Ninaivu Control Panel.app" / "Contents" / "MacOS" / "Ninaivu Control Panel"
        assert copy.read_text(encoding="utf-8") == panel, folder
        assert copy.stat().st_mode & 0o111
    assert not (system_apps / "Ninaivu.app").exists(), "only the panel is copied"


@runs_the_launcher
def test_the_apps_work_from_a_folder_with_quotes_and_dollars_in_its_name(tmp_path):
    """Audit 2026-10-06 A-55: the folder's path went into the two launchers
    bare, so a " or a $ in it broke them, and a backtick ran a command."""
    awkward = tmp_path / "Bob's \"photos\" $HOME `touch pwned`"
    awkward.mkdir()
    home = awkward / "home"
    home.mkdir()
    stage = _stage(awkward, INSTALLER)
    (stage / "ninaivu_control.pyw").write_text("import os; print('panel ran in', os.getcwd())\n")
    path, _asked = _no_dialogs(tmp_path)
    tools = tmp_path / "open-stub"
    _tool(tools, "open", 'printf "%s\\n" "$@"\n')
    env = {"HOME": str(home), "PATH": f"{tools}:{path}",
           "NINAIVU_SYSTEM_APPS_DIR": str(tmp_path / "none")}
    done = subprocess.run(["bash", str(stage / INSTALLER.name), "--apps-only"],
                          capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    apps = home / "Applications"
    panel = subprocess.run(
        ["bash", str(apps / "Ninaivu Control Panel.app/Contents/MacOS/Ninaivu Control Panel")],
        capture_output=True, text=True, timeout=60, env=env, cwd=tmp_path)
    assert panel.stdout.strip() == f"panel ran in {stage}", panel.stderr
    opened = subprocess.run(["bash", str(apps / "Ninaivu.app/Contents/MacOS/Ninaivu")],
                            capture_output=True, text=True, timeout=60, env=env, cwd=tmp_path)
    assert opened.stdout.splitlines() == ["-a", "Terminal", f"{stage}/Ninaivu.command"]
    assert not (tmp_path / "pwned").exists()


@runs_the_launcher
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root may write to a read-only folder")
def test_a_main_applications_folder_it_cannot_write_is_left_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stage = _stage(tmp_path, INSTALLER)
    path, _asked = _no_dialogs(tmp_path)
    locked = tmp_path / "Applications"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        result = subprocess.run(
            ["bash", str(stage / INSTALLER.name), "--apps-only"],
            capture_output=True, text=True, timeout=120,
            env={"HOME": str(home), "PATH": path, "NINAIVU_SYSTEM_APPS_DIR": str(locked)})
    finally:
        locked.chmod(0o755)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cannot write there" in result.stdout
    assert not (locked / "Ninaivu Control Panel.app").exists()
    assert (home / "Applications" / "Ninaivu Control Panel.app").exists()


# ---------------------------------------------------------------------------
# launcher/install-python-mac.sh
# ---------------------------------------------------------------------------

def _helper(tmp_path, *args, tools, extra_env=None):
    env = {"HOME": str(tmp_path), "PATH": f"{tools}:/usr/bin:/bin", "TMPDIR": str(tmp_path),
           "NINAIVU_NO_BREW": "1",
           "NINAIVU_PYTHON_CERTIFICATES": str(tmp_path / "no-certificates-command")}
    env.update(extra_env or {})
    return subprocess.run(["sh", str(HELPER), *args], capture_output=True, text=True,
                          env=env, timeout=60)


@runs_the_launcher
def test_find_takes_only_a_python_new_enough(tmp_path):
    tools = tmp_path / "bin"
    _tool(tools, "python3", "exit 1\n")                      # a 3.9 says no
    _tool(tools, "python3.14", "exit 1\n")                   # one that will not run
    newer = _tool(tools, "python3.13", "exit 0\n")
    _tool(tools, "python3.12", "exit 0\n")
    result = _helper(tmp_path, "--find", tools=tools)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(newer)


@runs_the_launcher
def test_declining_installs_nothing(tmp_path):
    tools = tmp_path / "bin"
    log = tmp_path / "ran.log"
    _tool(tools, "osascript", f'echo "osascript $*" >> "{log}"\nexit 1\n')
    _tool(tools, "curl", f'echo "curl $*" >> "{log}"\nexit 1\n')
    result = _helper(tmp_path, tools=tools)
    assert result.returncode == 1
    assert "Nothing was changed" in result.stdout
    assert "curl" not in log.read_text()


def _python_org(tmp_path, signature):
    """Stand-ins for a Mac with no Homebrew: a dialog answered Install, a
    download, pkgutil saying *signature*, and an osascript that logs the
    administrator install it is asked for."""
    tools = tmp_path / "bin"
    log = tmp_path / "ran.log"
    _tool(tools, "osascript",
          f'echo "osascript $*" >> "{log}"\n'
          'case "$*" in *"display dialog"*) echo Install ;; esac\nexit 0\n')
    _tool(tools, "curl",
          'while [ $# -gt 0 ]; do [ "$1" = -o ] && { shift; echo pkg > "$1"; }; shift; done\n')
    _tool(tools, "pkgutil", f"cat <<'OUT'\n{signature}\nOUT\n")
    return _helper(tmp_path, tools=tools), log


@runs_the_launcher
def test_an_unsigned_installer_is_refused(tmp_path):
    result, log = _python_org(tmp_path, "Package \"python.pkg\":\n   Status: no signature")
    assert result.returncode == 1
    assert "not signed by the Python Software Foundation" in result.stdout
    assert "administrator privileges" not in log.read_text()


@runs_the_launcher
def test_a_signature_apple_did_not_issue_is_refused(tmp_path):
    result, log = _python_org(
        tmp_path, "   Status: signed by a certificate trusted by macOS\n"
                  "   1. Developer ID Installer: Python Software Foundation (BMM5U3QVKW)")
    assert result.returncode == 1
    assert "administrator privileges" not in log.read_text()


@runs_the_launcher
def test_the_signed_installer_is_run_with_the_administrators_password(tmp_path):
    result, log = _python_org(
        tmp_path, "   Status: signed by a developer certificate issued by Apple for distribution\n"
                  "   Certificate Chain:\n"
                  "    1. Developer ID Installer: Python Software Foundation (BMM5U3QVKW)")
    assert result.returncode == 0, result.stdout + result.stderr
    ran = log.read_text()
    assert "with administrator privileges" in ran and "installer -pkg" in ran
    assert "python-3.12.10-macos11.pkg" in ran


@runs_the_launcher
def test_a_signer_that_only_names_the_psf_is_refused(tmp_path):
    """Audit 2026-10-06 A-54: the signer was matched by substring, so any
    Developer ID with "Python Software Foundation" in its name passed."""
    result, log = _python_org(
        tmp_path, "   Status: signed by a developer certificate issued by Apple for distribution\n"
                  "   Certificate Chain:\n"
                  "    1. Developer ID Installer: Not Python Software Foundation (ABCDE12345)\n"
                  "    2. Developer ID Certification Authority\n"
                  "    3. Python Software Foundation (BMM5U3QVKW)")
    assert result.returncode == 1
    assert "administrator privileges" not in log.read_text()


def test_the_installer_comes_from_python_org_and_brew_is_preferred():
    text = HELPER.read_text(encoding="utf-8")
    assert "https://www.python.org/ftp/python/$VERSION/python-$VERSION-macos11.pkg" in text
    assert "VERSION=3.12.10" in text
    assert "install python@3.12" in text
    assert "pkgutil --check-signature" in text
