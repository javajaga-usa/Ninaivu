"""Regressions for privacy, restore and backup defects found in the app audit."""
import pytest
import io
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from flask import Flask, g
from fake_drive import FakeDrive
from ninaivu.api.api import bp
from ninaivu.archive import course_material
from ninaivu.cloud import store
from ninaivu.cloud.drive import Credentials, DriveClient
from ninaivu.cloud.engine import SyncEngine
from ninaivu.media import scanner, stills, upload_review
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.storage import backup, db, recycle


@pytest.fixture
def base(tmp_path):
    yield tmp_path
    db.close_all()


def config(base):
    cfg = Config()
    cfg.state_dir = base / "state"
    cfg.active_root = str(base / "library")
    cfg.roots = [cfg.active_root]
    cfg.ai_enabled = cfg.watch = cfg.detect_orientation = False
    cfg.quality_scan = False
    cfg.min_media_bytes = 0
    Path(cfg.active_root).mkdir()
    cfg.ensure_dirs()
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    return cfg, conn


def insert(conn, cfg, name, **fields):
    return db.upsert_asset(conn, dict(root=cfg.active_root, rel_path=name,
        filename=name, kind="picture", ext="jpg", date_key="2025-01-01", **fields))


def test_share_reuse(base):
    cfg, conn = config(base)
    old_id = insert(conn, cfg, "public.jpg", visibility=0)
    db.create_share(conn, "audit-synthetic-token", "asset", old_id)
    recycle.recycle(conn, [old_id])
    new_id = insert(conn, cfg, "private.jpg", visibility=2)
    Image.new("RGB", (16, 16), "red").save(Path(cfg.active_root) / "private.jpg")
    app = Flask(__name__)
    app.config.update(MV_CONFIG=cfg, NINAIVU_FACE="home")
    app.register_blueprint(bp)

    @app.before_request
    def identify():
        g.user = auth.ANONYMOUS

    response = app.test_client().get(f"/api/share/audit-synthetic-token/file/{old_id}")
    assert old_id == new_id and response.status_code == 404
    response.close()


def test_restore_visibility(base):
    cfg, conn = config(base)
    source = Path(cfg.active_root) / "secret.jpg"
    Image.new("RGB", (16, 16), "red").save(source)
    asset_id = insert(conn, cfg, source.name, visibility=2, vis_source="item")
    recycle.recycle(conn, [asset_id])
    entry = recycle.listing(conn)[0]
    recycle.restore(conn, [entry["id"]])
    record = scanner.build_record(Path(cfg.active_root), source.name, source.stat(), cfg)
    db.upsert_asset(conn, record)
    restored = conn.execute("SELECT visibility, vis_source FROM assets").fetchone()
    assert tuple(restored) == (2, "item")


def test_stale_preview(base):
    cfg, conn = config(base)
    source = Path(cfg.active_root) / "photo.tif"
    Image.new("RGB", (16, 16), "red").save(source)
    cache = stills.StillStore(cfg.state_dir)
    result = cache.build(1, source, orient=Image.open)
    before = result.read_bytes()
    Image.new("RGB", (16, 16), "blue").save(source)
    result = cache.build(1, source, orient=Image.open)
    assert result.read_bytes() != before
    old_id = db.upsert_asset(conn, dict(root=cfg.active_root, rel_path=source.name,
        filename=source.name, kind="picture", ext="tif", visibility=2, date_key="2025-01-01"))
    recycle.recycle(conn, [old_id])
    new_source = source.with_name("public.tif")
    Image.new("RGB", (16, 16), "green").save(new_source)
    new_id = db.upsert_asset(conn, dict(root=cfg.active_root, rel_path=new_source.name,
        filename=new_source.name, kind="picture", ext="tif", visibility=0, date_key="2025-01-01"))
    app = Flask(__name__)
    app.config.update(MV_CONFIG=cfg, NINAIVU_FACE="home", MV_STILLS=cache)
    app.register_blueprint(bp)

    @app.before_request
    def identify():
        g.user = auth.ANONYMOUS

    response = app.test_client().get(f"/api/preview/{new_id}")
    assert old_id == new_id and response.status_code == 200 and response.data != before
    response.close()


