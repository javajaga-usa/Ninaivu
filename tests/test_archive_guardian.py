import shutil
from pathlib import Path

import pytest

from ninaivu import archive
from ninaivu.archive import database as db
from ninaivu.archive.guardian import ArchiveGuardian


@pytest.fixture(autouse=True)
def plenty_of_room(monkeypatch):
    """Pretend the disk is half empty, whatever the machine running this has.

    The guardian reports a warning below ten per cent free, which is correct
    and is somebody else's test. Here it means a suite that passes on a fresh
    laptop and fails on a full one — and a check that fails for reasons the
    commit had nothing to do with is how a red build stops meaning anything.
    """
    half = shutil.disk_usage("/")._replace(total=1000, used=500, free=500)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: half)


def recorded_archive(tmp_path: Path):
    archive.configure(tmp_path / 'state')
    db.close_db()
    db.init_db()
    root = tmp_path / 'Master'
    root.mkdir()
    source = tmp_path / 'source.jpg'
    source.write_bytes(b'family-photo')
    target = root / '2024' / '01' / '02' / 'source.jpg'
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    import hashlib
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    db.save_settings([str(tmp_path)], str(root), {'image'})
    job = db.create_job([str(tmp_path)], str(root), 'YYYY/MM/DD')
    db.claim_file(str(source), source.name, target.stat().st_size,
                  source.stat().st_mtime, job, destination_root=str(root))
    db.set_status(str(source), 'verified', file_hash=digest, dest_hash=digest,
                  destination_path=str(target))
    return root, target


def test_guardian_samples_without_changing_archive_rows(tmp_path):
    _, _ = recorded_archive(tmp_path)
    before = db.get_stats().copy()
    guardian = ArchiveGuardian(lambda: False, sample_size=8)
    state = guardian.run_once()
    assert state['status'] == 'healthy'
    assert state['checked'] == state['matched'] == 1
    assert db.get_stats() == before


def test_guardian_detects_change_and_only_increments_alert_on_change(tmp_path):
    _, target = recorded_archive(tmp_path)
    guardian = ArchiveGuardian(lambda: False, sample_size=8)
    healthy = guardian.run_once()
    again = guardian.run_once()
    assert again['change_id'] == healthy['change_id']

    target.write_bytes(b'corrupted')
    damaged = guardian.run_once()
    assert damaged['status'] == 'critical'
    assert damaged['mismatched'] == 1
    assert damaged['change_id'] == healthy['change_id'] + 1


def test_guardian_does_not_start_during_archive_run(tmp_path):
    recorded_archive(tmp_path)
    guardian = ArchiveGuardian(lambda: True)
    assert guardian.request_check() is False


def test_guardian_recovers_after_database_is_temporarily_unavailable(tmp_path,monkeypatch):
    import sqlite3
    recorded_archive(tmp_path)
    guardian=ArchiveGuardian(lambda:False)
    original=db.load_settings
    def unavailable():raise sqlite3.OperationalError('database unavailable')
    monkeypatch.setattr(db,'load_settings',unavailable)
    state=guardian.run_once()
    assert state['status']=='warning'
    assert 'could not complete' in state['message']
    assert not guardian.snapshot()['running']
    monkeypatch.setattr(db,'load_settings',original)
    assert guardian.run_once()['status']=='healthy'
