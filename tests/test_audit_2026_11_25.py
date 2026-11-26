"""Fixes for the external audit of 25 November 2026 (commit 791f50c).

F1: a restore, import or second-copy restore on a drive without hard links
checked the name was free and then renamed onto it. A file made in between
was replaced. Each case here makes the name exactly in that window.
"""

import errno
import os

import pytest

from ninaivu.storage import db
from ninaivu.cloud import store as store_mod
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


# -- F3: a phone backup is on disk before the phone is told it is safe ----------

def test_a_phone_backup_is_synced_before_it_is_called_safe(app, people, monkeypatch):
    import sqlite3

    from conftest import FAMILY, login
    from ninaivu.media import phone_backup, upload_review
    from test_phone_backup import back_up, photo

    db_path = app.config["MV_CONFIG"].db_path
    events = []

    def state_now():
        look = sqlite3.connect(db_path)
        try:
            staged = look.execute("SELECT COUNT(*) FROM phone_backups WHERE state=?",
                                  (phone_backup.STAGED,)).fetchone()[0]
            held = look.execute("SELECT COUNT(*) FROM pending_uploads").fetchone()[0]
        finally:
            look.close()
        return staged, held

    real_file, real_folder = upload_review.sync_file, upload_review.sync_folder
    monkeypatch.setattr(upload_review, "sync_file",
                        lambda p: (events.append(("file", Path(p).name, state_now())),
                                   real_file(p)))
    monkeypatch.setattr(upload_review, "sync_folder",
                        lambda p: (events.append(("folder", Path(p).name, state_now())),
                                   real_folder(p)))

    data = photo()
    done = back_up(login(app.test_client(), *FAMILY), "IMG_0001.png", data)

    assert done["state"] == phone_backup.STAGED
    kinds = [(kind, name) for kind, name, _ in events]
    assert ("file", "IMG_0001.png") in kinds
    assert kinds.index(("file", "IMG_0001.png")) < [k for k, _ in kinds].index("folder")
    assert ("folder", "pending-uploads") in kinds
    assert all(seen == (0, 0) for *_, seen in events), \
        "the file was called safe before it was synced"


def test_a_sync_that_fails_does_not_call_the_backup_safe(app, people, monkeypatch):
    from conftest import FAMILY, login
    from ninaivu.media import phone_backup, upload_review
    from test_phone_backup import back_up, photo

    def broken(_path):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(upload_review, "sync_file", broken)
    done = back_up(login(app.test_client(), *FAMILY), "IMG_0002.png", photo())
    assert done["state"] == phone_backup.FAILED
    assert done["state"] not in phone_backup.SAFE


# -- F7: password hashes at OWASP's profile, older ones remade on sign-in -------

from ninaivu.server import auth  # noqa: E402

OLD_PROFILE = dict(n=2 ** 14, r=8, p=1, dklen=32)


@pytest.fixture()
def real_profile(monkeypatch):
    monkeypatch.setattr(auth, "_SCRYPT", dict(auth.PASSWORD_PROFILE))


def _old_hash(secret):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(auth, "_SCRYPT", dict(OLD_PROFILE))
    try:
        return auth.hash_password(secret)
    finally:
        monkeypatch.undo()


def test_the_profile_is_one_owasp_lists():
    profile = auth.PASSWORD_PROFILE
    assert (profile["n"], profile["r"], profile["p"]) in {(2 ** 17, 8, 1), (2 ** 14, 8, 5)}


@pytest.mark.parametrize("backend", ["hashlib", "cryptography"])
def test_new_hashes_use_it_and_old_ones_still_verify(real_profile, monkeypatch, backend):
    import hashlib
    if backend == "cryptography":
        pytest.importorskip("cryptography")
        monkeypatch.delattr(hashlib, "scrypt", raising=False)
    stored = auth.hash_password("correct horse")
    assert stored.split("$")[1:4] == ["16384", "8", "5"]
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("wrong horse", stored)
    assert not auth.needs_rehash(stored)
    old = _old_hash("correct horse")
    assert auth.verify_password("correct horse", old)
    assert auth.needs_rehash(old)


def test_a_stored_hash_cannot_ask_for_unbounded_memory(real_profile):
    huge = "scrypt$1073741824$8$1$" + "00" * 16 + "$" + "00" * 32
    odd = "scrypt$1000$8$1$" + "00" * 16 + "$" + "00" * 32
    assert not auth.verify_password("x", huge) and not auth.verify_password("x", odd)
    assert not auth.needs_rehash(huge)


