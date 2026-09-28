"""Date boundaries, management isolation and physical relocation."""
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.server import auth, date_policy
from ninaivu.storage import db
from ninaivu.media import date_edit


@pytest.fixture()
def dates(scanned):
    cfg, conn, _ = scanned
    conn.execute("DELETE FROM meta WHERE key=?", (date_policy.KEY,))
    conn.commit()
    admin = auth.bootstrap_admin(conn, *ADMIN, "Dad")
    auth.create_user(conn, *FAMILY, display_name="Maya", role="family", created_by=admin.id)
    rows = conn.execute("SELECT id FROM assets ORDER BY id LIMIT 3").fetchall()
    ids = [r["id"] for r in rows]
    for asset_id, key in zip(ids, ["2013-12-31", "2014-01-01", ""]):
        db.update_asset(conn, asset_id, date_key=key)
    services = build_services(cfg)
    services.scanner.stop(join=True)
    home = create_home_app(services)
    console = create_admin_app(services)
    yield cfg, conn, ids, login(home.test_client(), *ADMIN), login(home.test_client(), *FAMILY), login(console.test_client(), *ADMIN)
    services.stop()


def listing(client):
    response = client.get('/api/assets?limit=200')
    assert response.status_code == 200, response.get_json()
    return {item['id'] for item in response.get_json()['items']}


def test_default_boundary_and_direct_links(dates):
    """By default family members and guests see the cutoff onwards, and admins
    see every date — undated media included — so nothing an admin manages is
    out of their reach in the family app."""
    _, _, (old, boundary, unknown), admin, family, console = dates
    assert {old, boundary, unknown} <= listing(admin)
    assert boundary in listing(family) and old not in listing(family)
    assert unknown not in listing(family)
    assert {old, boundary, unknown} <= listing(console)
    for forbidden in (old, unknown):
        for route in ('assets', 'file', 'thumb', 'download'):
            assert family.get(f'/api/{route}/{forbidden}').status_code == 404
    assert admin.get(f'/api/file/{boundary}').status_code == 200
    assert family.get(f'/api/file/{boundary}').status_code == 200
    assert 'no-store' in family.get(f'/api/file/{boundary}').headers['Cache-Control']
    admin_preview = {i['id'] for i in console.get('/api/admin/preview?role=admin').get_json()['items']}
    assert {old, boundary, unknown} <= admin_preview
    family_preview = {i['id'] for i in console.get('/api/admin/preview?role=family').get_json()['items']}
    assert boundary in family_preview and old not in family_preview and unknown not in family_preview


def test_admins_keep_undated_media_under_their_own_cutoff(dates):
    _, conn, (old, boundary, unknown), admin, family, _ = dates
    db.set_meta(conn, date_policy.KEY, __import__('json').dumps(
        dict(date_policy.DEFAULTS, admin='before', family='all')))
    conn.commit()
    seen = listing(admin)
    assert old in seen and unknown in seen and boundary not in seen
    assert admin.get(f'/api/file/{unknown}').status_code == 200
    assert admin.get(f'/api/file/{boundary}').status_code == 404
    assert {old, boundary, unknown} <= listing(family), "'all' still includes undated media"


def test_settings_persist_and_roles_are_isolated(dates):
    _, conn, (old, boundary, unknown), admin, family, console = dates
    assert console.get('/api/admin/date-policy').get_json() == date_policy.DEFAULTS
    policy = dict(date_policy.DEFAULTS, cutoff='2013-01-01', admin='all', family='before')
    assert console.post('/api/admin/date-policy', json=policy).status_code == 200
    assert date_policy.read(conn) == policy
    assert {old, boundary, unknown} <= listing(admin)
    assert old not in listing(family)
    assert family.post('/api/admin/date-policy', json=policy).status_code == 404
    for bad in (dict(policy, cutoff='2014-02-30'), dict(policy, family='evil'), []):
        assert console.post('/api/admin/date-policy', json=bad).status_code == 400
    assert date_policy.read(conn) == policy


def test_move_preserves_identity_bytes_membership_and_rescan_date(dates):
    cfg, conn, (old, _, _), admin, family, console = dates
    before = db.get_asset(conn, old)
    original = Path(before['root']) / before['rel_path']
    content = original.read_bytes()
    album = db.create_album(conn, 'Old photos')
    db.album_add(conn, album, [old])
    db.set_user_asset(conn, 1, old, favorite=1)
    response = console.patch(f'/api/admin/assets/{old}/creation-date', json={'creation_date': '2014-01-01'})
    assert response.status_code == 200, response.get_json()
    after = db.get_asset(conn, old)
    target = Path(after['root']) / after['rel_path']
    assert after['folder'] == '2014/01/01'
    assert target.read_bytes() == content and not original.exists()
    assert old in db.album_asset_ids(conn, album)
    assert conn.execute('SELECT favorite FROM user_assets WHERE asset_id=?', (old,)).fetchone()[0] == 1
    assert old in listing(family) and old in listing(admin)   # admins see every date by default
    assert family.get(f'/api/file/{old}').data == content
    stale = dict(after, date_key='2000-01-01', captured_at=0, date_source='exif')
    for upsert in (db.upsert_asset, lambda c, r: db.bulk_upsert(c, [r])):
        upsert(conn, stale)
        assert db.get_asset(conn, old)['date_key'] == '2014-01-01'


