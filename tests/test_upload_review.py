import io
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from test_date_access import dates as dates
from ninaivu.media import scanner, upload_review
from ninaivu.storage import db


def upload(family, name='new-photo.jpg'):
    stream = io.BytesIO()
    image = Image.new('RGB', (100, 80), 'green')
    exif = image.getexif()
    exif[0x9003] = '2015:06:07 12:30:00'
    image.save(stream, 'JPEG', exif=exif)
    content = stream.getvalue()
    response = family.post('/api/upload', data={'file': (io.BytesIO(content), name)})
    assert response.status_code == 200, response.get_json()
    return response.get_json(), content


def test_upload_stays_private_until_approved(dates):
    cfg, conn, _, _, family, admin = dates
    before = conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0]
    result, content = upload(family)
    assert result['pending_approval'] and result['total'] == 1
    upload_id = result['uploaded'][0]['id']
    row = upload_review.get(conn, upload_id)
    staged = upload_review.source_path(cfg, row)
    assert staged.read_bytes() == content
    assert conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0] == before
    assert not list(Path(cfg.active_root).rglob('new-photo.jpg'))
    assert family.get('/api/admin/uploads').status_code == 404
    assert family.get(f'/api/admin/uploads/{upload_id}/preview').status_code == 404
    assert family.post(f'/api/admin/uploads/{upload_id}/approve', json={}).status_code == 404
    pending = admin.get('/api/admin/uploads').get_json()
    assert pending['total'] == 1
    assert pending['items'][0]['creation_date'] == '2015-06-07'
    assert pending['items'][0]['uploader'] == 'Maya'
    assert admin.get(f'/api/admin/uploads/{upload_id}/preview').data == content
    response = admin.post(f'/api/admin/uploads/{upload_id}/approve', json={})
    assert response.status_code == 200, response.get_json()
    asset = response.get_json()['item']
    assert asset['folder'] == '2015/06/07'
    assert (Path(asset['root']) / asset['rel_path']).read_bytes() == content
    assert not staged.exists()
    assert family.get(f"/api/file/{asset['id']}").data == content
    assert admin.get('/api/admin/uploads').get_json()['total'] == 0
    assert admin.post(f'/api/admin/uploads/{upload_id}/approve', json={}).get_json()['item']['id'] == asset['id']
    assert conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0] == before + 1


def test_approval_date_override_controls_folder_and_visibility(dates):
    _, conn, _, home_admin, family, admin = dates
    result, _ = upload(family)
    upload_id = result['uploaded'][0]['id']
    url = f'/api/admin/uploads/{upload_id}/approve'
    assert admin.post(url, json={'creation_date': '2014-02-30'}).status_code == 400
    assert upload_review.get(conn, upload_id)['status'] == 'pending'
    # The date limit is off by default now; this is about one that is set.
    import json as _json
    from ninaivu.server import date_policy
    db.set_meta(conn, date_policy.KEY, _json.dumps(
        dict(date_policy.DEFAULTS, family='after', guest='after')))
    conn.commit()
    asset = admin.post(url, json={'creation_date': '2013-12-31'}).get_json()['item']
    assert asset['folder'] == '2013/12/31'
    assert family.get(f"/api/file/{asset['id']}").status_code == 404
    assert home_admin.get(f"/api/file/{asset['id']}").status_code == 200


def test_repeated_uploads_never_overwrite(dates):
    cfg, _, _, _, family, admin = dates
    names = []
    for _ in range(2):
        result, content = upload(family)
        upload_id = result['uploaded'][0]['id']
        asset = admin.post(f'/api/admin/uploads/{upload_id}/approve', json={}).get_json()['item']
        names.append(asset['rel_path'])
        assert (Path(cfg.active_root) / names[-1]).read_bytes() == content
    assert len(set(names)) == 2


def test_failed_approval_preserves_pending_upload(dates):
    cfg, conn, _, _, family, admin = dates
    result, content = upload(family)
    upload_id = result['uploaded'][0]['id']
    with patch('ninaivu.media.date_edit._move', side_effect=OSError('disk full')):
        assert admin.post(f'/api/admin/uploads/{upload_id}/approve', json={}).status_code == 409
    row = upload_review.get(conn, upload_id)
    assert row['status'] == 'pending'
    assert upload_review.source_path(cfg, row).read_bytes() == content


def test_pending_upload_directory_is_never_scanned(dates):
    cfg, conn, _, _, family, _ = dates
    result, _ = upload(family)
    row = upload_review.get(conn, result['uploaded'][0]['id'])
    folder = upload_review.source_path(cfg, row).parent
    cfg.index_hidden = True
    assert list(scanner.walk_media(folder, cfg)) == []
    assert all('pending-uploads' not in path for path, _ in scanner.walk_media(Path(cfg.state_dir), cfg))


def test_removed_library_cannot_publish_pending_upload(dates):
    cfg, conn, _, _, family, admin = dates
    result, _ = upload(family)
    cfg.roots = []
    cfg.active_root = None
    upload_id = result['uploaded'][0]['id']
    assert admin.post(f'/api/admin/uploads/{upload_id}/approve', json={}).status_code == 400
    assert upload_review.get(conn, upload_id)['status'] == 'pending'


def test_an_admin_can_refuse_an_upload_and_its_file_is_deleted(dates):
    """Not every upload belongs in the library; the queue could only approve."""
    cfg, conn, _, _, family, admin = dates
    before = conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0]
    result, _ = upload(family)
    upload_id = result['uploaded'][0]['id']
    row = upload_review.get(conn, upload_id)
    staged = upload_review.source_path(cfg, row)
    assert staged.exists()

    assert family.post(f'/api/admin/uploads/{upload_id}/reject', json={}).status_code == 404
    response = admin.post(f'/api/admin/uploads/{upload_id}/reject', json={})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['status'] == 'rejected'

    assert not staged.exists() and not staged.parent.exists(), "the file and its folder are gone"
    assert conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0] == before
    assert admin.get('/api/admin/uploads').get_json()['total'] == 0
    row = upload_review.get(conn, upload_id)
    assert row['status'] == 'rejected' and row['reviewed_at']
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='reject_upload'").fetchone()[0] == 1


def test_a_refused_upload_cannot_then_be_approved_nor_an_approved_one_refused(dates):
    _, _, _, _, family, admin = dates
    first = upload(family, 'one.jpg')[0]['uploaded'][0]['id']
    second = upload(family, 'two.jpg')[0]['uploaded'][0]['id']
    assert admin.post(f'/api/admin/uploads/{first}/reject', json={}).status_code == 200
    assert admin.post(f'/api/admin/uploads/{first}/approve', json={}).status_code == 400
    assert admin.post(f'/api/admin/uploads/{first}/reject', json={}).status_code == 409
    assert admin.post(f'/api/admin/uploads/{second}/approve', json={}).status_code == 200
    assert admin.post(f'/api/admin/uploads/{second}/reject', json={}).status_code == 409
    assert admin.post('/api/admin/uploads/9999/reject', json={}).status_code == 404


def test_an_approved_upload_leaves_no_empty_folder_behind(dates):
    cfg, conn, _, _, family, admin = dates
    upload_id = upload(family)[0]['uploaded'][0]['id']
    staged = upload_review.source_path(cfg, upload_review.get(conn, upload_id))
    assert admin.post(f'/api/admin/uploads/{upload_id}/approve', json={}).status_code == 200
    assert not staged.parent.exists()
