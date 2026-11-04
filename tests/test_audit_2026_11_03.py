"""The audit of 3 November 2026, after an evening on a Linux laptop.

An update to 3.6.0 on a 2.7 GB index: the start made its copy before the
update for eight minutes saying only "copying", was stopped twice as hung,
and each stop left a 2.3-2.7 GB staging folder to wait a day. The server the
menu entry started wrote its log where the Control Panel's View logs never
looked. And the mount table's escapes were read for spaces only.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from ninaivu.storage import backup, upgrade
from ninaivu.utils import mounts

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))     # start.py lives there

import start  # noqa: E402


# -- the copy before an update says which stage it is on ------------------------

def test_the_copy_before_an_update_names_each_stage_once(tmp_path, monkeypatch):
    from ninaivu.server import starting
    state = tmp_path / "state"
    state.mkdir()
    (state / "index.db").write_bytes(b"")
    steps = []
    monkeypatch.setattr(starting, "step", steps.append)
    monkeypatch.setattr(upgrade, "schemas", lambda _: {})
    made = tmp_path / "copy.tar.gz"

    def snapshot(*_args, progress=None, **_kwargs):
        for stage in ("copying", "copying", "checking", "checking", "verifying", "packing"):
            progress(stage, files_done=0, files_total=1, bytes_done=0, bytes_total=1)
        return made

    monkeypatch.setattr(backup, "snapshot", snapshot)
    monkeypatch.setattr(backup, "prune", lambda *a, **k: None)
    upgrade.prepare(state, version="9.9.9", environ={})
    base = upgrade.UPDATE_STEP
    assert steps == [base, f"{base}: copying",
                     f"{base}: working out a checksum of every file",
                     f"{base}: checking the copied index", f"{base}: packing the copy"]


def test_every_stage_keeps_the_step_a_panel_recognises():
    told = []
    progress = upgrade._update_progress(told.append)
    progress("copying")
    progress("something new")
    assert all(step.startswith(upgrade.UPDATE_STEP) for step in told)
    assert told[-1].endswith(": something new")


# -- staging left by a backup that was stopped ----------------------------------

def _staging(state: Path, name: str, owner: int | None, age: float = 0) -> Path:
    folder = state / f"{backup.STAGING_PREFIX}{name}"
    (folder / "state").mkdir(parents=True)
    (folder / "state" / "index.db").write_bytes(b"x" * 10)
    if owner is not None:
        (folder / backup.STAGING_OWNER).write_text(str(owner), encoding="ascii")
    if age:
        then = time.time() - age
        os.utime(folder, (then, then))
    return folder


def test_staging_of_a_process_that_has_gone_is_cleared_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_process_alive", lambda pid: pid == 4242)
    gone = _staging(tmp_path, "gone", owner=999_999)
    working = _staging(tmp_path, "working", owner=4242)
    unowned = _staging(tmp_path, "from-before", owner=None)
    backup._clear_old_staging(tmp_path)
    assert not gone.exists()
    assert working.exists()             # a backup still at work keeps its copy
    assert unowned.exists()             # no owner said: the day's wait, as before


def test_staging_a_day_old_goes_whoever_made_it(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_process_alive", lambda pid: True)
    old = _staging(tmp_path, "old", owner=4242, age=2 * 86400)
    unowned = _staging(tmp_path, "older", owner=None, age=2 * 86400)
    backup._clear_old_staging(tmp_path)
    assert not old.exists() and not unowned.exists()


def test_a_staging_folder_says_whose_it_is(tmp_path, monkeypatch):
    seen = []
    real = backup._claim_staging

    def claim(folder):
        real(folder)
        seen.append((folder / backup.STAGING_OWNER).read_text(encoding="ascii"))

    monkeypatch.setattr(backup, "_claim_staging", claim)
    state = tmp_path / "state"
    state.mkdir()
    import sqlite3
    with sqlite3.connect(state / "index.db") as conn:
        conn.execute("CREATE TABLE t (x)")
    backup.snapshot(state, tmp_path / "out")
    assert seen == [str(os.getpid())]
    assert not list(state.glob(f"{backup.STAGING_PREFIX}*"))   # its own, cleaned up


def test_this_process_counts_as_alive():
    assert backup._process_alive(os.getpid())
    assert not backup._process_alive(0)


# -- the launcher's log is the one the Control Panel shows -----------------------

def test_the_launcher_logs_where_it_is_told(tmp_path):
    told = tmp_path / "logs"
    assert start.server_log_path({"NINAIVU_LOG_DIR": str(told)}) == told / "server.log"
    assert told.is_dir()


def test_the_launcher_logs_beside_the_checkout_when_not_told(monkeypatch, tmp_path):
    monkeypatch.setattr(start, "HERE", tmp_path)
    assert start.server_log_path({}) == tmp_path / ".ninaivu-control" / "server.log"
    assert start.server_log_path({"NINAIVU_LOG_DIR": "  "}) == \
        tmp_path / ".ninaivu-control" / "server.log"


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_the_launchers_log_is_its_owners_only(tmp_path):
    path = tmp_path / "server.log"
    path.write_text("from before\n", encoding="utf-8")
    os.chmod(path, 0o644)
    with start.open_private_log(path) as log:
        log.write("and now\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "from before\nand now\n"


# -- the mount table's escapes ----------------------------------------------------

def test_mount_points_are_read_with_every_escape():
    assert mounts.unescape(r"/media/jaga/Ninaivu\040Archive") == "/media/jaga/Ninaivu Archive"
    assert mounts.unescape(r"/media/jaga/a\011b\134c\012") == "/media/jaga/a\tb\\c\n"
    assert mounts.unescape("/dev/sda1") == "/dev/sda1"


def test_a_drive_with_a_backslash_in_its_name_is_found(tmp_path):
    from ninaivu.utils import drives
    table = tmp_path / "mounts"
    table.write_text("/dev/sdb1 /media/jaga/Back\\134up vfat rw 0 0\n"
                     "/dev/sdc1 /media/jaga/My\\040Photos exfat rw 0 0\n", encoding="utf-8")
    assert drives._linux_device("/media/jaga/Back\\up", str(table)) == "/dev/sdb1"
    assert drives._linux_device("/media/jaga/My Photos", str(table)) == "/dev/sdc1"
