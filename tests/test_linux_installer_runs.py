"""The Linux installer, run: with a stand-in Python, systemctl and id.

The other installer tests read the scripts. These run install.sh in a
throwaway home with folder names that have a space, a quote, a $ and a % in
them, and check that what it wrote (the commands, the desktop entry, the
service) reads those paths back whole, each by its own rules, and that an
upgrade stops what is running and starts the new version.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "installers" / "linux" / "install.sh"

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not shutil.which("pgrep"),
    reason="runs the Linux installer")

FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 0; fi
if [ "$3" = "linger" ]; then while :; do sleep 1; done; fi
printf 'NINAIVU_HOME=%s\\n' "$NINAIVU_HOME" > "$RECORD"
for a in "$@"; do printf 'ARG=%s\\n' "$a" >> "$RECORD"; done
"""

SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$STUBS/systemctl.log"
for a in "$@"; do
    case "$a" in
        is-active) [ -f "$STUBS/active" ]; exit $? ;;
        stop) rm -f "$STUBS/active" ;;
        restart|start) touch "$STUBS/active" ;;
    esac
done
exit 0
"""

ID = """#!/bin/sh
case "$1" in -u) echo 1000 ;; -un) echo tester ;; *) echo "uid=1000(tester)" ;; esac
"""

AWKWARD = "My Apps/O'Neil $HOME `x` 50%"


def _stub(folder: Path, name: str, text: str) -> None:
    path = folder / name
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture()
def world(tmp_path):
    payload = tmp_path / "payload"
    (payload / "python" / "bin").mkdir(parents=True)
    (payload / "wheels").mkdir()
    (payload / "wheels" / "ninaivu-0-py3-none-any.whl").write_bytes(b"")
    (payload / "VERSION").write_text("9.9.9\n")
    (payload / "LICENSE").write_text("MIT\n")
    (payload / "README.md").write_text("Ninaivu\n")
    _stub(payload / "python" / "bin", "python3", FAKE_PYTHON)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs, "systemctl", SYSTEMCTL)
    _stub(stubs, "loginctl", "#!/bin/sh\nexit 0\n")
    _stub(stubs, "id", ID)
    home = tmp_path / "home dir"
    home.mkdir()
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(home), "STUBS": str(stubs),
           "RECORD": str(tmp_path / "record.txt"), "LANG": "C.UTF-8"}
    prefix = tmp_path / AWKWARD
    photos = tmp_path / "Family Photos 100%"
    return {"payload": payload, "env": env, "prefix": prefix, "photos": photos,
            "home": home, "stubs": stubs, "record": tmp_path / "record.txt"}


def install(world):
    return subprocess.run(
        ["sh", str(INSTALL), str(world["payload"]), "--prefix", str(world["prefix"]),
         "--photos", str(world["photos"]), "--quiet"],
        env=world["env"], capture_output=True, text=True, timeout=120)


def recorded(world) -> tuple[str, list[str]]:
    lines = world["record"].read_text().splitlines()
    return lines[0].split("=", 1)[1], [line[4:] for line in lines[1:]]


def state(world) -> str:
    return str(world["home"] / ".local" / "state" / "ninaivu")


# -- reading back each file the way its reader does ---------------------------------

def desktop_exec(value: str) -> list[str]:
    """Exec= split as the Desktop Entry Specification says a launcher does."""
    unescaped = re.sub(r"\\(.)", lambda m: {"s": " ", "n": "\n", "t": "\t", "r": "\r"}
                       .get(m.group(1), m.group(1)), value)
    words, word, quoted, i, started = [], "", False, 0, False
    while i < len(unescaped):
        c = unescaped[i]
        if quoted:
            if c == "\\" and i + 1 < len(unescaped) and unescaped[i + 1] in '"`$\\':
                word += unescaped[i + 1]
                i += 1
            elif c == '"':
                quoted = False
            else:
                word += c
        elif c == '"':
            quoted, started = True, True
        elif c == " ":
            if started or word:
                words.append(word)
            word, started = "", False
        else:
            word += c
        i += 1
    if started or word:
        words.append(word)
    return [w.replace("%%", "%") for w in words]


def systemd_words(value: str) -> list[str]:
    """A command line split as systemd does: quotes, C escapes inside them,
    then %% and $$ as the literal characters."""
    words, word, quoted, i, started = [], "", False, 0, False
    while i < len(value):
        c = value[i]
        if quoted:
            if c == "\\" and i + 1 < len(value):
                word += value[i + 1]
                i += 1
            elif c == '"':
                quoted = False
            else:
                word += c
        elif c == '"':
            quoted, started = True, True
        elif c == " ":
            if started or word:
                words.append(word)
            word, started = "", False
        else:
            word += c
        i += 1
    if started or word:
        words.append(word)
    return [w.replace("%%", "%").replace("$$", "$") for w in words]


def test_awkward_folder_names_survive_every_file_it_writes(world):
    done = install(world)
    assert done.returncode == 0, done.stderr
    prefix = world["prefix"]

    # The command itself.
    subprocess.run([str(prefix / "ninaivu"), "a folder"], env=world["env"], check=True)
    home, args = recorded(world)
    assert home == state(world)
    assert args == ["-m", "ninaivu", "a folder"]

    # The Control Panel's desktop entry.
    entry = (Path(world["env"]["HOME"]) / ".local/share/applications/ninaivu.desktop").read_text()
    exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
    command = desktop_exec(exec_line[len("Exec="):])
    assert command == [str(prefix / "ninaivu-panel")]
    subprocess.run(command, env=world["env"], check=True)
    assert recorded(world)[1] == ["-m", "ninaivu.desktop.app"]

    # The service.
    unit = (world["home"] / ".config/systemd/user/ninaivu.service").read_text()
    lines = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    assert systemd_words(lines["Environment"]) == [f"NINAIVU_HOME={state(world)}"]
    assert systemd_words(lines["ExecStart"]) == [
        str(prefix / "python/bin/python3"), "-m", "ninaivu", str(world["photos"]), "--supervised"]


def test_an_upgrade_stops_what_is_running_and_starts_the_new_version(world):
    assert install(world).returncode == 0
    log = world["stubs"] / "systemctl.log"
    log.unlink()
    (world["stubs"] / "active").touch()               # the service is running
    # And the old version running by hand, as the tray or the panel would be.
    by_hand = subprocess.Popen([str(world["prefix"] / "python/bin/python3"), "-m",
                                "ninaivu", "linger"], env=world["env"])
    try:
        time.sleep(0.5)
        (world["payload"] / "VERSION").write_text("10.0.0\n")
        done = install(world)
        assert done.returncode == 0, done.stderr
        assert by_hand.wait(10) is not None, "the old one was left running"
    finally:
        if by_hand.poll() is None:
            by_hand.kill()
    calls = log.read_text().splitlines()
    stopped = next(i for i, c in enumerate(calls) if c.endswith("stop ninaivu"))
    restarted = next(i for i, c in enumerate(calls) if c.endswith("restart ninaivu"))
    assert stopped < restarted
    assert not any("--now" in c and "enable" in c for c in calls), "enable --now leaves the old one"
    assert (world["prefix"] / "VERSION").read_text().strip() == "10.0.0"
    assert not (world["prefix"] / "python.old").exists()


def test_a_folder_name_with_a_line_break_is_refused(world):
    world["prefix"] = world["prefix"].parent / "two\nlines"
    done = install(world)
    assert done.returncode == 2 and "line break" in done.stderr


def test_the_uninstaller_removes_the_awkwardly_named_folder(world):
    assert install(world).returncode == 0
    subprocess.run([str(world["prefix"] / "uninstall")], env=world["env"], check=True,
                   capture_output=True)
    assert not world["prefix"].exists()
    assert Path(state(world)).exists(), "the state is kept without --purge"
