"""Fixes for the external audit of 26 November 2026 (commit 3b20458, 5.2.0).

R1: an encrypted restore took a photograph damaged in the middle, with its
    size and timestamp kept, as already restored.
R2: the cloud queue took an encrypted upload from before 5.2 as unchanged,
    with nothing recorded to compare the middle of the file with.
R3: a Debian first start killed between making its lock and writing its pid
    blocked every later start.
R4: on a drive with neither hard links nor a no-replace rename, a restore or
    import copied straight into the file's real name, part-written until done.
R5: a phone backup was called safe after a folder sync had failed.
R6: the dependency audit installed newer versions than the release ships.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ninaivu.cloud import restore
from ninaivu.cloud import store as store_mod
from ninaivu.storage import db
from ninaivu.utils import files as files_mod
from test_cloud_restore import run, upload
from test_cloud_restore import world as world                          # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _damage_the_middle_keeping_the_timestamp(path: Path) -> bytes:
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return bytes(data)


# -- R1 ----------------------------------------------------------------------

def test_an_encrypted_restore_sees_a_middle_damaged_original(world):
    db_path, _, client, library, files, _ = world
    key = upload(world, encrypted=True)
    beach = library / "2019/07/beach.jpg"                 # over 16 MB
    damaged = _damage_the_middle_keeping_the_timestamp(beach)

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None, key=key))

    assert state["beside"] == 1 and state["already_there"] == 2, state
    assert beach.read_bytes() == damaged, "the damaged file was overwritten"
    assert (library / "2019/07/beach (restored).jpg").read_bytes() == \
        files["2019/07/beach.jpg"]


def test_an_encrypted_record_carries_the_originals_own_digest(world):
    db_path, *_ = world
    upload(world, encrypted=True)
    items = restore.from_record(db.connect(db_path))
    assert items and all(item.encrypted and item.source_sha256 for item in items)


def test_an_encrypted_record_from_before_is_checked_against_the_backup(world):
    """No digest of the original recorded (an upload before 5.2): the backup
    is fetched and compared, not the sampled ends."""
    db_path, _, client, library, files, _ = world
    key = upload(world, encrypted=True)
    conn = db.connect(db_path)
    conn.execute("UPDATE cloud_uploads SET source_sha256=''")
    conn.commit()
    beach = library / "2019/07/beach.jpg"
    _damage_the_middle_keeping_the_timestamp(beach)

    state = run(restore.RestoreJob(connect=lambda: client,
                                   items=restore.from_record(db.connect(db_path)),
                                   destination=None, key=key))

    assert state["beside"] == 1 and state["already_there"] == 2, state
    assert (library / "2019/07/beach (restored).jpg").read_bytes() == \
        files["2019/07/beach.jpg"]
    assert not (library / restore.STAGING).exists() or not any(
        (library / restore.STAGING).iterdir()), "staging was left behind"


# -- R2 ----------------------------------------------------------------------

import test_cloud_migration as migration  # noqa: E402


@pytest.fixture()
def uploaded(tmp_path):
    world_ = migration.world.__wrapped__(tmp_path)
    return world_, migration.upload_everything(world_)


def test_an_edit_before_the_first_look_at_an_old_encrypted_record_is_sent(uploaded):
    (_, _, _, library, files, _), conn = uploaded
    conn.execute("UPDATE cloud_uploads SET encrypted=1, source_sha256='', digest='x', md5=''")
    conn.commit()
    big = library / "2019/07/a.jpg"                       # 200,000 bytes
    data = bytearray(big.read_bytes())
    data[len(data) // 2] ^= 0xFF
    stamp = big.stat().st_mtime + 3600
    big.write_bytes(bytes(data))
    os.utime(big, (stamp, stamp))

    assert store_mod.queue_missing(conn, migration.assets(library, files)) == 1
    row = conn.execute("SELECT state, source_sha256 FROM cloud_uploads WHERE rel_path=?",
                       ("2019/07/a.jpg",)).fetchone()
    assert row[0] == store_mod.PENDING
    assert row[1] == "", "the edited file was written down as the one backed up"


# -- R3 ----------------------------------------------------------------------

from test_audit_2026_11_25 import _installed, _sound, _where, posix_only  # noqa: E402


@posix_only
def test_a_start_killed_before_writing_its_pid_does_not_block_the_next(tmp_path):
    app, state, env = _installed(tmp_path)
    lock = state / "runtime.lock"
    lock.mkdir(parents=True)                              # empty: no pid yet
    old = lock.stat().st_mtime - 600
    os.utime(lock, (old, old))
    done = _where(app, env)
    assert done.returncode == 0, done.stderr
    _sound(state)
    assert not lock.exists()


@posix_only
def test_without_flock_an_old_empty_lock_is_still_recovered(tmp_path):
    app, state, env = _installed(tmp_path)
    # Every program on the PATH but flock.
    tools = tmp_path / "tools"
    tools.mkdir()
    for folder in ["/usr/bin", "/bin", *os.environ.get("PATH", "").split(os.pathsep)]:
        if not folder or not Path(folder).is_dir():
            continue
        for found in Path(folder).iterdir():
            if found.name != "flock" and not (tools / found.name).exists():
                try:
                    (tools / found.name).symlink_to(found)
                except OSError:
                    pass
    env = {**env, "PATH": str(tools)}
    lock = state / "runtime.lock"
    lock.mkdir(parents=True)
    old = lock.stat().st_mtime - 600
    os.utime(lock, (old, old))
    sh = next(Path(f) / "sh" for f in os.environ["PATH"].split(os.pathsep)
              if (Path(f) / "sh").exists())
    done = subprocess.run([str(sh), str(app / "ninaivu.sh"), "--where"], env=env,
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    assert not lock.exists()


def test_the_lock_is_not_handed_on_to_ninaivu():
    script = (ROOT / "installers/linux/debian/ninaivu.sh").read_text()
    release = script[script.index("release_lock() {"):script.index("take_lock() {")]
    assert "exec 9>&-" in release


# -- R4 ----------------------------------------------------------------------

STOPPED = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r})
    from ninaivu.utils import files
    def refuse(*a, **k):
        raise OSError(1, "no hard links here")
    files.os.link = refuse
    files._rename_no_replace = lambda *a: False
    files.CHUNK = 1024
    where = {where!r}
    real = {real!r}
    def stop(*a, **k):
        os._exit(93)
    setattr(files.os, where, stop) if where != "write" else None
    if where == "write":
        import builtins
        opened = builtins.open
        class Reader:
            def __init__(self, f): self.f, self.n = f, 0
            def read(self, n):
                self.n += 1
                if self.n == 3:
                    os._exit(93)
                return self.f.read(n)
            def __enter__(self): return self
            def __exit__(self, *a): self.f.close()
        builtins.open = lambda p, m="r", *a, **k: (
            Reader(opened(p, m, *a, **k)) if m == "rb" else opened(p, m, *a, **k))
    files.publish_no_replace(sys.argv[1], sys.argv[2])
""")


