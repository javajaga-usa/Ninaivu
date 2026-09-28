"""Edited copies are additive, reviewed unless an admin saved them, and retain the source's access policy."""
import io
from pathlib import Path

from PIL import Image
from conftest import ids_of
from test_date_access import dates as dates
from ninaivu.storage import db
from ninaivu.media import media, upload_review


def png():
    stream = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 120, 90)).save(stream, "PNG")
    return stream.getvalue()


def save(client, target):
    return client.post(f"/api/asset/{target}/edited-copy", data=png(), content_type="image/png")


def test_an_admins_copy_preserves_the_original_and_creates_distinct_files(as_admin, scanned):
    cfg, conn, _ = scanned
    target = ids_of(as_admin)[0]
    original = db.get_asset(conn, target)
    path = Path(original['root']) / original['rel_path']
    before = path.read_bytes()
    results = [save(as_admin, target) for _ in range(2)]
    assert all(r.status_code == 201 for r in results), [r.get_json() for r in results]
    assert results[0].json['id'] != results[1].json['id'] != target
    assert path.read_bytes() == before
    for response in results:
        assert response.json['playable']
        copy = db.get_asset(conn, response.json['id'])
        assert copy['folder'] == original['folder']
        assert copy['visibility'] == original['visibility']
        assert copy['rotation'] == 0
        assert as_admin.get(f"/api/thumb/{copy['id']}").status_code == 200
        with Image.open(path.parent / copy['filename']) as image:
            assert image.size == (64, 48)
            assert abs(media.read_exif(image)['captured_at'] - original['captured_at']) < 1


def test_guest_cannot_save(as_admin, as_guest):
    assert save(as_guest, ids_of(as_admin)[0]).status_code == 403


def test_png_copy_retains_transparency(as_admin, scanned):
    _, conn, _ = scanned
    target = ids_of(as_admin)[0]
    stream = io.BytesIO()
    Image.new('RGBA', (64, 48), (180, 120, 90, 37)).save(stream, 'PNG')
    response = as_admin.post(f'/api/asset/{target}/edited-copy',
                              data=stream.getvalue(), content_type='image/png')
    assert response.status_code == 201
    copy = db.get_asset(conn, response.json['id'])
    with Image.open(Path(copy['root']) / copy['rel_path']) as image:
        assert image.convert('RGBA').getpixel((0, 0)) == (180, 120, 90, 37)


def test_anonymous_cannot_save(anon):
    assert save(anon, 1).status_code in (401, 403)


def test_hidden_copy_stays_hidden(as_admin, as_family, scanned):
    target = ids_of(as_admin)[0]
    as_admin.post('/api/visibility', json={'ids':[target], 'visibility':'hidden'})
    assert save(as_family, target).status_code == 404
    response = save(as_admin, target)
    assert response.status_code == 201
    assert as_family.get(f"/api/asset/{response.json['id']}").status_code == 404


def test_rejects_invalid_upload(as_family):
    target = ids_of(as_family)[0]
    url = f'/api/asset/{target}/edited-copy'
    assert as_family.post(url, data=b'bad', content_type='image/png').status_code == 400
    assert as_family.post(url, data=png(), content_type='image/jpeg').status_code == 400


def test_failed_publication_keeps_source_and_cleans_row(as_admin, scanned, monkeypatch):
    target = ids_of(as_admin)[0]
    _, conn, _ = scanned
    count = conn.execute('SELECT count(*) FROM assets').fetchone()[0]
    monkeypatch.setattr('ninaivu.api.api.os.link', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    assert save(as_admin, target).status_code == 500
    assert conn.execute('SELECT count(*) FROM assets').fetchone()[0] == count
    assert as_admin.get(f'/api/file/{target}').status_code == 200


def test_a_family_members_edit_waits_for_an_administrator(dates):
    """Saving an edit is uploading a file, so it is reviewed like one.

    Nothing reaches the library, or any other member's view of it, until an
    administrator has looked: the bytes sit in the same private staging area a
    plain upload uses.
    """
    cfg, conn, _, _, family, admin = dates
    target = ids_of(family)[0]
    before = conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0]
    response = save(family, target)
    assert response.status_code == 202, response.get_json()
    body = response.get_json()
    assert body['status'] == 'pending'
    assert conn.execute('SELECT COUNT(*) FROM assets').fetchone()[0] == before
    row = upload_review.get(conn, body['pending_id'])
    assert row['status'] == 'pending'
    assert upload_review.source_path(cfg, row).is_file()
    assert body['pending_id'] in [item['id'] for item in admin.get('/api/admin/uploads').get_json()['items']]


