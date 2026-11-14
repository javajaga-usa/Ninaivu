"""Install a real .deb, run its private Python as a normal user, remove it.

Run as root on an expendable Linux CI runner, not on a person's installation.
"""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path


def check(package: Path) -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this installer smoke check as root on a disposable CI runner")
    subprocess.run(["apt-get", "install", "-y", str(package.resolve())], check=True)
    account = "ninaivu-package-check"
    subprocess.run(["useradd", "--no-create-home", account], check=True)
    user = pwd.getpwnam(account)
    try:
        with tempfile.TemporaryDirectory(prefix="ninaivu-deb-check-") as temporary:
            home = Path(temporary)
            os.chown(home, user.pw_uid, user.pw_gid)

            def run(*args: str) -> str:
                return subprocess.check_output(
                    ["runuser", "-u", account, "--", "env", f"HOME={home}",
                     "NINAIVU_NO_DIALOG=1", *args], text=True)

            values = dict(line.split("=", 1) for line in run("/usr/bin/ninaivu", "--where").splitlines())
            state = home / ".ninaivu"
            assert values["NINAIVU_STATE_DIR"] == str(state)
            assert values["NINAIVU_LOG_DIR"] == str(state / "logs")
            python = values["NINAIVU_PYTHON"]
            run(python, "-c", "import ninaivu, tkinter, flask, cv2; print(ninaivu.__version__)")
            run(python, "-m", "pip", "--version")
            run(python, "-c", "import pathlib, sysconfig; "
                "pathlib.Path(sysconfig.get_path('purelib'), 'extras-write-check').touch()")
            run("xvfb-run", "-a", python, "-c",
                "import tkinter; root = tkinter.Tk(); root.update(); root.destroy()")
            run("/usr/bin/ninaivu", "--server", "--help")
            assert Path("/usr/share/applications/ninaivu.desktop").is_file()
            marker = state / "keep-my-library.txt"
            marker.write_text("preserve on removal", encoding="utf-8")
            subprocess.run(["apt-get", "install", "--reinstall", "-y", str(package.resolve())], check=True)
            run("/usr/bin/ninaivu", "--where")
            assert marker.read_text(encoding="utf-8") == "preserve on removal"
            subprocess.run(["apt-get", "remove", "-y", "ninaivu"], check=True)
            assert marker.read_text(encoding="utf-8") == "preserve on removal"
            assert not Path("/usr/bin/ninaivu").exists()
            assert not Path("/opt/ninaivu/app/python/bin/python3").exists()
    finally:
        subprocess.run(["userdel", account], check=True)


if __name__ == "__main__":
    check(Path(sys.argv[1]))
