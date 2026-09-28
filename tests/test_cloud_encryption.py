"""Encrypted Drive backup: the key, the file format, the upload, and getting it back.

The promise is that nothing readable reaches Drive once encryption is on, and
that the photographs can still be recovered from Drive with the recovery file,
or with the passphrase, when this machine is gone. These drive the real engine
and client through the fake Drive and then decrypt what landed there.
"""
import json
import os
import sys
from pathlib import Path

import pytest

from fake_drive import FakeDrive
from ninaivu.cloud import crypto, keyring, store
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import engine as engine_mod
from ninaivu.cloud.upload_cache import UploadCache
from ninaivu.storage import db

pytest.importorskip("cryptography")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import cloud_decrypt  # noqa: E402

PASSPHRASE = "a long family passphrase"


# --- the file format ---------------------------------------------------------

@pytest.mark.parametrize("size", [0, 1, crypto.FILE_CHUNK_BYTES, crypto.FILE_CHUNK_BYTES + 17])
def test_v2_round_trips_across_chunk_boundaries(tmp_path, size):
    key, key_id = os.urandom(32), keyring.key_id(os.urandom(32))
    plain = tmp_path / "photo.jpg"
    plain.write_bytes(os.urandom(size))
    sealed = tmp_path / "photo.jpg.ninaivu"
    assert crypto.encrypt_file_v2(plain, sealed, key, key_id) == size + crypto.V2_OVERHEAD
    assert sealed.stat().st_size == size + crypto.V2_OVERHEAD
    assert crypto.read_key_id(sealed) == key_id
    opened = tmp_path / "out.jpg"
    assert crypto.decrypt_file_v2(sealed, opened, key) == size
    assert opened.read_bytes() == plain.read_bytes()


