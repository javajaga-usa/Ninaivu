"""The index, in Drive too — and nothing in it that must not be.

A copy of the index goes to Drive so that a lost computer loses nothing but
time: full folder paths, faces, albums, who may see what. These check both
halves of that: that the copy carries no key, sign-in, password or session,
and that a new computer with nothing but the Google account gets every file
back where it was.
"""

import json
import sqlite3
import tarfile
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from fake_drive import FakeDrive
from ninaivu.cloud import crypto, index_copy, keyring, store
from ninaivu.cloud import drive as drive_mod
from ninaivu.server import auth
from ninaivu.storage import backup, db

PASSPHRASE = "correct horse battery staple"


@pytest.fixture()
def home(app, people):
    """A library, a signed-in session, vectors, secrets, and a backup in Drive."""
    services = app.config["MV_SERVICES"]
    cfg = services.cfg
    conn = db.connect(cfg.db_path)
    auth.start_session(conn, people["admin"].id, "test", face="admin")
    for row in conn.execute("SELECT id FROM assets LIMIT 3"):
        db.store_embedding(conn, row["id"], "m", 4, np.ones(4, "float32").tobytes())
    cfg.notify_smtp_password = "hunter2-smtp"
    cfg.notify_webhook = "https://ntfy.sh/secret-topic-123"
    cfg.save()
    state = Path(cfg.state_dir)
    (state / "google.json").write_text('{"refresh_token": "SECRET-REFRESH"}')
    (state / "tls").mkdir(exist_ok=True)
    (state / "tls" / "ca.key").write_text("PRIVATE KEY")

    fake = FakeDrive()
    services.cloud.creds = drive_mod.Credentials(
        client_id="c", client_secret="s", refresh_token="r", access_token="a",
        expires_at=9e9)
    services.cloud.client = lambda: drive_mod.DriveClient(creds=services.cloud.creds,
                                                          transport=fake)
    engine = services.cloud.engine()
    engine.start()
    engine._thread.join(60)
    assert store.summary(conn)["done"] >= 10
    return SimpleNamespace(services=services, cfg=cfg, conn=conn, fake=fake,
                           copy=services.index_copy, state=state)


def unpack(bundle: Path, into: Path) -> Path:
    return backup.extract(bundle, into)


# -- what is in the copy ---------------------------------------------------------

def test_the_copy_carries_no_secrets(home, tmp_path):
    bundle = index_copy.make_bundle(home.state, tmp_path / "out")
    with tarfile.open(bundle) as tar:
        names = tar.getnames()
    assert "state/index.db" in names
    for secret in ("google.json", "cloud-encryption.json", "tls", "ninaivu-ca.key"):
        assert not any(n.startswith(f"state/{secret}") for n in names), secret
    state = unpack(bundle, tmp_path / "x")
    settings = json.loads((state / "config.json").read_text())
    assert settings.get("notify_smtp_password", "") == ""
    assert settings.get("notify_webhook", "") == ""
    raw = b"".join(p.read_bytes() for p in state.rglob("*") if p.is_file())
    for secret in (b"SECRET-REFRESH", b"hunter2-smtp", b"secret-topic-123", b"PRIVATE KEY"):
        assert secret not in raw


def test_the_copy_carries_no_sessions_and_no_rebuildable_vectors(home, tmp_path):
    state = unpack(index_copy.make_bundle(home.state, tmp_path / "out"), tmp_path / "x")
    with closing(sqlite3.connect(str(state / "index.db"))) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
        # ...so a restored library analyses every photograph again.
        assert conn.execute("SELECT MAX(ai_version) FROM assets").fetchone()[0] == 0
        # What cannot be rebuilt is all there.
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] >= 10
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 3
        assert conn.execute("SELECT COUNT(*) FROM cloud_uploads WHERE state='done'"
                            ).fetchone()[0] >= 10


def test_the_restore_tool_accepts_the_copy(home, tmp_path):
    result = backup.verify_bundle(index_copy.make_bundle(home.state, tmp_path / "out"))
    assert result["ok"], result["error"]
    assert result["assets"] >= 10


# -- keeping it in Drive ------------------------------------------------------------

def copies(fake):
    return {f["name"]: fid for fid, f in fake.files.items()
            if f["name"].startswith("ninaivu-index")}


def test_two_slots_updated_in_turn_and_never_more(home):
    first = home.copy.run()
    assert first["ok"], first
    second = home.copy.run()
    assert len(copies(home.fake)) == 2
    assert first["slot"] != second["slot"]
    third = home.copy.run()
    assert third["slot"] == first["slot"]
    assert len(copies(home.fake)) == 2, "a copy was added instead of replaced"
    assert home.fake.updates, "the older slot was not updated in place"
    folder = home.fake.files[third["remote_id"]]["parents"][0]
    assert home.fake.folders[folder]["name"] == index_copy.FOLDER


def test_it_is_encrypted_when_uploads_are(home):
    keyring.create(home.state, PASSPHRASE, PASSPHRASE)
    home.cfg.cloud_encrypt = True
    result = home.copy.run()
    assert result["encrypted"] and result["name"].endswith(".ninaivu")
    assert home.fake.files[result["remote_id"]]["bytes"].startswith(crypto.MAGIC_V2)


