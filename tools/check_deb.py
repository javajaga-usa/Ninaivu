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
                    ["runuser", "-u", account, "--", "env", "-i", f"HOME={home}",
                     "PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8",
                     "NINAIVU_NO_DIALOG=1", *args], text=True, cwd=home, timeout=120)

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
            # Exercise a fresh user's desktop integration from the installed package,
            # not the checkout or an existing portable copy.
            (home / "Desktop").mkdir()
            os.chown(home / "Desktop", user.pw_uid, user.pw_gid)
            run(python, "-c",
                "import os; from pathlib import Path; from ninaivu.desktop import shortcuts; "
                "os.environ['NINAIVU_PACKAGE']='deb'; "
                "home=Path.home(); "
                "made=shortcuts.settle(home/'.ninaivu/control', desktop=home/'Desktop'); "
                "assert len(made)==3; "
                "assert all('/usr/bin/ninaivu' in p.read_text() and "
                "'sh /usr/bin/ninaivu' not in p.read_text() "
                "for p in (home/'Desktop').glob('*.desktop'))")
            run(python, "-c",
                "import sys; from ninaivu.desktop import launch, errors; "
                "sys.modules['ninaivu.desktop.app']=None; "
                "messages=[]; errors.tell=messages.append; "
                "assert launch.main([])==1 and messages")
            run(python, "-c",
                "from types import SimpleNamespace; from ninaivu.desktop.tray import PanelTray; "
                "calls=[]; host=SimpleNamespace(post=lambda f:f(), show_window=lambda:None, "
                "open_family=lambda:None, start=lambda:None, stop=lambda:None, "
                "quit=lambda:calls.append('quit')); "
                "menu=PanelTray(host); "
                "next(item for item in menu.menu() if item['label']=='Quit')['action'](); "
                "assert calls==['quit']")
            # These must run against the installed bytecode as well: a stale
            # runtime in the release archive must fail the installer gate.
            run(python, "-c",
                "import queue; from types import SimpleNamespace; "
                "from ninaivu.desktop.app import Dashboard; "
                "events=queue.Queue(); seen=[]; "
                "[events.put(('call', lambda i=i:seen.append(i))) for i in range(101)]; "
                "Dashboard._pump_events(SimpleNamespace(events=events)); "
                "assert seen==list(range(100)) and events.qsize()==1")
            run(python, "-c",
                "from types import SimpleNamespace; from ninaivu.desktop.app import Dashboard; "
                "panel=SimpleNamespace(command_area=SimpleNamespace(winfo_reqheight=lambda:120), "
                "upper_pane=SimpleNamespace(winfo_reqheight=lambda:430), logs_visible=False, "
                "split=SimpleNamespace(cget=lambda name:0)); "
                "assert Dashboard._wanted_height(panel)==550")
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