def test_approving_an_edit_puts_it_beside_its_source(dates):
    """Approval files a derivative where the direct save would have put it.

    An ordinary upload is filed by date because it has no place of its own. An
    edit does: the photograph it came from, whose folder and visibility it keeps.
    """
    _, conn, _, _, family, admin = dates
    target = ids_of(family)[0]
    source = db.get_asset(conn, target)
    pending_id = save(family, target).get_json()['pending_id']
    approved = admin.post(f'/api/admin/uploads/{pending_id}/approve', json={})
    assert approved.status_code == 200, approved.get_json()
    copy = db.get_asset(conn, approved.get_json()['item']['id'])
    assert copy['id'] != target
    assert copy['folder'] == source['folder']
    assert copy['visibility'] == source['visibility']
    assert copy['rotation'] == 0
    assert Path(copy['rel_path']).parent == Path(source['rel_path']).parent
    assert (Path(copy['root']) / copy['rel_path']).is_file()
    assert Path(source['root'], source['rel_path']).is_file()


def test_the_queue_says_what_an_edit_is_and_where_it_goes(dates):
    """The filename is generated, so the queue has to explain the item itself."""
    _, conn, _, _, family, admin = dates
    target = ids_of(family)[0]
    source = db.get_asset(conn, target)
    pending_id = save(family, target).get_json()['pending_id']
    item = next(i for i in admin.get('/api/admin/uploads').get_json()['items']
                if i['id'] == pending_id)
    assert item['edited_from'] == source['filename']
    # Not filed by date: an ordinary upload is, and the queue must not promise
    # a year/month/day destination for something going beside its source.
    assert item['files_by_date'] is False
    assert not item['destination'].endswith('year/month/day')
    assert item['destination'].endswith(source['folder'] or source['root'])

    plain = family.post('/api/upload', data={'file': (io.BytesIO(png()), 'arrival.png')})
    assert plain.status_code == 200, plain.get_json()
    other = next(i for i in admin.get('/api/admin/uploads').get_json()['items']
                 if i['id'] == plain.get_json()['uploaded'][0]['id'])
    assert other['edited_from'] is None and other['files_by_date'] is True


def test_an_approved_edit_keeps_its_source_creation_date(dates):
    """The copy is the same photograph, so it carries the same moment.

    Down to the time of day: the review queue pre-fills the source's date, and
    approving with that unchanged value must not re-date the copy to midnight.
    The written file carries it too, so a later rescan reads the same date.
    """
    _, conn, _, _, family, admin = dates
    target = ids_of(family)[0]
    source = db.get_asset(conn, target)
    pending_id = save(family, target).get_json()['pending_id']
    # Exactly what the console sends when an administrator leaves the date alone.
    approved = admin.post(f'/api/admin/uploads/{pending_id}/approve',
                          json={'creation_date': source['date_key']})
    assert approved.status_code == 200, approved.get_json()
    copy = db.get_asset(conn, approved.get_json()['item']['id'])
    assert copy['date_key'] == source['date_key']
    assert copy['captured_at'] == source['captured_at']
    with Image.open(Path(copy['root']) / copy['rel_path']) as image:
        assert abs(media.read_exif(image)['captured_at'] - source['captured_at']) < 1


def test_an_administrator_can_still_re_date_an_approved_edit(dates):
    """Keeping the date by default must not make it impossible to change."""
    _, conn, _, _, family, admin = dates
    target = ids_of(family)[0]
    source = db.get_asset(conn, target)
    pending_id = save(family, target).get_json()['pending_id']
    approved = admin.post(f'/api/admin/uploads/{pending_id}/approve',
                          json={'creation_date': '2011-03-04'})
    assert approved.status_code == 200, approved.get_json()
    copy = db.get_asset(conn, approved.get_json()['item']['id'])
    assert copy['date_key'] == '2011-03-04' != source['date_key']
    # Re-dated, but still filed beside its source rather than by the new date.
    assert copy['folder'] == source['folder']
