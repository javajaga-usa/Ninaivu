"""The installed launcher keeps writes out of its root-owned program folder."""
from __future__ import annotations

import os
import importlib.util
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_deb_contains_the_runtime_menu_and_root_owned_metadata(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("build_deb", ROOT / "installers/linux/build-deb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    name = "Ninaivu-4.3.0-linux-x64"
    payload = tmp_path / f"{name}-portable"
    (payload / "app/python/bin").mkdir(parents=True)
    (payload / "app/python/bin/python3").write_text("private runtime")
    (payload / "ninaivu").write_text("native starter")
    (payload / "logs").mkdir()
    archive = tmp_path / f"{name}-portable.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        target.add(payload, arcname=payload.name)
    (tmp_path / f"{name}-portable-packages.txt").write_text("pinned wheels\n")

    def dpkg(args, **kwargs):
        assert args[:4] == ["dpkg-deb", "--root-owner-group", "-Zxz", "--build"]
        stage = Path(args[4])
        assert "Architecture: amd64" in (stage / "DEBIAN/control").read_text()
        assert (stage / "opt/ninaivu/app/python/bin/python3").is_file()
        assert not (stage / "opt/ninaivu/logs").exists()
        assert (stage / "usr/bin/ninaivu").readlink() == Path("/opt/ninaivu/ninaivu")
        assert "Exec=/usr/bin/ninaivu" in (stage / "usr/share/applications/ninaivu.desktop").read_text()
        assert "NINAIVU_PACKAGE=deb" in (stage / "opt/ninaivu/app/ninaivu.sh").read_text()

    monkeypatch.setattr(module.subprocess, "run", dpkg)
    assert module.build(archive).name == f"{name}.deb"
    assert (tmp_path / f"{name}-packages.txt").read_text() == "pinned wheels\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX package launcher")
def test_installed_launcher_uses_a_user_runtime_and_home_state(tmp_path):
    app = tmp_path / "installed/app"
    (app / "python/bin").mkdir(parents=True)
    (app / "python/bin/python3").symlink_to(sys.executable)
    shutil.copyfile(ROOT / "installers/linux/debian/ninaivu.sh", app / "ninaivu.sh")
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("NINAIVU_")}
    env.update(HOME=str(home), NINAIVU_PORTABLE="1", NINAIVU_PORTABLE_ROOT="/old")
    result = subprocess.check_output(["sh", str(app / "ninaivu.sh"), "--where"],
                                     text=True, env=env, timeout=60)
    values = dict(line.split("=", 1) for line in result.splitlines())
    assert values["NINAIVU_STATE_DIR"] == str(home / ".ninaivu")
    assert values["NINAIVU_LOG_DIR"] == str(home / ".ninaivu/logs")
    python = values["NINAIVU_PYTHON"]
    assert python == str(home / ".ninaivu/runtime/bin/python3")
    assert "include-system-site-packages = true" in (home / ".ninaivu/runtime/pyvenv.cfg").read_text()
    assert not (app.parent / "logs").exists()
    assert subprocess.check_output([python, "-c", "import sys; print(sys.prefix)"], text=True).strip() == str(home / ".ninaivu/runtime")
    # Reopening uses the same writable runtime instead of discarding Extras.
    marker = home / ".ninaivu/runtime/keep-extra"
    marker.touch()
    subprocess.check_call(["sh", str(app / "ninaivu.sh"), "--where"], env=env, timeout=30)
    assert marker.exists()


def test_debian_update_instructions_use_the_package_manager(monkeypatch):
    from ninaivu.desktop.updating import update_steps
    monkeypatch.setenv("NINAIVU_PACKAGE", "deb")
    steps = update_steps("4.3.0", Path("/opt/ninaivu"))
    assert ".deb" in steps[0]["detail"]
    assert "sudo apt install" in steps[1]["detail"]
    assert "home folder" in steps[2]["detail"]


def test_release_publishes_deb_and_keeps_archives_only_for_internal_checks():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["release"]["steps"]
    publication = next(step for step in steps if "softprops/action-gh-release@" in step.get("uses", ""))
    files = publication["with"]["files"]
    assert "out/linux/*.deb" in files
    assert "out/linux/*.tar.gz" not in files
    assert "out/windows/*.zip" in files and "out/macos/*.zip" in files
    for job in ("linux", "debian"):
        assert any("tools/check_deb.py" in step.get("run", "") for step in workflow["jobs"][job]["steps"])
