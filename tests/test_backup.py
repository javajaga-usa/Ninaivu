"""The index gets copied without anybody remembering to do it.

The photographs are protected by Archive and Cloud. The index is the other
half — names on faces, albums, visibility, share links, corrected dates — and
until this existed the only thing that copied it was a command-line script
somebody had to run.
"""

import json
import tarfile
import time
from pathlib import Path

import pytest

from ninaivu.storage import backup, db


@pytest.fixture()
def state(tmp_path):
    """A state directory with a real index and the odds and ends beside it."""
    state = tmp_path / "state"
    state.mkdir()
    conn = db.init_db(state / "index.db")
    conn.execute(
        "INSERT INTO assets(id, root, rel_path, filename, folder, ext, kind, size, "
        "mtime, date_key) VALUES(1,'/lib','a.jpg','a.jpg','','jpg','picture',1,1,'2026-01-01')")
    conn.commit()
    (state / "config.json").write_text('{"roots": ["/lib"]}', encoding="utf-8")
    (state / "avatars").mkdir()
    (state / "avatars" / "dad.png").write_bytes(b"not really a png")
    return state


def test_a_bundle_holds_the_index_and_the_settings(state, tmp_path):
    made = backup.snapshot(state, tmp_path / "out")
    assert made and made.exists()

    with tarfile.open(made) as tar:
        names = tar.getnames()
    assert "state/index.db" in names
    assert "state/config.json" in names
    assert "state/avatars/dad.png" in names
    assert "state/backup_manifest.json" in names


def test_the_copy_of_the_index_is_a_working_database(state, tmp_path):
    """The point of the online backup API: a snapshot taken while the library
    is in use is still a database, not a half-written file."""
    made = backup.snapshot(state, tmp_path / "out")
    with tarfile.open(made) as tar:
        tar.extract("state/index.db", tmp_path / "unpacked", filter="data")

    conn = db.connect(tmp_path / "unpacked" / "state" / "index.db")
    assert conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"] == 1


def test_every_file_is_checksummed(state, tmp_path):
    import json

    made = backup.snapshot(state, tmp_path / "out")
    with tarfile.open(made) as tar:
        manifest = json.loads(
            tar.extractfile("state/backup_manifest.json").read().decode("utf-8"))
    assert manifest["files"]["index.db"]["sha256"]
    assert manifest["files"]["config.json"]["size"] > 0


def test_same_second_backups_do_not_replace_each_other(state, tmp_path, monkeypatch):
    from datetime import datetime

    class FrozenDatetime:
        @staticmethod
        def now():
            return datetime(2026, 9, 9, 12, 0, 0)

    monkeypatch.setattr(backup, "datetime", FrozenDatetime)
    out = tmp_path / "out"
    first = backup.snapshot(state, out)
    original = first.read_bytes()
    (state / "config.json").write_text('{"changed": true}', encoding="utf-8")
    second = backup.snapshot(state, out)
    assert first != second
    assert first.read_bytes() == original
    assert len(backup.bundles(out)) == 2


def test_failed_bundle_is_never_published(state, tmp_path, monkeypatch):
    out = tmp_path / "out"
    original = backup.snapshot(state, out)
    original_bytes = original.read_bytes()

    def interrupted(self, *args, **kwargs):
        assert [item["name"] for item in backup.bundles(out)] == [original.name]
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "add", interrupted)
    assert backup.snapshot(state, out) is None
    assert original.read_bytes() == original_bytes
    assert list(out.iterdir()) == [original]


def test_only_the_newest_are_kept(state, tmp_path):
    out = tmp_path / "out"
    for _ in range(3):
        assert backup.snapshot(state, out)
        time.sleep(1.05)          # the name carries whole seconds
    assert len(backup.bundles(out)) == 3

    removed = backup.prune(out, keep=2)
    assert len(removed) == 1
    kept = backup.bundles(out)
    assert len(kept) == 2
    assert kept[0]["at"] >= kept[1]["at"], "prune kept the wrong ones"


def test_prune_touches_nothing_else(state, tmp_path):
    out = tmp_path / "out"
    backup.snapshot(state, out)
    stranger = out / "holiday-photos.tar.gz"
    stranger.write_bytes(b"not ours")

    backup.prune(out, keep=0)
    assert stranger.exists(), "prune removed a file it did not write"


def test_a_quiet_library_is_not_copied_again(state, cfg_for):
    """Seven identical copies of a library nobody touched is not a backup
    policy, it is a disk filling up."""
    keeper = backup.BackupKeeper(cfg_for(state, every_hours=24))
    assert keeper.due() is True
    keeper.run()
    assert keeper.due() is False, "a second copy was due immediately"