def _stop_publishing(tmp_path, where):
    staged = tmp_path / "photo.part"
    staged.write_bytes(os.urandom(10_000))
    target = tmp_path / "photo.jpg"
    code = STOPPED.format(root=str(ROOT), where=where, real="")
    done = subprocess.run([sys.executable, "-c", code, str(staged), str(target)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 93, done.stderr
    return staged, target


@pytest.mark.skipif(os.name == "nt", reason="the fallback is for exFAT on macOS/Linux")
@pytest.mark.parametrize("where", ["write", "fsync"])
def test_a_copy_stopped_part_way_leaves_nothing_under_the_real_name(tmp_path, where):
    staged, target = _stop_publishing(tmp_path, where)
    assert staged.exists() and staged.stat().st_size == 10_000
    assert not target.exists(), "a part-written file is under the photograph's name"
    parts = list(tmp_path.glob(".photo.jpg.*" + files_mod.PART_SUFFIX))
    assert parts, "the part copy is under a dot-name"


@pytest.mark.skipif(os.name == "nt", reason="the fallback is for exFAT on macOS/Linux")
def test_what_a_stopped_copy_left_is_cleared_by_the_next(tmp_path, monkeypatch):
    staged, target = _stop_publishing(tmp_path, "replace")
    # Stopped in the moment between holding the name and renaming onto it.
    assert target.exists() and target.stat().st_size == 0
    for part in tmp_path.glob(".photo.jpg.*" + files_mod.PART_SUFFIX):
        old = part.stat().st_mtime - 2 * files_mod.PART_STALE_SECONDS
        os.utime(part, (old, old))

    def refuse(*_a, **_k):
        raise OSError(errno.EPERM, "no hard links here")
    monkeypatch.setattr(files_mod.os, "link", refuse)
    monkeypatch.setattr(files_mod, "_rename_no_replace", lambda *a: False)
    data = staged.read_bytes()
    files_mod.publish_no_replace(staged, target)
    assert target.read_bytes() == data and not staged.exists()
    assert not list(tmp_path.glob(".photo.jpg.*" + files_mod.PART_SUFFIX))


@pytest.mark.skipif(os.name == "nt", reason="the fallback is for exFAT on macOS/Linux")
def test_a_live_part_copy_and_a_real_empty_file_are_left_alone(tmp_path, monkeypatch):
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"")
    live = tmp_path / (".photo.jpg.abc" + files_mod.PART_SUFFIX)
    live.write_bytes(b"being written")
    staged = tmp_path / "photo.part"
    staged.write_bytes(b"RESTORED")
    monkeypatch.setattr(files_mod.os, "link",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EPERM, "x")))
    monkeypatch.setattr(files_mod, "_rename_no_replace", lambda *a: False)
    with pytest.raises(FileExistsError):
        files_mod.publish_no_replace(staged, target)
    assert live.exists() and target.exists() and staged.exists()


