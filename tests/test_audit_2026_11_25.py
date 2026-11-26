"""Fixes for the external audit of 25 November 2026 (commit 791f50c).

F1: a restore, import or second-copy restore on a drive without hard links
checked the name was free and then renamed onto it. A file made in between
was replaced. Each case here makes the name exactly in that window.
"""

import errno
import os

import pytest

from ninaivu.storage import db
from ninaivu.cloud import restore
from ninaivu.storage import importer as importer_mod
from ninaivu.utils import files as files_mod

from test_cloud_restore import lose_the_library, run, upload, world  # noqa: F401


def _no_links(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError(errno.EPERM, "Operation not permitted (exFAT)")
    monkeypatch.setattr(files_mod.os, "link", refuse)


def _taken_in_the_window(monkeypatch, make, *, atomic_rename=True):
    """Make the destination just before the no-hard-link step runs. With
    *atomic_rename* False the system also has no rename that refuses a
    name (exFAT on macOS), so the exclusive copy is what is tested."""
    real = files_mod._rename_no_replace
    done = []

    def racing(src, dst):
        if not done:
            done.append(dst)
            make(dst)
        return real(src, dst) if atomic_rename else False
    monkeypatch.setattr(files_mod, "_rename_no_replace", racing)
    return done


def _new_original(path):
    with open(path, "wb") as handle:
        handle.write(b"NEW ORIGINAL")


# -- the primitive ----------------------------------------------------------

@pytest.mark.parametrize("atomic_rename", [True, False])
@pytest.mark.parametrize("kind", ["file", "folder", "link"])
def test_a_name_taken_in_the_window_is_never_replaced(tmp_path, monkeypatch,
                                                       atomic_rename, kind):
    if kind == "link" and os.name == "nt":
        pytest.skip("symlinks need privileges on Windows")
    staged = tmp_path / "photo.part"
    staged.write_bytes(b"RESTORED")
    target = tmp_path / "photo.jpg"
    elsewhere = tmp_path / "elsewhere.jpg"

    def make(dst):
        if kind == "file":
            _new_original(dst)
        elif kind == "folder":
            os.mkdir(dst)
        else:
            os.symlink(elsewhere, dst)          # dangling: never written through

    _no_links(monkeypatch)
    _taken_in_the_window(monkeypatch, make, atomic_rename=atomic_rename)
    with pytest.raises(FileExistsError):
        files_mod.publish_no_replace(staged, target)

    assert staged.read_bytes() == b"RESTORED", "the staged copy was lost"
    if kind == "file":
        assert target.read_bytes() == b"NEW ORIGINAL"
    elif kind == "folder":
        assert target.is_dir() and not any(target.iterdir())
    else:
        assert target.is_symlink() and not elsewhere.exists()


@pytest.mark.parametrize("atomic_rename", [True, False])
def test_without_hard_links_a_free_name_still_gets_the_file(tmp_path, monkeypatch,
                                                            atomic_rename):
    staged = tmp_path / "photo.part"
    staged.write_bytes(b"RESTORED")
    os.utime(staged, (1_262_304_000, 1_262_304_000))
    target = tmp_path / "photo.jpg"
    _no_links(monkeypatch)
    if not atomic_rename:
        monkeypatch.setattr(files_mod, "_rename_no_replace", lambda *a: False)
    files_mod.publish_no_replace(staged, target)
    assert target.read_bytes() == b"RESTORED" and not staged.exists()
    assert abs(target.stat().st_mtime - 1_262_304_000) < 2


def test_a_copy_that_fails_part_way_leaves_nothing_and_keeps_the_staged_file(
        tmp_path, monkeypatch):
    staged = tmp_path / "photo.part"
    staged.write_bytes(b"RESTORED" * 1000)
    target = tmp_path / "photo.jpg"
    _no_links(monkeypatch)
    monkeypatch.setattr(files_mod, "_rename_no_replace", lambda *a: False)
    monkeypatch.setattr(files_mod.os, "fsync",
                        lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")))
    with pytest.raises(OSError):
        files_mod.publish_no_replace(staged, target)
    assert not target.exists() and staged.exists()


# -- the restore, the import and the second copy -------------------------------

@pytest.mark.parametrize("atomic_rename", [True, False])
def test_a_restore_never_replaces_a_photograph_imported_meanwhile(world, monkeypatch,  # noqa: F811
                                                                  atomic_rename):
    db_path, _, client, library, files, _ = world
    upload(world)
    lose_the_library(library)
    items = [i for i in restore.from_record(db.connect(db_path))
             if i.rel_path == "2021/garden.jpg"]
    _no_links(monkeypatch)
    _taken_in_the_window(monkeypatch, _new_original, atomic_rename=atomic_rename)

    state = run(restore.RestoreJob(connect=lambda: client, items=items,
                                   destination=None))

    assert (library / "2021/garden.jpg").read_bytes() == b"NEW ORIGINAL"
    assert (library / "2021/garden (restored).jpg").read_bytes() == files["2021/garden.jpg"]
    assert state["failed"] == 0 and state["beside"] == 1


@pytest.mark.parametrize("atomic_rename", [True, False])
def test_an_import_takes_the_next_name_when_one_appears_meanwhile(tmp_path, monkeypatch,
                                                                  atomic_rename):
    partial = tmp_path / "x.part"
    partial.write_bytes(b"IMPORTED")
    _no_links(monkeypatch)
    _taken_in_the_window(monkeypatch, _new_original, atomic_rename=atomic_rename)
    target = importer_mod._publish(partial, tmp_path / "a.jpg")
    assert (tmp_path / "a.jpg").read_bytes() == b"NEW ORIGINAL"
    assert target.name == "a (2).jpg" and target.read_bytes() == b"IMPORTED"


def test_a_date_change_without_hard_links_never_replaces_a_file_made_meanwhile(
        tmp_path, monkeypatch):
    from ninaivu.media import date_edit
    source = tmp_path / "a.jpg"
    source.write_bytes(b"MOVED")
    target = tmp_path / "b.jpg"
    _no_links(monkeypatch)
    _taken_in_the_window(monkeypatch, _new_original)
    with pytest.raises(FileExistsError):
        date_edit._move(source, target)
    assert target.read_bytes() == b"NEW ORIGINAL" and source.read_bytes() == b"MOVED"


# -- F2: the Debian launcher's first start -------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX package launcher")

#: A stand-in for the package's private Python whose venv shows bin/python3
#: early and finishes later, as CPython's does: first a bare venv without pip
#: or system site-packages, then a pause, then the real one.
SLOW_VENV = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    for last; do :; done
    "{real}" -m venv --without-pip "$last"
    : > "{started}"
    sleep 3
fi
exec "{real}" "$@"
"""


def _installed(tmp_path, *, slow=False):
    app = tmp_path / "installed/app"
    (app / "python/bin").mkdir(parents=True)
    if slow:
        wrapper = app / "python/bin/python3"
        wrapper.write_text(SLOW_VENV.format(real=sys.executable,
                                            started=tmp_path / "started"))
        wrapper.chmod(0o755)
    else:
        (app / "python/bin/python3").symlink_to(sys.executable)
    shutil.copyfile(ROOT / "installers/linux/debian/ninaivu.sh", app / "ninaivu.sh")
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("NINAIVU_")}
    env.update(HOME=str(home))
    return app, home / ".ninaivu", env


def _where(app, env, **kwargs):
    return subprocess.run(["sh", str(app / "ninaivu.sh"), "--where"], env=env,
                          capture_output=True, text=True, timeout=180, **kwargs)


def _sound(state):
    runtime = state / "runtime"
    assert "include-system-site-packages = true" in (runtime / "pyvenv.cfg").read_text()
    assert (runtime / ".ninaivu-ready").exists()
    subprocess.check_call([str(runtime / "bin/python3"), "-c", "import pip"], timeout=60)


@posix_only
def test_two_first_starts_take_turns(tmp_path):
    app, state, env = _installed(tmp_path, slow=True)
    first = subprocess.Popen(["sh", str(app / "ninaivu.sh"), "--where"], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 60
    while not (tmp_path / "started").exists():
        assert time.monotonic() < deadline and first.poll() is None, first.stderr.read()
        time.sleep(0.05)
    assert (state / "runtime/bin/python3").exists(), "the half-made venv is showing"
    second = _where(app, env)
    # Before the fix the second start saw bin/python3 and went on at once.
    assert first.poll() is not None, "the second start did not wait for the first"
    assert first.wait(60) == 0 and second.returncode == 0, second.stderr
    _sound(state)


@posix_only
def test_a_start_interrupted_before_the_venv_was_finished_is_repaired(tmp_path):
    app, state, env = _installed(tmp_path)
    runtime = state / "runtime"
    state.mkdir()
    # What CPython leaves when it is killed while setting up pip: the
    # interpreter is there, system site-packages are not switched on yet.
    subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", str(runtime)])
    assert "include-system-site-packages = false" in (runtime / "pyvenv.cfg").read_text()
    assert _where(app, env).returncode == 0
    _sound(state)


@posix_only
def test_a_missing_pip_and_a_stale_marker_are_repaired_keeping_extras(tmp_path):
    app, state, env = _installed(tmp_path)
    assert _where(app, env).returncode == 0
    runtime = state / "runtime"
    extra = runtime / "keep-extra"
    extra.touch()
    for pip in runtime.glob("lib/python*/site-packages/pip"):
        shutil.rmtree(pip)
    assert _where(app, env).returncode == 0
    _sound(state)
    assert extra.exists(), "repairing threw away Extras"

    # A marker from another Python (the package moved to a new minor version).
    (runtime / ".ninaivu-ready").write_text(f"3.0 {app}\n")
    assert _where(app, env).returncode == 0
    minor = "%d.%d" % sys.version_info[:2]
    assert (runtime / ".ninaivu-ready").read_text().split()[0] == minor
    assert extra.exists()


@posix_only
def test_a_lock_left_by_a_killed_start_does_not_block_the_next(tmp_path):
    app, state, env = _installed(tmp_path)
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock = state / "runtime.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(dead.pid))
    assert _where(app, env).returncode == 0
    _sound(state)
    assert not lock.exists()