def test_a_changed_index_is_copied_again(state, cfg_for):
    keeper = backup.BackupKeeper(cfg_for(state, every_hours=0.0001))
    keeper.run()
    time.sleep(0.5)
    (state / "index.db").touch()
    assert keeper.due() is True


@pytest.mark.parametrize("changed", [
    "index.db-wal", "archive.db-wal", "config.json", "avatars/dad.png",
])
def test_backup_detects_wal_and_settings_changes(state, cfg_for, monkeypatch, changed):
    import os

    keeper = backup.BackupKeeper(cfg_for(state, every_hours=1))
    baseline = time.time() + 100
    monkeypatch.setattr(backup, "latest", lambda folder: {"at": baseline})
    monkeypatch.setattr(backup.time, "time", lambda: baseline + 7200)
    assert keeper.due() is False
    path = state / changed
    path.touch()
    os.utime(path, (baseline + 1, baseline + 1))
    assert keeper.due() is True


def test_a_backup_not_yet_due_is_looked_at_again_soon(state, cfg_for, monkeypatch):
    """The loop checked 15 minutes after start and, finding nothing due, then
    slept the whole interval. A server restarted every day never reached the
    second look: on the machine this was found on, a day's backup slid to two."""
    keeper = backup.BackupKeeper(cfg_for(state, every_hours=24))
    waits, answers, ran = [], iter([False, True]), []

    class Clock:
        def wait(self, seconds):
            waits.append(seconds)
            return len(waits) > 3                 # stop after a few rounds

        def set(self):
            pass

        def clear(self):
            pass

    keeper._stop = Clock()
    monkeypatch.setattr(keeper, "due", lambda: next(answers, False))
    monkeypatch.setattr(keeper, "run", lambda: ran.append(1))
    keeper._loop()
    assert max(waits) <= 900, f"waited {max(waits) / 3600:.0f} h between looks"
    assert ran == [1], "a backup that fell due was not made at the next look"


def test_turning_it_off_while_it_runs_does_not_spin(state, cfg_for):
    """The console can set the interval to 0 while the loop runs. A wait of
    0 returns at once, so the loop span, holding a core until a restart."""
    cfg = cfg_for(state, every_hours=24)
    keeper = backup.BackupKeeper(cfg)
    cfg.backup_every_hours = 0
    waits = []

    class Clock:
        def wait(self, seconds):
            waits.append(seconds)
            return len(waits) > 2

    keeper._stop = Clock()
    keeper._loop()
    assert min(waits) >= 60, f"waited {min(waits)} s between looks"


def test_turning_it_off_means_off(state, cfg_for):
    keeper = backup.BackupKeeper(cfg_for(state, every_hours=0))
    assert keeper.due() is False
    keeper.start()
    assert keeper._thread is None, "a disabled keeper started a thread"


def test_the_state_the_console_shows(state, cfg_for):
    keeper = backup.BackupKeeper(cfg_for(state, every_hours=24))
    empty = keeper.state()
    assert empty["last"] is None and empty["count"] == 0

    keeper.run()
    now = keeper.state()
    assert now["count"] == 1 and now["last"]["bytes"] > 0
    assert now["error"] is None


@pytest.fixture()
def cfg_for(tmp_path):
    """A config object with just the fields the keeper reads."""
    class Fake:
        def __init__(self, state, every_hours):
            self.state_dir = state
            self.backup_dir = str(tmp_path / "out")
            self.backup_every_hours = every_hours
            self.backup_keep = 7

    return lambda state, every_hours=24: Fake(state, every_hours)


def test_backups_on_the_drive_they_protect_are_flagged(state, cfg_for, tmp_path):
    """Everything in tmp_path is one drive, which is exactly the default setup:
    the backups sit beside the index they are meant to survive."""
    cfg = cfg_for(state)
    cfg.roots = [str(tmp_path / "lib")]
    (tmp_path / "lib").mkdir()
    shared = backup.BackupKeeper(cfg).state()["same_drive_as"]
    assert shared == ["the library index", "the photo library"]


def test_backups_on_another_drive_are_not_flagged(state, cfg_for, tmp_path, monkeypatch):
    cfg = cfg_for(state)
    out = tmp_path / "out"
    real = backup._device
    monkeypatch.setattr(backup, "_device",
                        lambda path: 9999 if Path(path) == out else real(path))
    assert backup.BackupKeeper(cfg).state()["same_drive_as"] == []


