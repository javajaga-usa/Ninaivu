import hashlib
import importlib.util
import json
from pathlib import Path

from ninaivu import archive, build_services, create_admin_app
from ninaivu.server import auth
from ninaivu.storage import db as library_db
from ninaivu.archive import database as db
from conftest import ADMIN, login


def load_verifier():
    path = Path(__file__).parents[1] / 'tools' / 'verify_recovery_report.py'
    spec = importlib.util.spec_from_file_location('verify_recovery_report', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_portable_report_and_standalone_verifier(cfg, tmp_path):
    services = build_services(cfg)
    auth.bootstrap_admin(library_db.connect(cfg.db_path), *ADMIN)
    client = login(create_admin_app(services).test_client(), *ADMIN)

    root = tmp_path / 'Master'
    target = root / '2024' / 'photo.jpg'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'portable-photo')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    archive.configure(cfg.state_dir, 0)
    db.init_db()
    db.save_settings([str(tmp_path)], str(root), {'image'})
    job = db.create_job([str(tmp_path)], str(root), 'YYYY/MM/DD')
    source = tmp_path / 'card' / 'photo.jpg'
    db.claim_file(str(source), 'photo.jpg', target.stat().st_size, 0, job,
                  destination_root=str(root))
    db.set_status(str(source), 'verified', file_hash=digest, dest_hash=digest,
                  destination_path=str(target))

    response = client.get('/api/archive/recovery.json')
    assert response.status_code == 200
    report = json.loads(response.get_data(as_text=True))
    assert report['format'] == 'ninaivu-recovery-v1'
    assert report['files'][0]['path'] == '2024/photo.jpg'

    report_path = tmp_path / 'recovery.json'
    report_path.write_text(json.dumps(report), encoding='utf-8')
    verifier = load_verifier()
    assert verifier.verify(report_path, root)['matched'] == 1
    target.write_bytes(b'changed')
    assert verifier.verify(report_path, root)['changed'] == ['2024/photo.jpg']
