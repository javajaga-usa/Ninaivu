"""What made CI fail on 6 November 2026, when 3.7.0 was to be released.

The browser job's ffmpeg took 29 minutes from a crawling package mirror, and
the job's limit ended it before a test ran; on the attempt before, the
'visibility' library was started with the family app and the console on one
port, because two free ports were asked for one at a time and the system gave
the same one twice.
"""

import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "ci_apt_install.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash")
                                or not shutil.which("timeout"),
                                reason="the script is for the Linux runners")


def _fake_apt(tmp_path: Path, slow_installs: int) -> Path:
    """An apt-get that hangs on its first *slow_installs* installs, and, like
    the real one, will not install while another holds its lock.

    Named for this test alone (``pkill -x`` in the script goes by the name),
    and run by bash itself, so the name is the one a process list shows.
    """
    count, lock = tmp_path / "installs", tmp_path / "lockpid"
    fake = tmp_path / f"fakeapt{secrets.token_hex(3)}"
    fake.write_text(f"""#!/bin/bash
echo "$@" >> {tmp_path / "calls"}
if [ "$1" = install ]; then
  if [ -f {lock} ] && kill -0 "$(cat {lock})" 2>/dev/null; then
    echo "E: Could not get lock /var/lib/dpkg/lock-frontend"; exit 100
  fi
  echo $$ > {lock}
  n=$(( $(cat {count} 2>/dev/null || echo 0) + 1 )); echo $n > {count}
  if [ $n -le {slow_installs} ]; then sleep 30; fi
fi
exit 0
""")
    fake.chmod(0o755)
    return fake


MIRRORS = ("http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
           "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
           "https://security.ubuntu.com/ubuntu/\tpriority:3\n")


def _run(tmp_path, fake, *args, **env):
    (tmp_path / "mirrors").write_text(MIRRORS)
    env = dict(os.environ, CI_APT_GET=str(fake), CI_APT_SECONDS="1", CI_APT_ATTEMPTS="3",
               CI_APT_CONF=str(tmp_path / "apt.conf"),
               CI_APT_MIRRORS=str(tmp_path / "mirrors"), **env)
    began = time.monotonic()
    done = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    return done, time.monotonic() - began


def test_an_install_that_crawls_is_tried_again(tmp_path):
    done, took = _run(tmp_path, _fake_apt(tmp_path, slow_installs=1), "ffmpeg")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "did not finish in 1s" in done.stdout and took < 20
    calls = (tmp_path / "calls").read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["update", "install", "install"]
    assert "ffmpeg" in calls[1]
    # apt's own settings, for every apt-get on the runner, Playwright's too.
    settings = (tmp_path / "apt.conf").read_text()
    assert 'Acquire::Retries "3"' in settings and 'DPkg::Lock::Timeout "300"' in settings


def test_the_next_attempt_asks_another_mirror_first(tmp_path):
    done, _ = _run(tmp_path, _fake_apt(tmp_path, slow_installs=1), "ffmpeg")
    assert done.returncode == 0, done.stdout + done.stderr
    lines = (tmp_path / "mirrors").read_text().splitlines()
    assert lines == ["https://archive.ubuntu.com/ubuntu/\tpriority:1",
                     "https://security.ubuntu.com/ubuntu/\tpriority:2",
                     "http://azure.archive.ubuntu.com/ubuntu/\tpriority:3"]
    assert "asking https://archive.ubuntu.com/ubuntu/ first" in done.stdout


def test_a_quick_install_leaves_the_mirrors_as_they_were(tmp_path):
    done, _ = _run(tmp_path, _fake_apt(tmp_path, slow_installs=0), "ffmpeg")
    assert done.returncode == 0 and (tmp_path / "mirrors").read_text() == MIRRORS


def test_an_apt_get_left_behind_does_not_hold_the_lock_for_the_next_attempt(tmp_path):
    """6 November 2026, later: Playwright's apt-get, under sudo in a process
    of its own, outlived the limit, and the next two attempts found the lock
    taken and failed in two seconds each."""
    fake = _fake_apt(tmp_path, slow_installs=1)
    # bash forks for the install (the '; true'), as Playwright does: the
    # limit ends bash, not the apt-get it started.
    done, took = _run(tmp_path, fake, "--run", "bash", "-c", f"{fake} install libwpe; true")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Could not get lock" not in done.stdout and took < 25
    assert (tmp_path / "installs").read_text().strip() == "2"


def test_a_failure_is_not_called_a_timeout(tmp_path):
    done, _ = _run(tmp_path, _fake_apt(tmp_path, slow_installs=0), "--run", "false")
    assert done.returncode == 1
    assert "failed with exit 1" in done.stdout and "did not finish in" not in done.stdout


def test_a_mirror_that_never_answers_fails_the_step_and_says_why(tmp_path):
    done, took = _run(tmp_path, _fake_apt(tmp_path, slow_installs=9), "ffmpeg")
    assert done.returncode == 1 and "too slow or down" in done.stdout
    assert took < 30


def test_a_quick_mirror_installs_once(tmp_path):
    done, _ = _run(tmp_path, _fake_apt(tmp_path, slow_installs=0), "xvfb", "fonts-noto-core")
    assert done.returncode == 0 and "trying again" not in done.stdout
    assert [c.split()[0] for c in (tmp_path / "calls").read_text().splitlines()] == ["update", "install"]


def test_a_command_that_installs_through_apt_is_limited_too(tmp_path):
    flag = tmp_path / "ran"
    done, _ = _run(tmp_path, _fake_apt(tmp_path, slow_installs=0), "--run", "bash", "-c", f"echo >> {flag}")
    assert done.returncode == 0 and flag.read_text().count("\n") == 1


def test_packages_are_kept_where_the_workflow_caches_them(tmp_path):
    cache = tmp_path / "kept"
    done, _ = _run(tmp_path, _fake_apt(tmp_path, slow_installs=0), "ffmpeg", CI_APT_CACHE=str(cache))
    assert done.returncode == 0
    assert f'Dir::Cache::Archives "{cache}/";' in (tmp_path / "apt.conf").read_text()
    assert (cache / "partial").is_dir()
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    assert "path: ~/.cache/ci-apt/*.deb" in workflow
    assert 'CI_APT_CACHE="$HOME/.cache/ci-apt"' in workflow


def test_the_workflows_install_through_it():
    tests = (ROOT / ".github/workflows/tests.yml").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "sudo apt-get" not in tests and "sudo apt-get" not in release
    assert tests.count("tools/ci_apt_install.sh") >= 2
    assert os.access(SCRIPT, os.X_OK)


def test_the_browser_runner_asks_for_two_different_ports():
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "tools"))
    import check_bring_across
    import check_portable
    import run_browser_tests
    for _ in range(50):
        for module in (run_browser_tests, check_portable, check_bring_across):
            first, second = (module.find_free_ports(2) if hasattr(module, "find_free_ports")
                             else module.free_ports(2))
            assert first != second
