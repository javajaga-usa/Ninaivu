"""What made CI fail on 6 November 2026, when 3.7.0 was to be released.

The browser job's ffmpeg took 29 minutes from a crawling package mirror, and
the job's limit ended it before a test ran; on the attempt before, the
'visibility' library was started with the family app and the console on one
port, because two free ports were asked for one at a time and the system gave
the same one twice.
"""

import os
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
    """An apt-get that hangs on its first *slow_installs* installs."""
    count = tmp_path / "installs"
    fake = tmp_path / "apt-get"
    fake.write_text(f"""#!/usr/bin/env bash
echo "$@" >> {tmp_path / "calls"}
if [ "$1" = install ]; then
  n=$(( $(cat {count} 2>/dev/null || echo 0) + 1 )); echo $n > {count}
  if [ $n -le {slow_installs} ]; then sleep 30; fi
fi
exit 0
""")
    fake.chmod(0o755)
    return fake


def _run(tmp_path, fake, *args):
    env = dict(os.environ, CI_APT_GET=str(fake), CI_APT_SECONDS="1", CI_APT_ATTEMPTS="3")
    began = time.monotonic()
    done = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    return done, time.monotonic() - began


def test_an_install_that_crawls_is_tried_again(tmp_path):
    done, took = _run(tmp_path, _fake_apt(tmp_path, slow_installs=1), "ffmpeg")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "trying again" in done.stdout and took < 20
    calls = (tmp_path / "calls").read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["update", "install", "install"]
    assert "ffmpeg" in calls[1] and "Acquire::Retries=3" in calls[1]


def test_a_mirror_that_never_answers_fails_the_step_and_says_why(tmp_path):
    done, took = _run(tmp_path, _fake_apt(tmp_path, slow_installs=9), "ffmpeg")
    assert done.returncode == 1 and "too slow or down" in done.stdout
    assert took < 20


def test_a_quick_mirror_installs_once(tmp_path):
    done, _ = _run(tmp_path, _fake_apt(tmp_path, slow_installs=0), "xvfb", "fonts-noto-core")
    assert done.returncode == 0 and "trying again" not in done.stdout
    assert [c.split()[0] for c in (tmp_path / "calls").read_text().splitlines()] == ["update", "install"]


def test_a_command_that_installs_through_apt_is_limited_too(tmp_path):
    flag = tmp_path / "ran"
    done, _ = _run(tmp_path, _fake_apt(tmp_path, slow_installs=0), "--run", "bash", "-c", f"echo >> {flag}")
    assert done.returncode == 0 and flag.read_text().count("\n") == 1


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