def test_more_memory_than_hashlibs_default_still_verifies(real_profile):
    """N=2^17 takes 128 MiB, four times hashlib's 32 MiB default: without an
    explicit allowance it failed, and the sign-in with it."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(auth, "_SCRYPT", dict(n=2 ** 17, r=8, p=1, dklen=32))
    try:
        stored = auth.hash_password("big memory")
    finally:
        monkeypatch.undo()
    assert auth.verify_password("big memory", stored)
    assert not auth.needs_rehash(stored), "an equally strong hash is left alone"


def test_signing_in_remakes_an_older_hash(real_profile, scanned):
    _, conn, _ = scanned
    admin = auth.bootstrap_admin(conn, "dad", "a good long password", "Dad")
    old = _old_hash("a good long password")
    conn.execute("UPDATE users SET password=? WHERE id=?", (old, admin.id))
    conn.commit()

    assert auth.authenticate(conn, "dad", "wrong password entirely") is None
    assert conn.execute("SELECT password FROM users WHERE id=?",
                        (admin.id,)).fetchone()[0] == old, "a failed sign-in changed it"

    assert auth.authenticate(conn, "dad", "a good long password") is not None
    new = conn.execute("SELECT password FROM users WHERE id=?", (admin.id,)).fetchone()[0]
    assert new != old and new.split("$")[3] == "5"
    assert auth.authenticate(conn, "dad", "a good long password") is not None


def test_a_pin_is_remade_too(real_profile, scanned):
    _, conn, _ = scanned
    admin = auth.bootstrap_admin(conn, "dad", "a good long password", "Dad")
    kid = auth.create_user(conn, "kid", None, display_name="Kid",
                           role=auth.ROLE_FAMILY, created_by=admin.id)
    conn.execute("UPDATE users SET pin=? WHERE id=?", (_old_hash("2468"), kid.id))
    conn.commit()
    assert auth.verify_pin(conn, kid.id, "2468")
    assert conn.execute("SELECT pin FROM users WHERE id=?",
                        (kid.id,)).fetchone()[0].split("$")[3] == "5"


# -- F9: a same-size edit in the middle of a big file is sent again -------------

import test_cloud_migration as migration  # noqa: E402


@pytest.fixture()
def uploaded(tmp_path):
    world_ = migration.world.__wrapped__(tmp_path)
    return world_, migration.upload_everything(world_)


def _edit_the_middle(path):
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    stamp = path.stat().st_mtime + 3600
    path.write_bytes(bytes(data))
    os.utime(path, (stamp, stamp))


def test_an_edit_in_the_middle_at_the_same_size_is_sent(uploaded):
    (_, _, _, library, files, _), conn = uploaded
    big = library / "2019/07/a.jpg"                       # 200,000 bytes
    _edit_the_middle(big)
    from ninaivu.utils import source_version as sv
    row = conn.execute("SELECT source_version FROM cloud_uploads WHERE rel_path=?",
                       ("2019/07/a.jpg",)).fetchone()
    assert sv.same_content(row[0], big), "the sampled ends alone cannot see it"

    assert store_mod.queue_missing(conn, migration.assets(library, files)) == 1
    assert store_mod.state_of(conn, str(library), "2019/07/a.jpg") == store_mod.PENDING


def test_the_same_edit_is_seen_by_the_queue_of_changed_files(uploaded):
    (_, _, _, library, files, _), conn = uploaded
    _edit_the_middle(library / "2019/07/a.jpg")
    conn.execute("CREATE TEMP TABLE a_now(id INTEGER PRIMARY KEY, root, rel_path, "
                 "filename, size, mtime, kind)")
    conn.executemany("INSERT INTO a_now(root, rel_path, filename, size, mtime, kind) "
                     "VALUES(:root, :rel_path, :filename, :size, :mtime, :kind)",
                     list(migration.assets(library, files)))
    assert store_mod.queue_changed(
        conn, "SELECT id, root, rel_path, filename, size, mtime, kind FROM a_now") == 1


def test_a_copy_that_only_lost_its_timestamps_is_still_not_sent(uploaded):
    world_, conn = uploaded
    new = migration.move_without_timestamps(world_)
    assert store_mod.queue_missing(conn, migration.assets(new, world_[4])) == 0
    assert all(r[0] for r in conn.execute(
        "SELECT source_sha256 FROM cloud_uploads WHERE size > ?",
        (2 * 65536,)))


def test_an_encrypted_record_from_before_learns_the_digest_and_then_sees_edits(uploaded):
    (_, _, _, library, files, _), conn = uploaded
    # As an encrypted upload made before the file's own digest was kept: what
    # Drive holds is ciphertext, so nothing recorded speaks for the middle.
    conn.execute("UPDATE cloud_uploads SET encrypted=1, source_sha256='', digest='x', md5=''")
    conn.commit()
    big = library / "2019/07/a.jpg"
    stamp = big.stat().st_mtime + 60
    os.utime(big, (stamp, stamp))
    assert store_mod.queue_missing(conn, migration.assets(library, files)) == 0
    learned = conn.execute("SELECT source_sha256 FROM cloud_uploads WHERE rel_path=?",
                           ("2019/07/a.jpg",)).fetchone()[0]
    assert learned == files_mod.sha256_file(big)
    _edit_the_middle(big)
    assert store_mod.queue_missing(conn, migration.assets(library, files)) == 1


def test_an_encrypted_upload_records_the_files_own_digest(tmp_path):
    from ninaivu.cloud.upload_cache import UploadCache
    world_ = migration.world.__wrapped__(tmp_path)
    db_path, _, client, library, files, _ = world_
    conn = db.connect(db_path)
    store_mod.queue_missing(conn, migration.assets(library, files))
    key = os.urandom(32)
    from ninaivu.cloud import engine as engine_mod
    engine = engine_mod.SyncEngine(
        connect=lambda: client, open_db=lambda: db.connect(db_path),
        encryption=lambda: (key, b"k" * 8), cache=UploadCache(tmp_path / "cache"))
    engine.start()
    engine._thread.join(30)
    for rel in files:
        row = conn.execute("SELECT state, encrypted, source_sha256 FROM cloud_uploads "
                           "WHERE rel_path=?", (rel,)).fetchone()
        assert row[0] == store_mod.DONE and row[1] == 1, tuple(row)
        assert row[2] == files_mod.sha256_file(library / rel)