# -- R5 ----------------------------------------------------------------------

def test_a_folder_sync_that_fails_does_not_call_the_backup_safe(app, people, monkeypatch):
    if os.name == "nt":
        pytest.skip("Windows does not sync folders")
    from conftest import FAMILY, login
    from ninaivu.media import phone_backup
    from test_phone_backup import back_up, photo

    real = os.fsync
    hit = []

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            hit.append(fd)
            raise OSError(errno.EIO, "I/O error")
        return real(fd)
    monkeypatch.setattr(files_mod.os, "fsync", fsync)
    done = back_up(login(app.test_client(), *FAMILY), "IMG_0003.png", photo())
    assert hit, "no folder was synced"
    assert done["state"] not in phone_backup.SAFE, done


def test_a_drive_that_cannot_sync_folders_still_passes(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Windows does not sync folders")

    def unsupported(_fd):
        raise OSError(errno.EINVAL, "Invalid argument")
    monkeypatch.setattr(files_mod.os, "fsync", unsupported)
    files_mod.sync_folder(tmp_path, strict=True)


def test_by_default_a_folder_sync_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(files_mod.os, "fsync",
                        lambda _fd: (_ for _ in ()).throw(OSError(errno.EIO, "I/O")))
    files_mod.sync_folder(tmp_path)
    files_mod.sync_folder(tmp_path / "missing")


# -- R6 ----------------------------------------------------------------------

def test_the_dependency_audit_installs_the_versions_the_release_ships():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    step = workflow[workflow.index("- name: Install every requirement set"):]
    step = step[:step.index("pip freeze")]
    install = step[step.index("pip install --extra-index-url"):]
    assert "-c requirements/constraints-tested.txt" in install
    assert "-r requirements/requirements.txt" in install
