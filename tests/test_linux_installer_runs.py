"""The Linux installer, run: with a stand-in Python, systemctl and id.

The other installer tests read the scripts. These run install.sh in a
throwaway home with folder names that have a space, a quote, a $ and a % in
them, and check that what it wrote (the commands, the desktop entry, the
service) reads those paths back whole, each by its own rules, and that an
upgrade stops what is running and starts the new version, keeps the library,
the state and the AI models, and that uninstalling removes only what was
installed.
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
printf 'NINAIVU_STATE_DIR=%s\\n' "$NINAIVU_STATE_DIR" >> "$RECORD"
printf 'NINAIVU_AI_MODELS_DIR=%s\\n' "$NINAIVU_AI_MODELS_DIR" >> "$RECORD"
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


def install(world, photos=True):
    return subprocess.run(
        ["sh", str(INSTALL), str(world["payload"]), "--prefix", str(world["prefix"]),
         *(["--photos", str(world["photos"])] if photos else []), "--quiet"],
        env=world["env"], capture_output=True, text=True, timeout=120)


def recorded_env(world) -> dict[str, str]:
    lines = world["record"].read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line.startswith("NINAIVU_"))


def recorded(world) -> tuple[str, list[str]]:
    lines = world["record"].read_text().splitlines()
    return (recorded_env(world)["NINAIVU_HOME"],
            [line[4:] for line in lines if line.startswith("ARG=")])


def unit_path(world) -> Path:
    return world["home"] / ".config/systemd/user/ninaivu.service"


def service(world) -> dict[str, list]:
    """Environment= as a list of assignments, ExecStart= as its words."""
    out: dict[str, list] = {"Environment": []}
    for line in unit_path(world).read_text().splitlines():
        key, _, value = line.partition("=")
        if key == "Environment":
            out["Environment"] += systemd_words(value)
        elif key == "ExecStart":
            out["ExecStart"] = systemd_words(value)
    return out


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

    # The service. The program is /usr/bin/env, a literal path: systemd takes
    # the first word of ExecStart= as the file to run without turning $$ back
    # into $, so a $ in the folder's name made a unit that could not start.
    assert service(world)["Environment"] == [
        f"NINAIVU_HOME={state(world)}", f"NINAIVU_STATE_DIR={state(world)}",
        f"NINAIVU_AI_MODELS_DIR={state(world)}/ai-models"]
    assert service(world)["ExecStart"] == [
        "/usr/bin/env", str(prefix / "python/bin/python3"), "-m", "ninaivu",
        str(world["photos"]), "--supervised"]


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


# -- audit 2026-10-06: A-22 to A-25 ---------------------------------------------------

def test_an_upgrade_keeps_the_photographs_folder_the_service_had(world):
    """A-22: re-running the installer, as the guide says to upgrade, used to
    point the service at ~/Pictures, and the server took that as the library."""
    assert install(world).returncode == 0
    done = install(world, photos=False)
    assert done.returncode == 0, done.stderr
    assert service(world)["ExecStart"][4] == str(world["photos"])
    assert not (world["home"] / "Pictures").exists()


def test_an_upgrade_reads_the_folder_from_a_service_an_older_version_wrote(world):
    assert install(world).returncode == 0
    python = world["prefix"] / "python/bin/python3"
    # As 1.0.x wrote it: the Python is the program, and only NINAIVU_HOME.
    quoted = str(world["photos"]).replace("%", "%%")
    unit_path(world).write_text(
        "[Service]\n"
        f'Environment="NINAIVU_HOME={state(world)}"\n'
        f'ExecStart="{python}" -m ninaivu "{quoted}" --supervised\n')
    done = install(world, photos=False)
    assert done.returncode == 0, done.stderr
    assert service(world)["ExecStart"][4] == str(world["photos"])


def test_with_no_service_but_a_library_the_server_keeps_its_own(world):
    """No earlier service to read, but the server has settings: no folder is
    passed, so the server opens the library it already has."""
    legacy = world["home"] / ".ninaivu"
    legacy.mkdir()
    (legacy / "config.json").write_text("{}")
    done = install(world, photos=False)
    assert done.returncode == 0, done.stderr
    assert service(world)["ExecStart"] == [
        "/usr/bin/env", str(world["prefix"] / "python/bin/python3"), "-m", "ninaivu",
        "--supervised"]
    assert not (world["home"] / "Pictures").exists()
    # And an upgrade after that passes none either.
    assert install(world, photos=False).returncode == 0
    assert service(world)["ExecStart"][4] == "--supervised"


def test_the_state_an_earlier_server_used_is_the_one_used_and_purged(world):
    """A-25: the server reads NINAIVU_STATE_DIR, and 1.0.x set only
    NINAIVU_HOME, so its index, accounts and keys are in ~/.ninaivu. That
    folder stays in use, is named to the server, and --purge removes it."""
    legacy = world["home"] / ".ninaivu"
    legacy.mkdir()
    (legacy / "index.db").write_bytes(b"")
    assert install(world).returncode == 0
    assert f"NINAIVU_STATE_DIR={legacy}" in service(world)["Environment"]
    subprocess.run([str(world["prefix"] / "ninaivu")], env=world["env"], check=True)
    assert recorded_env(world)["NINAIVU_STATE_DIR"] == str(legacy)
    assert recorded_env(world)["NINAIVU_AI_MODELS_DIR"] == f"{state(world)}/ai-models"

    done = subprocess.run([str(world["prefix"] / "uninstall"), "--purge"], env=world["env"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert not legacy.exists()
    assert not Path(state(world)).exists()
    assert world["photos"].is_dir(), "the library is never touched"


def test_a_fresh_install_names_its_own_state_folder_to_the_server(world):
    assert install(world).returncode == 0
    Path(state(world), "index.db").write_bytes(b"")
    subprocess.run([str(world["prefix"] / "uninstall"), "--purge"], env=world["env"],
                   check=True, capture_output=True)
    assert not Path(state(world)).exists()


def test_uninstall_removes_only_what_was_installed(world):
    """A-24: --prefix ~/Apps once took a sibling app with it."""
    world["prefix"].mkdir(parents=True)
    neighbour = world["prefix"] / "another app"
    neighbour.mkdir()
    (neighbour / "keep.txt").write_text("not Ninaivu's")
    assert install(world).returncode == 0
    bindir = world["home"] / ".local/bin"
    assert (bindir / "ninaivu").is_symlink()
    (bindir / "someone-elses").symlink_to("/bin/true")
    done = subprocess.run([str(world["prefix"] / "uninstall")], env=world["env"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (neighbour / "keep.txt").read_text() == "not Ninaivu's"
    assert sorted(p.name for p in world["prefix"].iterdir()) == ["another app"]
    assert not (bindir / "ninaivu").exists() and (bindir / "someone-elses").is_symlink()
    assert not unit_path(world).exists()
    assert world["photos"].is_dir(), "the library is never touched"


def test_an_upgrade_moves_the_ai_models_out_of_the_old_python(world):
    """A-23: the models were in site-packages/.ai-models, which an upgrade
    deletes with the old Python: gigabytes gone on every upgrade."""
    assert install(world).returncode == 0
    old = world["prefix"] / "python/lib/python3.12/site-packages/.ai-models"
    (old / "magicbrush").mkdir(parents=True)
    (old / "magicbrush" / "unet.safetensors").write_bytes(b"weights")
    (old / "settings.json").write_text("{}")
    models = Path(state(world)) / "ai-models"
    models.mkdir(parents=True)
    (models / "settings.json").write_text('{"kept": true}')
    done = install(world)
    assert done.returncode == 0, done.stderr
    assert (models / "magicbrush" / "unet.safetensors").read_bytes() == b"weights"
    assert (models / "settings.json").read_text() == '{"kept": true}', "one already there wins"
    assert not old.exists(), "the old Python went"


def _files(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_an_upgrade_changes_nothing_in_the_library_or_the_state_folder(world):
    """An update replaces the program only: every photograph, the library's
    marker, the index, the settings and the setup code are left byte for
    byte as they were."""
    assert install(world).returncode == 0
    photos = world["photos"]
    (photos / "2024" / "05" / "01").mkdir(parents=True)
    (photos / "2024" / "05" / "01" / "IMG_0001.jpg").write_bytes(b"\xff\xd8 a photograph")
    (photos / ".ninaivu-library").write_text("library-id")
    server_state = Path(dict(
        a.split("=", 1) for a in service(world)["Environment"])["NINAIVU_STATE_DIR"])
    server_state.mkdir(parents=True, exist_ok=True)
    (server_state / "index.db").write_bytes(b"the index")
    (server_state / "config.json").write_text('{"roots": []}')
    (server_state / "setup-code.txt").write_text("123456")
    library, kept = _files(photos), _files(server_state)
    done = install(world, photos=False)
    assert done.returncode == 0, done.stderr
    assert _files(photos) == library
    assert _files(server_state) == kept
