import io
import json
import pytest
from PIL import Image
from ninaivu.media import ai_editing


def test_compact_local_plan_is_validated(monkeypatch):
    monkeypatch.setenv('NINAIVU_EDIT_MODEL', 'qwen3:4b')
    calls = []
    class Connection:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == ('127.0.0.1', 11434)
        def request(self, method, path, body, headers):
            calls.append(json.loads(body))
        def getresponse(self):
            return self
        status = 200
        def read(self, limit):
            return json.dumps({'message': {'content': json.dumps({'summary':'Lift shadows gently.', 'unsupported':False, 'adjustments':{'shadows':20}})}}).encode()
        def close(self):
            pass
    monkeypatch.setattr(ai_editing.http.client, 'HTTPConnection', Connection)
    assert ai_editing.plan('Lift shadows but leave saturation alone', {'saturation':0})['patch'] == {'shadows':20}
    assert 'images' not in str(calls)
    assert calls[0]['stream'] is False


@pytest.mark.parametrize('patch', [{'exposure':float('nan')}, {'exposure':True}, {'crop':'face'}, {'script':'alert(1)'}, {'noise':-5}])
def test_invalid_model_operations_rejected(patch):
    with pytest.raises(ValueError):
        ai_editing.validate_adjustments(patch)


def test_guest_cannot_use_local_inference(as_guest):
    assert as_guest.post('/api/ai-playground/plan', json={'prompt':'enhance'}).status_code == 403
    assert as_guest.post('/api/ai-playground/generate', json={}).status_code == 403


def test_plan_route_validation_and_unconfigured_model(as_family, monkeypatch):
    monkeypatch.setenv('NINAIVU_EDIT_MODEL', '')
    assert as_family.post('/api/ai-playground/plan', json={'prompt':'enhance','current':{}}).status_code == 503
    assert as_family.post('/api/ai-playground/plan', json={'prompt':'undress this person'}).status_code == 400
    assert as_family.post('/api/ai-playground/plan', json=[]).status_code == 400
    assert as_family.post('/api/ai-playground/plan', data='x'*13000).status_code == 413


def test_generation_unconfigured_and_bad_payload(as_family, monkeypatch):
    monkeypatch.setenv('NINAIVU_IMAGE_EDIT_MODEL', '')
    assert as_family.post('/api/ai-playground/generate', json={'prompt':'turn this into a painting','image':''}).status_code == 503
    assert as_family.post('/api/ai-playground/generate', json={'prompt':'paint','image':'!!!'}).status_code == 400


def test_generation_route_returns_transient_png(as_family, monkeypatch):
    from ninaivu.media import generative_editing
    output = io.BytesIO()
    Image.new('RGB', (32,32), 'blue').save(output, 'PNG')
    monkeypatch.setattr(generative_editing, 'generate', lambda *_: output.getvalue())
    response = as_family.post('/api/ai-playground/generate', json={'prompt':'make it blue','image':''})
    assert response.status_code == 200
    assert response.mimetype == 'image/png'
    assert 'no-store' in response.headers['Cache-Control']