def drive_client(fake):
    return DriveClient(Credentials(access_token="synthetic", expires_at=9e9), transport=fake)


def test_resumed_changed_file(base):
    cfg, conn = config(base)
    store.init_schema(conn)
    source = Path(cfg.active_root) / "clip.mp4"
    chunk = 256 * 1024
    original, replacement = b"A" * chunk + b"B" * chunk, b"C" * chunk + b"D" * chunk
    source.write_bytes(original)
    store.remember(conn, cfg.active_root, source.name, size=len(original))
    fake = FakeDrive()
    client = drive_client(fake)
    session = client.begin_upload(source.name, "synthetic-parent", len(original))
    client.send_chunk(session, original[:chunk], 0, len(original))
    store.save_resume(conn, cfg.active_root, source.name, session)
    source.write_bytes(replacement)
    store.queue_missing(conn, [dict(root=cfg.active_root, rel_path=source.name, size=len(replacement))])
    engine = SyncEngine(connect=lambda: client, open_db=lambda: conn)
    row = store.pending_batch(conn)[0]
    outcome = engine._one(conn, client, row)
    uploaded = fake.content(source.name)
    assert outcome == "sent" and uploaded == replacement


def test_completed_resume(base):
    source = base / "clip.mp4"
    source.write_bytes(b"synthetic video")
    calls = []

    def transport(method, url, **kwargs):
        calls.append((method, url))
        return 200, {}, b'{"id":"already-uploaded"}'

    client = drive_client(transport)
    result = client.upload_file(source, source.name, "synthetic-parent", resume_url="https://synthetic.invalid/session")
    assert result == "already-uploaded"
    assert len(calls) == 1



def test_pending_backup(base):
    cfg, conn = config(base)
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(image, "JPEG")

    class Upload:
        def save(self, stream):
            stream.write(image.getvalue())

    pending = upload_review.stage(conn, cfg, Upload(), "pending.jpg", cfg.active_root, "", None)
    bundle = backup.snapshot(cfg.state_dir, base / "backups")
    restored = backup.extract(bundle, base / "restored")
    backup.verify_state(restored)
    restored_conn = db.connect(restored / "index.db")
    row = upload_review.get(restored_conn, pending["id"])
    expected = restored / "pending-uploads" / row["storage_key"] / row["filename"]
    assert row["status"] == "pending" and expected.read_bytes() == image.getvalue()


def test_camera_limit(base):
    names = [f"{i:03}.jpg" for i in range(41)]
    with patch.object(course_material.os, "walk", return_value=[("synthetic", [], names)]):
        found = course_material.camera_photographs("synthetic", reader=lambda p: p.endswith("040.jpg"))
    assert found is None




def test_legacy_shares_retire_once_and_new_shares_survive_restart(base):
    cfg, conn = config(base)
    asset = insert(conn, cfg, 'photo.jpg')
    db.create_share(conn, 'legacy', 'asset', asset)
    conn.execute("DELETE FROM meta WHERE key='share_delete_guard'")
    conn.commit()
    db.init_db(cfg.db_path)
    assert db.get_share(conn, 'legacy')['target_id'] == -1
    db.create_share(conn, 'new', 'asset', asset)
    db.init_db(cfg.db_path)
    assert db.get_share(conn, 'new')['target_id'] == asset


def test_direct_album_deletion_revokes_share_before_id_reuse(base):
    cfg, conn = config(base)
    album = db.create_album(conn, 'Original')
    db.create_share(conn, 'album-link', 'album', album)
    conn.execute('DELETE FROM albums WHERE id=?', (album,))
    conn.commit()
    new_album = db.create_album(conn, 'Private')
    assert new_album == album
    assert db.get_share(conn, 'album-link')['target_id'] == -1