def test_collision_and_invalid_dates_do_not_modify_original(dates):
    _, conn, (old, _, _), _, family, console = dates
    before = db.get_asset(conn, old)
    source = Path(before['root']) / before['rel_path']
    dest = Path(before['root']) / '2014/01/01' / before['filename']
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b'existing file')
    url = f'/api/admin/assets/{old}/creation-date'
    assert console.patch(url, json={'creation_date': '2014-01-01'}).status_code == 409
    assert source.exists() and dest.read_bytes() == b'existing file'
    assert db.get_asset(conn, old)['rel_path'] == before['rel_path']
    assert console.patch(url, json={'creation_date': '2014-02-30'}).status_code == 400
    assert family.patch(url, json={'creation_date': '2014-01-01'}).status_code == 404


def test_companion_failure_rolls_back(dates):
    _, conn, (old, _, _), _, _, _ = dates
    before = db.get_asset(conn, old)
    source = Path(before['root']) / before['rel_path']
    sidecar = source.with_suffix('.xmp')
    sidecar.write_text('metadata')
    move = date_edit._move
    def fail_sidecar(src, dst):
        if src == sidecar:
            raise OSError('simulated disk failure')
        move(src, dst)
    with patch.object(date_edit, '_move', side_effect=fail_sidecar), pytest.raises(OSError):
        date_edit.relocate(conn, old, date_edit.parse_date('2014-01-01'), [before['root']])
    assert source.exists() and sidecar.exists()
    assert db.get_asset(conn, old)['rel_path'] == before['rel_path']


def test_albums_and_shared_links_follow_dates(dates):
    _, conn, (old, boundary, _), admin, family, _ = dates
    album = db.create_album(conn, 'Before cutoff')
    db.album_add(conn, album, [old])
    assert family.get(f'/api/albums/{album}').status_code == 404
    assert admin.get(f'/api/albums/{album}').status_code == 200
    assert album not in {a['id'] for a in family.get('/api/albums').get_json()['albums']}
    token = 'date-test-share'
    db.create_share(conn, token, 'asset', old, created_by=1)
    assert family.get(f'/api/share/{token}').status_code == 404
    db.album_add(conn, album, [boundary])
    assert family.get(f'/api/albums/{album}').status_code == 404
    assert not family.get(f'/api/assets?album={album}').get_json()['items']


def test_move_updates_archive_and_cloud_references(dates):
    cfg, conn, (old, _, _), _, _, console = dates
    import sqlite3
    before = db.get_asset(conn, old)
    source = str(Path(before['root']) / before['rel_path'])
    with sqlite3.connect(cfg.state_dir / 'archive.db') as archive:
        archive.execute("INSERT INTO files(source_path, destination_path, status) VALUES(?, ?, 'verified')",
                        ('external-original.jpg', source))
        archive.execute("INSERT INTO files(source_path, duplicate_of, status) VALUES(?, ?, 'duplicate')",
                        ('external-duplicate.jpg', source))
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, remote_id) VALUES(?, ?, 'remote-original')",
                 (before['root'], before['rel_path']))
    conn.commit()
    response = console.patch(f'/api/admin/assets/{old}/creation-date', json={'creation_date': '2014-01-01'})
    assert response.status_code == 200, response.get_json()
    after = db.get_asset(conn, old)
    target = str(Path(after['root']) / after['rel_path'])
    with sqlite3.connect(cfg.state_dir / 'archive.db') as archive:
        assert archive.execute("SELECT destination_path FROM files WHERE status='verified'").fetchone()[0] == target
        assert archive.execute("SELECT duplicate_of FROM files WHERE status='duplicate'").fetchone()[0] == target
    assert conn.execute("SELECT rel_path FROM cloud_uploads WHERE remote_id='remote-original'").fetchone()[0] == after['rel_path']


def test_database_failure_restores_original(dates):
    _, conn, (old, _, _), _, _, _ = dates
    before = db.get_asset(conn, old)
    source = Path(before['root']) / before['rel_path']
    conn.execute("CREATE TRIGGER reject_move BEFORE UPDATE OF rel_path ON assets "
                 "BEGIN SELECT RAISE(ABORT, 'simulated database failure'); END")
    conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        date_edit.relocate(conn, old, date_edit.parse_date('2014-01-01'), [before['root']])
    assert source.exists()
    assert db.get_asset(conn, old)['rel_path'] == before['rel_path']
