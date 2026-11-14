"""Wrap the tested private Linux runtime in a Debian desktop package."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def build(archive: Path) -> Path:
    name = archive.name.removesuffix("-portable.tar.gz")
    version, arch = name.removeprefix("Ninaivu-").split("-linux-")
    architecture = {"x64": "amd64", "arm64": "arm64"}[arch]
    output = archive.with_name(f"{name}.deb")
    with tempfile.TemporaryDirectory(prefix="ninaivu-deb-") as temporary:
        stage = Path(temporary)
        unpack = stage / "unpacked"
        with tarfile.open(archive) as source:
            source.extractall(unpack, filter="data")
        payload = unpack / f"{name}-portable"
        package = stage / "package"
        installed = package / "opt/ninaivu"
        installed.parent.mkdir(parents=True)
        shutil.copytree(payload, installed, ignore=shutil.ignore_patterns("logs"))
        shutil.copyfile(ROOT / "installers/linux/debian/ninaivu.sh", installed / "app/ninaivu.sh")
        (installed / "app/ninaivu.sh").chmod(0o755)
        (installed / "app/README-PORTABLE.txt").unlink(missing_ok=True)
        binary = package / "usr/bin"
        binary.mkdir(parents=True)
        (binary / "ninaivu").symlink_to("/opt/ninaivu/ninaivu")
        desktop = package / "usr/share/applications"
        desktop.mkdir(parents=True)
        (desktop / "ninaivu.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Ninaivu\n"
            "Comment=Your family's photo library\nExec=/usr/bin/ninaivu\n"
            "Icon=ninaivu\nTerminal=false\nCategories=Graphics;Photography;\n",
            encoding="utf-8")
        icons = package / "usr/share/icons/hicolor/512x512/apps"
        icons.mkdir(parents=True)
        shutil.copyfile(ROOT / "ninaivu/static/icons/panel/icon-512.png", icons / "ninaivu.png")
        control = package / "DEBIAN"
        control.mkdir()
        (control / "control").write_text(
            f"Package: ninaivu\nVersion: {version}\nArchitecture: {architecture}\n"
            "Maintainer: Ninaivu <javajaga-usa@users.noreply.github.com>\n"
            "Section: graphics\nPriority: optional\n"
            "Depends: libc6 (>= 2.28), libx11-6, libxext6, libxrender1, libxft2, "
            "libxss1, libfontconfig1, libgl1, libegl1, libglib2.0-0, libsm6, "
            "libice6, libfribidi0, fonts-dejavu-core\n"
            "Description: Private family photo library\n"
            " Includes a private Python runtime. Libraries and settings stay in ~/.ninaivu.\n",
            encoding="utf-8")
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build",
                        str(package), str(output.resolve())], check=True)
    manifest = archive.with_name(f"{name}-portable-packages.txt")
    shutil.copyfile(manifest, archive.with_name(f"{name}-packages.txt"))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    print(build(parser.parse_args().archive))