def test_restore_collision_preserves_manual_date_album_and_favorite(base):
    cfg, conn = config(base)
    root = Path(cfg.active_root)
    source = root / 'secret.jpg'
    Image.new('RGB', (16, 16), 'red').save(source)
    user = auth.create_user(conn, 'family', '', role='family')
    asset = insert(conn, cfg, source.name, visibility=2, vis_source='item', date_source='manual')
    album = db.create_album(conn, 'Memories')
    conn.execute('INSERT INTO album_items VALUES(?,?,?)', (album, asset, 1.0))
    conn.execute('INSERT INTO user_assets(user_id,asset_id,favorite,rating) VALUES(?,?,1,5)', (user.id, asset))
    conn.commit()
    recycle.recycle(conn, [asset])
    Image.new('RGB', (16, 16), 'blue').save(source)
    occupant = source.read_bytes()
    result = recycle.restore(conn, [recycle.listing(conn)[0]['id']])
    assert result['restored'] == 1 and source.read_bytes() == occupant
    restored = conn.execute('SELECT * FROM assets').fetchone()
    assert restored['rel_path'] != source.name
    assert (root / restored['rel_path']).exists()
    assert (restored['visibility'], restored['vis_source'], restored['date_source']) == (2, 'item', 'manual')
    assert conn.execute('SELECT asset_id FROM album_items').fetchone()[0] == restored['id']
    assert tuple(conn.execute('SELECT favorite,rating FROM user_assets').fetchone()) == (1, 5)


def test_legacy_recycle_entry_restores_hidden(base):
    cfg, conn = config(base)
    source = Path(cfg.active_root) / 'old.jpg'
    Image.new('RGB', (16, 16), 'red').save(source)
    asset = insert(conn, cfg, source.name)
    recycle.recycle(conn, [asset])
    conn.execute('UPDATE recycled SET metadata=NULL')
    conn.commit()
    result = recycle.restore(conn, [recycle.listing(conn)[0]['id']])
    assert result['restored'] == 1
    assert conn.execute('SELECT visibility FROM assets').fetchone()[0] == 2


def test_changed_completed_file_requeues_even_at_same_size(base):
    cfg, conn = config(base)
    store.init_schema(conn)
    source = Path(cfg.active_root) / 'clip.mp4'
    source.write_bytes(b'old')
    store.remember(conn, cfg.active_root, source.name, size=3)
    store.record_done(conn, cfg.active_root, source.name, remote_id='done')
    source.write_bytes(b'new')
    assert store.queue_missing(conn, [dict(root=cfg.active_root, rel_path=source.name, size=3)]) == 1
    assert store.pending_batch(conn)[0]['resume_url'] == ''


def test_changed_source_during_upload_is_not_marked_done(base):
    cfg, conn = config(base)
    store.init_schema(conn)
    source = Path(cfg.active_root) / 'clip.mp4'
    source.write_bytes(b'old')
    store.remember(conn, cfg.active_root, source.name, size=3)
    fake = FakeDrive()
    client = drive_client(fake)
    original = client.upload_file
    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        source.write_bytes(b'new')
        return result
    client.upload_file = changing
    engine = SyncEngine(connect=lambda: client, open_db=lambda: conn)
    assert engine._one(conn, client, store.pending_batch(conn)[0]) == 'wait'
    row = conn.execute('SELECT state,resume_url FROM cloud_uploads').fetchone()
    assert row['state'] != 'done' and row['resume_url'] == ''


def test_proxy_cache_rejects_another_source_or_legacy_cache(base):
    from ninaivu.utils.proxies import ProxyStore
    from ninaivu.utils.source_version import source_version, stamp_source
    first, second = base / 'first.avi', base / 'second.avi'
    first.write_bytes(b'first'); second.write_bytes(b'second')
    cache = ProxyStore(base)
    output = cache.path_for(1)
    output.parent.mkdir()
    output.write_bytes(b'cached')
    assert cache.ready(1, first) is None
    stamp_source(output, source_version(first))
    assert cache.ready(1, first) == output
    assert cache.ready(1, second) is None


def test_measurement_checks_cancellation_and_marks_partial_counts(base):
    from ninaivu.archive.scanner import _measure_folder
    for index in range(4):
        (base / str(index)).write_bytes(b'x')
    assert _measure_folder(base, limit=2) == (2, 2, True)
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        _measure_folder(base, checkpoint=cancel)
    assert calls == 3


def test_backup_rejects_missing_pending_bytes(base):
    cfg, conn = config(base)
    conn.execute("INSERT INTO pending_uploads(storage_key,filename,root,uploaded_at,record) VALUES('missing','x.jpg',?,0,'{}')", (cfg.active_root,))
    conn.commit()
    assert backup.snapshot(cfg.state_dir, base / 'backups') is None
