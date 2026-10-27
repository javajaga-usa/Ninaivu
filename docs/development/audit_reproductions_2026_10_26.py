"""Characterizations of defects found in the 2026-10-26 audit, inverted now
that they are fixed (tests/test_audit_2026_10_27.py has them in the suite).

They passed while the defects existed; they now pass because they are gone.
A-05 has no characterization here: its test checks the source.
Run explicitly with PYTHONPATH=.:tests and pytest -p conftest.
All data is synthetic. Disk-space readings and media decoding are stubbed in
the upload test so it never fills a real disk.
"""
import io
import shutil
import time
from types import SimpleNamespace

from werkzeug.datastructures import FileStorage
from ninaivu.server import auth
from ninaivu.storage import db
from ninaivu.replica.receive import carry_sessions
from ninaivu.replica.publish import Publisher
from ninaivu.media import upload_review, phone_backup


def test_replaced_account_inherits_previous_replica_session(tmp_path):
    old_path, new_path = tmp_path / 'old.db', tmp_path / 'new.db'
    old = db.init_db(old_path)
    auth.init_auth_schema(old)
    auth.create_user(old, 'owner', 'ownerpassword1', role=auth.ROLE_ADMIN)
    original = auth.create_user(old, 'original', 'originalpass1', role=auth.ROLE_FAMILY)
    token, _ = auth.start_session(old, original.id)
    new = db.init_db(new_path)
    old.backup(new)
    auth.delete_user(new, original.id)
    replacement = auth.create_user(new, 'replacement', 'replacementpass1', role=auth.ROLE_ADMIN)
    assert original.id == replacement.id
    db.close_all()
    carry_sessions(old_path, new_path)
    new = db.connect(new_path)
    assert auth.session_user(new, token) is None
    print('FIXED: old family token is not carried to the replacement account')


def test_password_reset_does_not_revoke_replica_session(tmp_path):
    old_path, new_path = tmp_path / 'old.db', tmp_path / 'new.db'
    old = db.init_db(old_path)
    auth.init_auth_schema(old)
    user = auth.create_user(old, 'maya', 'originalpass1', role=auth.ROLE_FAMILY)
    token, _ = auth.start_session(old, user.id)
    new = db.init_db(new_path)
    old.backup(new)
    auth.set_password(new, user.id, 'replacementpass1')
    auth.end_all_sessions(new, user.id)
    db.close_all()
    carry_sessions(old_path, new_path)
    assert auth.session_user(db.connect(new_path), token) is None
    print('FIXED: pre-reset replica token ends with the new credentials')


def test_settings_change_does_not_trigger_publication(tmp_path):
    (tmp_path / 'share').mkdir()
    cfg = SimpleNamespace(state_dir=tmp_path, db_path=tmp_path / 'index.db',
                          replica_dir=str(tmp_path / 'share'), replica_every_minutes=1,
                          open_browsing=True)
    cfg.db_path.write_bytes(b'index')
    publisher = Publisher(cfg)
    publisher._remember(folder=str(publisher.folder), at=time.time() - 120,
                        stamp=publisher._index_stamp())
    cfg.open_browsing = False
    assert publisher.due() is True
    print('FIXED: closing open browsing makes a hand-over due')


def test_upload_writes_past_free_space_floor(tmp_path, monkeypatch):
    conn = db.init_db(tmp_path / 'index.db')
    cfg = SimpleNamespace(state_dir=tmp_path, upload_quota_gb=0)
    upload = FileStorage(stream=io.BytesIO(b'x' * 4096), filename='clip.mp4')
    assert upload.content_length == 0
    monkeypatch.setattr(upload_review.shutil, 'disk_usage',
                        lambda _: shutil._ntuple_diskusage(10**10, 0, phone_backup.KEEP_FREE_BYTES + 1024))
    monkeypatch.setattr(upload_review, '_refuse_oversized', lambda _: None)
    monkeypatch.setattr(upload_review.scanner, 'build_record',
                        lambda *a: {'kind': 'video', 'date_key': '', 'size': 4096})
    import pytest
    with pytest.raises(ValueError, match='not enough free space'):
        upload_review.stage(conn, cfg, upload, 'clip.mp4', '/library', '', 1)
    assert not list((tmp_path / 'pending-uploads').rglob('*.mp4'))
    print('FIXED: multipart upload is refused with only 1024 bytes above reserve')


def test_legacy_settings_route_does_not_trigger_replica(as_admin, cfg, tmp_path):
    (tmp_path / 'share').mkdir()
    cfg.replica_dir = str(tmp_path / 'share')
    cfg.replica_every_minutes = 1
    publisher = Publisher(cfg)
    publisher._remember(folder=str(publisher.folder), at=time.time() - 120,
                        stamp=publisher._index_stamp())
    before = cfg.nsfw_filter
    response = as_admin.post('/api/settings', json={'nsfw_filter': not before})
    assert response.status_code == 200, response.get_json()
    assert cfg.nsfw_filter != before
    assert publisher.due() is True
    print('FIXED: settings API change makes publication due')