def test_a_changed_byte_or_the_wrong_key_writes_nothing(tmp_path):
    key, key_id = os.urandom(32), b"12345678"
    plain = tmp_path / "photo.jpg"
    plain.write_bytes(b"family" * 1000)
    sealed = tmp_path / "sealed.ninaivu"
    crypto.encrypt_file_v2(plain, sealed, key, key_id)
    data = bytearray(sealed.read_bytes())
    data[len(data) // 2] ^= 1
    tampered = tmp_path / "tampered.ninaivu"
    tampered.write_bytes(bytes(data))
    for source, attempt_key in ((tampered, key), (sealed, os.urandom(32))):
        target = tmp_path / f"out-{source.stem}-{attempt_key[:2].hex()}.jpg"
        with pytest.raises(ValueError):
            crypto.decrypt_file_v2(source, target, attempt_key)
        assert not target.exists(), "unauthenticated plaintext was published"


# --- the key -------------------------------------------------------------------

def test_a_key_is_made_once_from_a_good_passphrase(tmp_path):
    with pytest.raises(ValueError, match="at least"):
        keyring.create(tmp_path, "short", "short")
    with pytest.raises(ValueError, match="match"):
        keyring.create(tmp_path, PASSPHRASE, PASSPHRASE + "!")
    record = keyring.create(tmp_path, PASSPHRASE, PASSPHRASE)
    assert keyring.load(tmp_path)["key_id"] == record["key_id"]
    with pytest.raises(ValueError, match="already exists"):
        keyring.create(tmp_path, PASSPHRASE, PASSPHRASE)
    stored = json.loads(keyring.path(tmp_path).read_text())
    assert PASSPHRASE not in json.dumps(stored), "the passphrase was stored"
    if os.name != "nt":
        assert keyring.path(tmp_path).stat().st_mode & 0o077 == 0, "the key file is readable by others"


def test_the_passphrase_and_drive_params_recreate_the_key(tmp_path):
    record = keyring.create(tmp_path, PASSPHRASE, PASSPHRASE)
    params = keyring.public_params(record)
    assert "key" not in params, "the Drive params file must not hold the key"
    key, _ = keyring.key_material(record)
    assert keyring.key_from(passphrase=PASSPHRASE, params=params) == key
    assert keyring.key_from(recovery=keyring.recovery_document(record)) == key
    with pytest.raises(ValueError, match="not the key"):
        keyring.key_from(passphrase="the wrong passphrase", params=params)


def test_a_damaged_key_file_is_not_used(tmp_path):
    keyring.create(tmp_path, PASSPHRASE, PASSPHRASE)
    record = json.loads(keyring.path(tmp_path).read_text())
    record["key_id"] = "0000000000000000"
    keyring.path(tmp_path).write_text(json.dumps(record))
    assert keyring.load(tmp_path) is None


# --- the upload ------------------------------------------------------------------

@pytest.fixture()
def setup(tmp_path):
    db_path = tmp_path / "index.db"
    store.init_schema(db.init_db(db_path))
    fake = FakeDrive()
    creds = drive_mod.Credentials(client_id="cid", client_secret="secret", refresh_token="r",
                                  access_token="a", expires_at=9e9, folder_name="Ninaivu")
    client = drive_mod.DriveClient(creds=creds, transport=fake)
    library = tmp_path / "lib"
    (library / "2019").mkdir(parents=True)
    (library / "2019" / "beach.jpg").write_bytes(os.urandom(3 * 1024 * 1024 + 5))
    (library / "2019" / "pier.jpg").write_bytes(b"P" * 2048)
    state = tmp_path / "state"
    record = keyring.create(state, PASSPHRASE, PASSPHRASE)
    return db_path, fake, client, library, state, record


def _queue(conn, library):
    for path in sorted(library.rglob("*.jpg")):
        store.remember(conn, str(library), path.relative_to(library).as_posix(),
                       size=path.stat().st_size, filename=path.name)


def _engine(db_path, client, state, encryption, published):
    def publish(drive_client):
        if published:
            return
        published.append(drive_client.upload_file(
            _params_file(state), "ninaivu-encryption.json", drive_client.ninaivu_root()))

    return engine_mod.SyncEngine(connect=lambda: client, open_db=lambda: db.connect(db_path),
                                 encryption=encryption, cache=UploadCache(state / "cache"),
                                 publish_params=publish)


def _params_file(state):
    target = state / "params.json"
    target.write_text(json.dumps(keyring.public_params(keyring.load(state))))
    return target


def _run(engine):
    engine.start()
    engine._thread.join(60)
    assert not engine.running


def test_nothing_readable_reaches_drive_and_it_all_comes_back(setup, tmp_path):
    db_path, fake, client, library, state, record = setup
    conn = db.connect(db_path)
    _queue(conn, library)
    published = []
    _run(_engine(db_path, client, state, lambda: keyring.key_material(keyring.load(state)), published))

    names = fake.uploaded_names()
    assert sorted(names) == ["beach.jpg.ninaivu", "ninaivu-encryption.json", "pier.jpg.ninaivu"]
    params = json.loads(fake.content("ninaivu-encryption.json"))
    assert "key" not in params and params["key_id"] == record["key_id"]
    for name in ("beach.jpg", "pier.jpg"):
        sent = fake.content(f"{name}.ninaivu")
        original = (library / "2019" / name).read_bytes()
        assert sent.startswith(crypto.MAGIC_V2) and original[:64] not in sent

    rows = conn.execute("SELECT encrypted, state FROM cloud_uploads").fetchall()
    assert all(r["encrypted"] == 1 and r["state"] == store.DONE for r in rows)
    assert store.summary(conn)["encrypted"] == 2
    assert not list((state / "cache").glob("*")), "ciphertext was left behind in the cache"

    # The day the machine is gone: download the folder, decrypt with the recovery file.
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    for name in ("beach.jpg.ninaivu", "pier.jpg.ninaivu"):
        (downloaded / name).write_bytes(fake.content(name))
    recovery = tmp_path / "recovery.json"
    recovery.write_text(json.dumps(keyring.recovery_document(record)))
    out = tmp_path / "restored"
    assert cloud_decrypt.main(["--recovery", str(recovery), str(downloaded), str(out)]) == 0
    assert (out / "beach.jpg").read_bytes() == (library / "2019" / "beach.jpg").read_bytes()

    # ...or with only the passphrase and the params file from Drive.
    params_file = tmp_path / "ninaivu-encryption.json"
    params_file.write_text(json.dumps(params))
    out2 = tmp_path / "restored-with-passphrase"
    assert cloud_decrypt.main(["--params", str(params_file), "--passphrase", PASSPHRASE,
                               str(downloaded), str(out2)]) == 0
    assert (out2 / "pier.jpg").read_bytes() == b"P" * 2048
    # And it never overwrites what is already there.
    assert cloud_decrypt.main(["--recovery", str(recovery), str(downloaded), str(out)]) == 1


def test_encryption_on_without_a_usable_key_uploads_nothing(setup):
    db_path, fake, client, library, state, _ = setup
    conn = db.connect(db_path)
    _queue(conn, library)

    def missing():
        raise RuntimeError("encryption is on but the encryption key is missing")

    engine = _engine(db_path, client, state, missing, [])
    _run(engine)
    assert fake.uploaded_names() == []
    assert "key is missing" in engine.state.snapshot()["last_error"]
    assert store.summary(conn)["done"] == 0


def test_a_resumed_upload_sends_the_ciphertext_it_started(setup, monkeypatch):
    """Encryption makes new bytes every time, so a resume must not re-encrypt."""
    db_path, fake, client, library, state, _ = setup
    conn = db.connect(db_path)
    _queue(conn, library)
    engine = _engine(db_path, client, state, lambda: keyring.key_material(keyring.load(state)), [])
    row = dict(conn.execute("SELECT * FROM cloud_uploads WHERE filename='pier.jpg'").fetchone())
    cache = UploadCache(state / "cache").path_for(row["root"], row["rel_path"])
    crypto.encrypt_file_v2(library / "2019" / "pier.jpg", cache, *keyring.key_material(keyring.load(state)))
    before = cache.read_bytes()
    row["resume_url"] = "https://upload.example/session"

    encrypted_again = []
    monkeypatch.setattr(engine_mod.crypto, "encrypt_file_v2", lambda *a, **k: encrypted_again.append(a))
    sent = {}

    def upload_file(path, name, parent, **kwargs):
        sent.update(path=Path(path), name=name, resume=kwargs.get("resume_url"), data=Path(path).read_bytes())
        return "file-resumed"

    monkeypatch.setattr(client, "upload_file", upload_file)
    monkeypatch.setattr(engine, "_folder", lambda c, r: "folder")
    assert engine._one(conn, client, row) == "sent"
    assert encrypted_again == [], "the file was encrypted again, so the resume would send different bytes"
    assert sent["path"] == cache and sent["data"] == before and sent["resume"] == row["resume_url"]
    assert sent["name"] == "pier.jpg.ninaivu"


def test_the_upload_cache_deletes_only_its_own_files(tmp_path):
    cache = UploadCache(tmp_path / "cache")
    mine = cache.path_for("/lib", "a.jpg")
    mine.write_bytes(b"x")
    cache.discard(mine)
    assert not mine.exists()
    photo = tmp_path / "photo.ninaivu"
    photo.write_bytes(b"precious")
    with pytest.raises(ValueError):
        cache.discard(photo)
    with pytest.raises(ValueError):
        cache.discard(tmp_path / "cache" / "notes.txt")
    assert photo.read_bytes() == b"precious"


# --- the console -------------------------------------------------------------------

def test_the_console_makes_the_key_and_guards_the_switch(app, people):
    from conftest import ADMIN, FAMILY, login
    admin = login(app.test_client(), *ADMIN)
    cfg = app.config["MV_CONFIG"]

    refused = admin.post("/api/cloud/settings", json={"encrypt": True})
    assert refused.status_code == 400 and not cfg.cloud_encrypt, "encryption went on with no key"
    assert admin.post("/api/cloud/encryption/key", json={"passphrase": "short", "confirm": "short"}).status_code == 400

    made = admin.post("/api/cloud/encryption/key", json={"passphrase": PASSPHRASE, "confirm": PASSPHRASE})
    assert made.status_code == 200, made.get_json()
    recovery = made.get_json()["recovery"]
    assert recovery["key"] and made.get_json()["encryption"]["key_exists"]
    assert admin.post("/api/cloud/encryption/key", json={"passphrase": PASSPHRASE, "confirm": PASSPHRASE}).status_code == 409

    assert admin.post("/api/cloud/settings", json={"encrypt": True}).get_json()["encryption"]["enabled"]
    assert json.loads(Path(cfg.config_path).read_text(encoding="utf-8"))["cloud_encrypt"] is True
    assert admin.get("/api/cloud/encryption/recovery").get_json()["key"] == recovery["key"]

    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/cloud/encryption/recovery").status_code in (401, 403, 404)


def test_the_service_refuses_to_upload_in_the_clear_and_publishes_params_once(setup):
    from types import SimpleNamespace
    from ninaivu.cloud.service import CloudService
    db_path, fake, client, _, state, record = setup
    cfg = SimpleNamespace(state_dir=state, cloud_folder_name="Ninaivu", cloud_encrypt=True,
                          cloud_rate_kbps=0,
                          cloud_window_start="", cloud_window_end="")
    service = CloudService(cfg, lambda: db.connect(db_path))
    assert service._encryption() == keyring.key_material(record)

    service._publish_params(client)
    service._publish_params(client)
    assert fake.uploaded_names().count("ninaivu-encryption.json") == 1, "the params file went up twice"
    assert keyring.load(state)["params_remote_id"]

    keyring.path(state).unlink()
    with pytest.raises(RuntimeError, match="missing"):
        service._encryption()
    cfg.cloud_encrypt = False
    assert service._encryption() is None