def test_turning_encryption_on_leaves_no_plain_copy_behind(home):
    home.copy.run()
    home.copy.run()
    keyring.create(home.state, PASSPHRASE, PASSPHRASE)
    home.cfg.cloud_encrypt = True
    home.copy.run()
    home.copy.run()
    names = copies(home.fake)
    assert len(names) == 2 and all(n.endswith(".ninaivu") for n in names)


def test_it_will_not_send_in_the_clear_when_the_key_is_missing(home):
    home.cfg.cloud_encrypt = True
    result = home.copy.run()
    assert not result["ok"] and "key" in result["error"]
    assert copies(home.fake) == {}


def test_it_is_sent_daily_and_only_when_something_changed(home):
    clock = SimpleNamespace(now=time.time())
    home.copy._clock = lambda: clock.now
    home.cfg.cloud_enabled = True
    assert home.copy.due()
    home.copy.run()
    assert not home.copy.due()
    clock.now += 2 * 86400                     # a day on, but nothing changed
    assert not home.copy.due()
    home.conn.execute("UPDATE assets SET caption='new' WHERE id=(SELECT MIN(id) FROM assets)")
    home.conn.commit()
    (home.state / "index.db").touch()
    assert home.copy.due()
    home.copy._hold = lambda: "someone is watching a video"
    assert not home.copy.due()


# -- a new computer ---------------------------------------------------------------------

def new_computer(home, tmp_path):
    """A second Ninaivu with an empty index and only the Google account."""
    from ninaivu.cloud.service import CloudService

    state = tmp_path / "new-state"
    state.mkdir()
    db.init_db(state / "index.db")
    cfg = SimpleNamespace(**{**vars(home.cfg), "state_dir": state,
                             "db_path": state / "index.db", "cloud_encrypt": False,
                             "roots": []})
    service = CloudService(cfg, connect_db=lambda: db.connect(state / "index.db"))
    service.creds = home.services.cloud.creds
    service.client = home.services.cloud.client
    return service


def test_a_new_computer_gets_every_folder_back_as_it_was(home, tmp_path):
    home.copy.run()
    service = new_computer(home, tmp_path)
    items = service.restore_items({"source": "drive"})
    assert service.index_found, "the index copy was not used"
    deep = [i for i in items if i.rel_path.count("/") >= 3]
    assert deep, "the full paths were lost"
    assert all(i.sha256 for i in items), "not checked against what was sent"

    target = tmp_path / "rebuilt"
    service.restore_start(items, destination=str(target), key=None)
    service._restore.join(60)
    state = service.restore_status()
    assert state["restored"] == len(items), state
    original = Path(home.cfg.active_root)
    for item in items:
        assert (target / item.rel_path).read_bytes() == (original / item.rel_path).read_bytes()
    assert "backup_restore.py restore" in state["message"]
    assert Path(service.index_found["bundle"]).is_file()


def test_an_encrypted_copy_asks_for_the_key_first(home, tmp_path):
    record = keyring.create(home.state, PASSPHRASE, PASSPHRASE)
    home.cfg.cloud_encrypt = True
    home.copy.run()
    service = new_computer(home, tmp_path)
    with pytest.raises(index_copy.NeedsKey):
        service.restore_items({"source": "drive"})
    items = service.restore_items({"source": "drive"},
                                  recovery=keyring.recovery_document(record))
    assert items and service.index_found["encrypted"]


def test_the_wizard_says_it_read_the_index_copy(app, home):
    from conftest import ADMIN, login

    home.copy.run()
    admin = login(app.test_client(), *ADMIN)
    preview = admin.post("/api/cloud/restore/preview",
                         json={"scope": {"source": "drive"}}).get_json()
    assert preview["from_index_copy"] and preview["files"] >= 10


def test_an_encrypted_copy_makes_the_wizard_ask_for_the_key(app, home):
    from conftest import ADMIN, login

    record = keyring.create(home.state, PASSPHRASE, PASSPHRASE)
    home.cfg.cloud_encrypt = True
    home.copy.run()
    keyring.path(home.state).unlink()          # as on a computer without it
    admin = login(app.test_client(), *ADMIN)
    refused = admin.post("/api/cloud/restore/preview",
                         json={"scope": {"source": "drive"}})
    assert refused.status_code == 409 and refused.get_json()["needs_key"]
    ok = admin.post("/api/cloud/restore/preview",
                    json={"scope": {"source": "drive"},
                          "recovery": keyring.recovery_document(record)})
    assert ok.status_code == 200 and ok.get_json()["from_index_copy"]["encrypted"]


def test_the_console_sends_a_copy_now(app, home):
    from conftest import ADMIN, FAMILY, login

    admin = login(app.test_client(), *ADMIN)
    assert admin.post("/api/cloud/index-copy/run").get_json()["ok"]
    deadline = time.time() + 60
    status = {}
    while time.time() < deadline:
        status = admin.get("/api/cloud/index-copy").get_json()
        if not status["running"] and status["last"]:
            break
        time.sleep(0.1)
    assert status["last"], status
    assert status["last"]["name"].startswith("ninaivu-index-a")
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/cloud/index-copy").status_code in (401, 403, 404)
