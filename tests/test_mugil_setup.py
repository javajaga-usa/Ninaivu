"""``ninaivu mugil``: starting Ninaivu with the computer, and the checklist."""

import subprocess
from types import SimpleNamespace

from ninaivu.cli import mugil


def test_on_linux_a_user_service_is_written_and_enabled_not_started(tmp_path):
    ran = []
    env = {"NINAIVU_STATE_DIR": "/srv/ninaivu state", "USER": "jaga"}
    said = []
    code = mugil.setup(platform="linux", home=tmp_path, environ=env,
                       runner=lambda step, **_: ran.append(step) or SimpleNamespace(
                           returncode=0, stdout="", stderr=""), out=said.append)
    if not mugil.shutil.which("systemctl"):
        assert code == 1 and not ran
    else:
        assert code == 0
        assert ran == [["systemctl", "--user", "daemon-reload"],
                       ["systemctl", "--user", "enable", "ninaivu.service"]]
        assert any("enable-linger jaga" in line for line in said)
    unit = (tmp_path / ".config/systemd/user/ninaivu.service").read_text()
    assert "Restart=always" in unit and "WantedBy=default.target" in unit
    assert "Environment=NINAIVU_STATE_DIR=/srv/ninaivu state" in unit
    assert "-m ninaivu" in unit


def test_a_portable_folder_is_started_through_its_own_launcher(tmp_path):
    (tmp_path / "ninaivu").write_text("#!/bin/sh\n")
    env = {"NINAIVU_PORTABLE": "1", "NINAIVU_PORTABLE_ROOT": str(tmp_path)}
    assert mugil.start_command(env) == [str(tmp_path / "ninaivu"), "--server"]


def test_a_dry_run_writes_nothing(tmp_path):
    said = []
    assert mugil.setup(platform="linux", dry_run=True, home=tmp_path, environ={},
                       out=said.append) == 0
    assert not (tmp_path / ".config").exists() and "ExecStart=" in said[1]


def test_status_on_one_computer_says_so(cfg, capsys):
    assert mugil.status(cfg) == 0
    assert "only one" in capsys.readouterr().out


def test_the_command_is_there():
    done = subprocess.run([mugil.sys.executable, "-m", "ninaivu", "mugil", "--help"],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0 and "setup" in done.stdout
