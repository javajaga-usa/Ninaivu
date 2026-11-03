"""Recovery cleanup removes only verified old bytes, never live or changed data."""
import json

import pytest

from ninaivu.storage import relocate, recovery_cleanup


@pytest.fixture
def moved(tmp_path):
    source, target = tmp_path / 'old', tmp_path / 'new'
    source.mkdir()
    (source / 'index.db').write_bytes(b'old index')
    (source / 'thumbs').mkdir()
    (source / 'thumbs/a.jpg').write_bytes(b'thumb')
    relocate.request(source, target)
    assert relocate.apply_pending(source) == target
    return source, target


def test_cleanup_preserves_forwarding_live_index_and_added_or_changed_files(moved):
    source, target = moved
    (target / 'index.db').write_bytes(b'current index')
    (source / 'thumbs/a.jpg').write_bytes(b'edited recovery thumbnail')
    (source / 'added.txt').write_text('keep me')
    preview = recovery_cleanup.preview(target)
    assert preview['files'] == 2
    result = recovery_cleanup.clean(target)
    assert result['removed'] == 1 and result['preserved'] == 1
    assert (target / 'index.db').read_bytes() == b'current index'
    assert (source / 'added.txt').read_text() == 'keep me'
    assert (source / 'thumbs/a.jpg').exists()
    assert relocate.resolve(source) == target
    assert recovery_cleanup.clean(target)['removed'] == 0


def test_cleanup_leaves_only_the_forwarding_note_when_unchanged(moved):
    source, target = moved
    recovery_cleanup.clean(target)
    assert [p.name for p in source.iterdir() if not p.name.endswith('.lock')] == [relocate.REDIRECT]
    assert relocate.resolve(source) == target


@pytest.mark.parametrize('name', ['.', '../outside', '/index.db', 'thumbs/../../index.db',
                                 'index-location.json', 'ninaivu-server.lock'])
def test_invalid_manifest_removes_nothing(moved, name):
    source, target = moved
    manifest = json.loads((target / relocate.MANIFEST).read_text())
    manifest['files'][name] = {'size': 0, 'sha256': 'x'}
    relocate.write(target / relocate.MANIFEST, manifest)
    with pytest.raises(ValueError):
        recovery_cleanup.clean(target)
    assert (source / 'index.db').exists()


def test_changed_forwarding_note_removes_nothing(moved, tmp_path):
    source, target = moved
    elsewhere = tmp_path / 'other'
    elsewhere.mkdir()
    (elsewhere / 'index.db').touch()
    relocate.write(source / relocate.REDIRECT, {'destination': str(elsewhere)})
    with pytest.raises(ValueError, match='does not forward'):
        recovery_cleanup.clean(target)
    assert (source / 'index.db').exists()


def test_symlink_to_unrelated_data_is_preserved(moved, tmp_path):
    source, target = moved
    outside = tmp_path / 'keep'
    outside.write_bytes((source / 'index.db').read_bytes())
    (source / 'index.db').unlink()
    (source / 'index.db').symlink_to(outside)
    recovery_cleanup.clean(target)
    assert outside.exists() and (source / 'index.db').is_symlink()


@pytest.mark.parametrize('role', ['anon', 'as_family', 'as_guest'])
def test_cleanup_endpoints_require_an_administrator(request, role):
    client = request.getfixturevalue(role)
    assert client.get('/api/admin/migration/recovery-cleanup').status_code in {401, 403}
    assert client.post('/api/admin/migration/recovery-cleanup', json={'confirm': True}).status_code in {401, 403}


def test_administrator_previews_and_explicitly_confirms_cleanup(as_admin, tmp_path):
    import hashlib
    from ninaivu.api import migration_api
    state = as_admin.application.config['MV_CONFIG'].state_dir
    source = tmp_path / 'original recovery'
    source.mkdir()
    (source / 'old.txt').write_bytes(b'copied bytes')
    relocate.write(source / relocate.REDIRECT, {'destination': str(state)})
    relocate.write(state / relocate.MANIFEST, {'source': str(source), 'destination': str(state),
        'files': {'old.txt': {'size': 12, 'sha256': hashlib.sha256(b'copied bytes').hexdigest()}}})
    preview = as_admin.get('/api/admin/migration/recovery-cleanup')
    assert preview.status_code == 200 and preview.get_json()['source'] == str(source)
    assert as_admin.post('/api/admin/migration/recovery-cleanup', json={}).status_code == 400
    assert (source / 'old.txt').exists()
    started = as_admin.post('/api/admin/migration/recovery-cleanup', json={'confirm': True})
    assert started.status_code == 202
    migration_api._recovery_cleaner._thread.join(10)
    assert not (source / 'old.txt').exists()
    assert relocate.resolve(source) == state