def test_a_drive_that_cannot_be_identified_is_not_called_the_same(tmp_path, monkeypatch):
    """Some network and virtual filesystems report device 0 for everything."""
    from types import SimpleNamespace
    monkeypatch.setattr(backup, "os", SimpleNamespace(stat=lambda _p: SimpleNamespace(st_dev=0)))
    assert backup._device(tmp_path) is None


def test_backup_settings_survive_a_settings_save(tmp_path, monkeypatch):
    """They are read from config.json, so saving settings must not drop them."""
    from ninaivu.server.config import Config
    monkeypatch.delenv("NINAIVU_BACKUP_DIR", raising=False)
    monkeypatch.delenv("NINAIVU_BACKUP_KEEP", raising=False)
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.backup_dir, cfg.backup_keep, cfg.backup_every_hours = str(tmp_path / "elsewhere"), 3, 6.0
    cfg.save()
    saved = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    assert (saved["backup_dir"], saved["backup_keep"], saved["backup_every_hours"]) == \
        (str(tmp_path / "elsewhere"), 3, 6.0)


def test_every_new_backup_is_test_restored(state, cfg_for):
    keeper = backup.BackupKeeper(cfg_for(state))
    made = keeper.run()
    verified = keeper.state()["verified"]
    assert verified["ok"] and verified["bundle"] == made.name, verified
    assert verified["assets"] == 1


def test_a_damaged_backup_fails_its_restore_check(state, cfg_for, tmp_path):
    keeper = backup.BackupKeeper(cfg_for(state))
    made = keeper.run()
    # Rewrite the bundle with the index changed after its checksum was taken.
    work = tmp_path / "unpacked"
    with tarfile.open(made, "r:gz") as tar:
        tar.extractall(work)
    index = work / "state" / "index.db"
    index.write_bytes(index.read_bytes()[:-1] + b"X")
    with tarfile.open(made, "w:gz") as tar:
        tar.add(work / "state", arcname="state")

    result = keeper.verify_latest()
    assert not result["ok"] and "mismatch" in result["error"], result
    assert keeper.state()["verified"]["ok"] is False, "the failure was not recorded"


def test_a_bundle_that_fails_its_check_does_not_displace_a_good_one(state, cfg_for,
                                                                    monkeypatch):
    """Pruning ran before the new bundle had verified. With keep=1 that traded
    the only known-good backup for one that then failed its restore check."""
    cfg = cfg_for(state)
    cfg.backup_keep = 1
    keeper = backup.BackupKeeper(cfg)
    good = keeper.run()
    assert good is not None and keeper.state()["verified"]["ok"]
    time.sleep(1.05)                                   # the name carries whole seconds

    # The state changes, so a new bundle is due — and this one lands damaged.
    (state / "config.json").write_text('{"roots": ["/lib", "/more"]}', encoding="utf-8")
    real_verify = backup.verify_bundle

    def damaged(archive):
        result = real_verify(archive)
        return dict(result, ok=False, error="checksum mismatch (simulated)")

    monkeypatch.setattr(backup, "verify_bundle", damaged)
    bad = keeper.run()
    assert bad is not None and bad != good

    names = [b["name"] for b in backup.bundles(keeper.folder)]
    assert names == [good.name], (
        f"the good bundle should be the one left, not {names}")
    assert not bad.exists(), "the bundle that failed its check was kept"
    assert keeper.last_error and "did not verify" in keeper.last_error


def test_the_check_refuses_unsafe_archives(tmp_path):
    evil = tmp_path / "ninaivu_backup_evil.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="../../outside.txt")
    result = backup.verify_bundle(evil)
    assert not result["ok"] and "unsafe" in result["error"]
    assert not (tmp_path.parent / "outside.txt").exists()


def test_there_is_nothing_to_check_before_the_first_backup(state, cfg_for):
    result = backup.BackupKeeper(cfg_for(state)).verify_latest()
    assert result["ok"] is False and "no backup" in result["error"]


def test_the_console_can_run_a_restore_check(app, people):
    from conftest import ADMIN, FAMILY, login
    admin = login(app.test_client(), *ADMIN)
    if app.config.get("MV_BACKUPS") is None:
        pytest.skip("backups are not configured in this app")
    assert admin.post("/api/admin/backups/now").status_code == 200
    result = admin.post("/api/admin/backups/verify").get_json()
    assert result["verified"]["ok"], result
    assert result["verified"]["assets"] > 0
    family = login(app.test_client(), *FAMILY)
    assert family.post("/api/admin/backups/verify").status_code in (401, 403, 404)
