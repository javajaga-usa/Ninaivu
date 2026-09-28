"""Invalid album input must not leave partial mutations or server errors."""


def test_rejected_creation_does_not_leave_an_album(as_family):
    before = as_family.get('/api/albums').get_json()
    for ids in (None, '12', {}, [2**63], [True], [1.5], ['bad']):
        response = as_family.post('/api/albums', json={'name': 'Must not exist', 'ids': ids})
        assert response.status_code == 400, response.get_json()
        assert as_family.get('/api/albums').get_json() == before


def test_invalid_cover_does_not_rename_album(as_family):
    album = as_family.post('/api/albums', json={'name': 'Original'}).get_json()['id']
    for cover in ([], {}, 'bad', 2**63, True, 1.5):
        response = as_family.patch(f'/api/albums/{album}',
                                   json={'name': 'Changed', 'cover_id': cover})
        assert response.status_code == 400
        assert as_family.get(f'/api/albums/{album}').get_json()['album']['name'] == 'Original'


def test_album_routes_reject_non_objects(as_family):
    album = as_family.post('/api/albums', json={'name': 'Original'}).get_json()['id']
    for body in ([1], 'bad', True):
        assert as_family.post('/api/albums', json=body).status_code == 400
        assert as_family.patch(f'/api/albums/{album}', json=body).status_code == 400
        assert as_family.post(f'/api/albums/{album}/items', json=body).status_code == 400
