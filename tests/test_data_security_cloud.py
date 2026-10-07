"""Where the household's data goes, and who else can read it: the data-security pass.

The copy of the index in Drive, the local backup bundles and the folder they
go to, the settings file written by a re-root, the cloud key's passphrase, the
AI server's address, the mail server's connection, the webhook address on the
settings page, the desktop panel's logs, how long the sign-in record and the
archive's logs are kept, Hugging Face's usage reports, and Windows folder
permissions. One test (or a few) per finding.
"""

import json
import os
import smtplib
import sqlite3
import subprocess
import sys
import tarfile
import time
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ADMIN, login
from fake_drive import FakeDrive
from ninaivu.cloud import drive as drive_mod
from ninaivu.cloud import index_copy, keyring, store
from ninaivu.server import auth, settings_groups
from ninaivu.storage import backup, db

ON_POSIX = os.name == "posix"


# -- 1. the copy of the index in Drive ------------------------------------------------

def _copy_with(tmp_path, encryption):
    state = tmp_path / "state"
    state.mkdir()
    index = state / "index.db"
    store.init_schema(db.init_db(index))
    fake = FakeDrive()
    gdrive = drive_mod.DriveClient(creds=drive_mod.Credentials(
        client_id="cid", client_secret="secret", refresh_token="r", access_token="a",
        expires_at=9e9, folder_name="Ninaivu"), transport=fake)
    cfg = SimpleNamespace(state_dir=state, cloud_rate_kbps=0, cloud_index_every_hours=24,
                          cloud_enabled=True, cloud_encrypt=encryption is not None)
    service = SimpleNamespace(creds=gdrive.creds, client=lambda: gdrive,
                              _encryption=lambda: encryption,
                              window=lambda: SimpleNamespace(is_open=lambda: True))
    return index_copy.IndexCopy(cfg, service, connect_db=lambda: db.connect(index)), fake


def test_the_index_copy_is_never_sent_unencrypted(tmp_path):
    copy, fake = _copy_with(tmp_path, encryption=None)
    result = copy.run()
    assert not result["ok"]
    assert "only sent encrypted" in result["error"] and "Mugil" in result["error"]
    assert fake.uploads == [], "something went to Drive in the clear"
    # And it is not even tried every half hour: the schedule says why instead.
    copy.error = ""
    assert not copy.due()
    assert "only sent encrypted" in copy.error


def test_the_index_copy_carries_no_sign_in_record_counters_or_bin_details(tmp_path):
    index = tmp_path / "index.db"
    conn = db.init_db(index)
    auth.init_auth_schema(conn)
    auth.audit(conn, None, "login_failed", "my-password-typed-as-a-name")
    conn.execute("INSERT INTO auth_limits(key, attempts) VALUES(?, '[]')",
                 ("*|share:LIVE-SHARE-TOKEN-456",))
    conn.execute("INSERT INTO recycled(root, rel_path, filename, bin_path, deleted_at, metadata) "
                 "VALUES('/lib', 'a.jpg', 'a.jpg', '/bin/a.jpg', 1, ?)",
                 (json.dumps({"faces": ["Grandma"], "gps": [12.9, 80.2]}),))
    conn.commit()
    conn.close()

    index_copy._slim(index)

    with closing(sqlite3.connect(str(index))) as slim:
        assert slim.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
        assert slim.execute("SELECT COUNT(*) FROM auth_limits").fetchone()[0] == 0
        assert slim.execute("SELECT metadata FROM recycled").fetchone()[0] is None
        assert slim.execute("SELECT COUNT(*) FROM recycled").fetchone()[0] == 1
    raw = index.read_bytes()
    for secret in (b"my-password-typed-as-a-name", b"LIVE-SHARE-TOKEN-456", b"Grandma"):
        assert secret not in raw


# -- 2. local backup bundles and where they go -------------------------------------------

def _state_with_secrets(cfg):
    cfg.save()
    state = Path(cfg.state_dir)
    (state / "google.json").write_text('{"refresh_token": "SECRET-REFRESH"}')
    (state / "cloud-encryption.json").write_text('{"key": "THE-CLOUD-KEY"}')
    return state


def _names(bundle: Path) -> set[str]:
    with tarfile.open(bundle) as tar:
        return set(tar.getnames())


def test_a_backup_folder_inside_the_library_or_the_second_copy_is_refused(scanned, tmp_path):
    cfg, _, _ = scanned
    inside = Path(cfg.roots[0]) / "backups"
    with pytest.raises(settings_groups.BadValue, match="library"):
        settings_groups.apply(cfg, {"backup_dir": str(inside)})
    assert cfg.backup_dir == ""

    cfg.mirror_dir = str(tmp_path / "second-copy")
    with pytest.raises(settings_groups.BadValue, match="second copy"):
        settings_groups.apply(cfg, {"backup_dir": str(tmp_path / "second-copy" / "b")})

    assert settings_groups.apply(cfg, {"backup_dir": str(tmp_path / "elsewhere")}) == ["backup_dir"]


def test_a_backup_is_refused_when_its_folder_has_become_part_of_the_library(scanned):
    cfg, _, _ = scanned
    cfg.backup_dir = str(Path(cfg.roots[0]) / "backups")      # set before the check existed
    keeper = backup.BackupKeeper(cfg)
    assert keeper.run() is None
    assert "library" in keeper.last_error
    assert not (Path(cfg.roots[0]) / "backups").exists()


def test_a_backup_outside_the_state_folder_leaves_the_drive_keys_at_home(scanned, tmp_path):
    cfg, _, _ = scanned
    _state_with_secrets(cfg)
    cfg.backup_dir = str(tmp_path / "usb-disk" / "ninaivu")
    made = backup.BackupKeeper(cfg).run()
    assert made is not None and made.parent == Path(cfg.backup_dir)
    names = _names(made)
    assert "state/index.db" in names and "state/config.json" in names
    assert "state/google.json" not in names
    assert "state/cloud-encryption.json" not in names
    with tarfile.open(made) as tar:
        manifest = json.load(tar.extractfile("state/backup_manifest.json"))
    assert manifest["left_out"] == sorted(backup.KEPT_HOME)
    assert backup.verify_bundle(made)["ok"]
    if ON_POSIX:
        assert Path(cfg.backup_dir).stat().st_mode & 0o077 == 0


def test_a_backup_in_the_state_folder_still_carries_everything(scanned):
    cfg, _, _ = scanned
    _state_with_secrets(cfg)
    made = backup.BackupKeeper(cfg).run()
    assert made is not None
    assert {"state/google.json", "state/cloud-encryption.json"} <= _names(made)


def test_restoring_a_bundle_without_the_keys_keeps_this_machines(tmp_path):
    from ninaivu.cli import backup_restore as tool

    existing, extracted, candidate = tmp_path / "live", tmp_path / "bundle", tmp_path / "new"
    existing.mkdir()
    for name in ("google.json", "cloud-encryption.json"):
        (existing / name).write_text(f"this machine's {name}")
    (existing / "index.db").write_bytes(b"old index")
    extracted.mkdir()
    (extracted / "index.db").write_bytes(b"index from the bundle")
    (extracted / "backup_manifest.json").write_text(json.dumps(
        {"files": {}, "left_out": list(backup.KEPT_HOME)}))

    tool._prepare_replacement(existing, extracted, candidate)
    assert (candidate / "index.db").read_bytes() == b"index from the bundle"
    for name in ("google.json", "cloud-encryption.json"):
        assert (candidate / name).read_text() == f"this machine's {name}"


# -- 3. the settings file a re-root writes ------------------------------------------------

@pytest.mark.skipif(not ON_POSIX, reason="file modes")
def test_a_reroot_writes_the_settings_owner_only_and_not_through_a_planted_link(tmp_path):
    from ninaivu.storage import reroot

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"roots": ["/old"], "active_root": "/old",
                                "notify_smtp_password": "hunter2"}))
    outside = tmp_path / "somebody-elses.txt"
    outside.write_text("theirs")
    (tmp_path / "config.json.reroot.tmp").symlink_to(outside)
    old = os.umask(0o022)
    try:
        assert reroot.rewrite_config(path, reroot.normalise("/old"), "/new", dry_run=False)
    finally:
        os.umask(old)
    assert outside.read_text() == "theirs"
    assert json.loads(path.read_text())["roots"] == ["/new"]
    assert path.stat().st_mode & 0o777 == 0o600


# -- 4. the passphrase for the cloud key --------------------------------------------------

@pytest.mark.parametrize("weak", ["aaaaaaaaaaaaaaaa", "abcabcabcabcabc", "121212121212",
                                  "password password password", "Summer summer summer",
                                  "aabbccaabbccaabbcc"])
def test_a_passphrase_of_one_repeated_letter_or_word_is_refused(tmp_path, weak):
    with pytest.raises(ValueError, match="too easy to guess"):
        keyring.create(tmp_path, weak, weak)
    assert not keyring.path(tmp_path).exists()


def test_an_ordinary_passphrase_is_still_accepted(tmp_path):
    record = keyring.create(tmp_path, "correct horse battery", "correct horse battery")
    assert record["key_id"]
    with pytest.raises(ValueError, match="at least"):
        keyring.create(tmp_path / "x", "short", "short")


# -- 5. the AI server's address -------------------------------------------------------------

def test_an_ai_server_host_name_is_accepted_only_over_https():
    comfyui = pytest.importorskip("ninaivu_studio.ai_server.comfyui")
    assert comfyui.check_address("http://192.168.1.50:8188") == "http://192.168.1.50:8188"
    assert comfyui.check_address("http://gpu-box.local:8188") == "http://gpu-box.local:8188"
    assert comfyui.check_address("https://comfy.example.com") == "https://comfy.example.com"
    with pytest.raises(ValueError, match="host name"):
        comfyui.check_address("http://comfy.example.com")
    with pytest.raises(ValueError, match="internet"):
        comfyui.check_address("https://8.8.8.8")


def test_the_advanced_page_applies_the_same_rule_to_the_ai_server(monkeypatch):
    pytest.importorskip("ninaivu_studio.ai_server.comfyui")
    with pytest.raises(settings_groups.BadValue, match="host name"):
        settings_groups.coerce("ai_server_url", "http://comfy.example.com")
    with pytest.raises(settings_groups.BadValue, match="internet"):
        settings_groups.coerce("ai_server_url", "http://8.8.8.8:8188")
    assert settings_groups.coerce("ai_server_url", "http://10.0.0.5:8188/") == "http://10.0.0.5:8188"


def test_without_the_studio_the_advanced_page_keeps_its_own_check(monkeypatch):
    # The extension not installed: the core must still import and save.
    monkeypatch.setitem(sys.modules, "ninaivu_studio", None)
    monkeypatch.setitem(sys.modules, "ninaivu_studio.ai_server", None)
    assert settings_groups.coerce("ai_server_url", "http://comfy.example.com") \
        == "http://comfy.example.com"
    with pytest.raises(settings_groups.BadValue):
        settings_groups.coerce("ai_server_url", "ftp://comfy.example.com")


# -- 6. the mail server -----------------------------------------------------------------------

class _Mail:
    """Stands in for smtplib's two connection classes, recording what was done."""

    def __init__(self, calls, kind):
        self.calls, self.kind = calls, kind

    def __call__(self, host, port, timeout=None, context=None):
        self.calls.append((self.kind, host, port))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        assert context is not None and context.verify_mode == __import__("ssl").CERT_REQUIRED
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, message):
        self.calls.append(("send",))


@pytest.fixture()
def mail(monkeypatch):
    calls = []
    monkeypatch.setattr(smtplib, "SMTP", _Mail(calls, "plain"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Mail(calls, "ssl"))
    return calls


def _message():
    message = EmailMessage()
    message["Subject"], message["To"] = "t", "dad@example.com"
    message.set_content("x")
    return message


def test_port_465_is_tls_from_the_first_byte(mail):
    from ninaivu.utils import notify

    notify.send_mail(_message(), host="smtp.example.com", port=465, user="dad",
                     password="pw", tls=False)
    assert mail == [("ssl", "smtp.example.com", 465), ("login", "dad"), ("send",)]


def test_a_password_is_never_sent_over_a_plain_connection(mail):
    from ninaivu.utils import digest, notify

    with pytest.raises(ValueError, match="unencrypted"):
        notify.send_mail(_message(), host="smtp.example.com", port=25, user="dad",
                         password="pw", tls=False)
    assert mail == [], "a connection was opened"
    # The notifications and the weekly photograph both go the same way.
    sent = notify.Notifier(smtp_host="smtp.example.com", smtp_port=25, smtp_user="dad",
                           smtp_password="pw", smtp_to="dad@example.com",
                           smtp_tls=False)._email("t", "d")
    assert "unencrypted" in sent
    sent = digest.Digest(smtp_host="smtp.example.com", smtp_port=25, smtp_user="dad",
                         smtp_password="pw", smtp_tls=False, to="family@example.com"
                         ).send(_message())
    assert "unencrypted" in sent
    assert mail == []


def test_a_relay_on_this_computer_may_be_plain(mail):
    from ninaivu.utils import notify

    notify.send_mail(_message(), host="127.0.0.1", port=25, user="dad", password="pw",
                     tls=False)
    assert mail == [("plain", "127.0.0.1", 25), ("login", "dad"), ("send",)]
    mail.clear()
    notify.send_mail(_message(), host="smtp.example.com", port=587, user="dad",
                     password="pw", tls=True)
    assert mail == [("plain", "smtp.example.com", 587), ("starttls",), ("login", "dad"),
                    ("send",)]


# -- 7. the webhook address on the settings pages ---------------------------------------------

HOOK = "https://hooks.slack.com/services/T000/B000/SECRETxyz"


def test_the_advanced_page_shows_only_a_hint_of_the_webhook():
    from ninaivu.server.config import Config

    cfg = Config()
    cfg.notify_webhook = HOOK
    described = settings_groups.describe(cfg)
    item = next(s for g in described["groups"] for s in g["settings"]
                if s["name"] == "notify_webhook")
    assert item["secret"] is True and item["value"] is True
    assert item["hint"] == "https://hooks.slack.com/…"
    assert "SECRETxyz" not in json.dumps(described)

    # Blank, or the hint sent back as it was shown, keeps the saved address.
    assert settings_groups.apply(cfg, {"notify_webhook": ""}) == []
    assert settings_groups.apply(cfg, {"notify_webhook": item["hint"]}) == []
    assert cfg.notify_webhook == HOOK
    assert settings_groups.apply(cfg, {"notify_webhook": "https://ntfy.sh/new"}) \
        == ["notify_webhook"]


def test_the_notifications_page_never_hands_the_webhook_back(app, people, scanned):
    cfg, _, _ = scanned
    admin = login(app.test_client(), *ADMIN)
    admin.post("/api/admin/notifications", json={"webhook": HOOK, "events": ["integrity"]})
    assert cfg.notify_webhook == HOOK

    shown = admin.get("/api/admin/notifications").get_json()
    assert "SECRETxyz" not in json.dumps(shown)
    assert shown["form"]["webhook"] == "https://hooks.slack.com/…"
    assert shown["form"]["webhook_saved"] is True

    # The console's Save sends every field back as the form holds it.
    # The hint sent back unchanged keeps the address; a field emptied on
    # purpose removes it, as it always did.
    assert admin.post("/api/admin/notifications",
                      json={"webhook": shown["form"]["webhook"],
                            "events": ["integrity"]}).status_code == 200
    assert cfg.notify_webhook == HOOK
    admin.post("/api/admin/notifications", json={"webhook": "", "events": ["integrity"]})
    assert cfg.notify_webhook == ""
    admin.post("/api/admin/notifications", json={"webhook": HOOK, "events": ["integrity"]})
    admin.post("/api/admin/notifications", json={"webhook_clear": True})
    assert cfg.notify_webhook == ""


# -- 8. the desktop panel's logs -----------------------------------------------------------

def test_the_panel_logs_are_owner_only_and_do_not_grow_for_ever(tmp_path, monkeypatch):
    from ninaivu.desktop import control

    folder = tmp_path / ".ninaivu-control"
    log = folder / "server.log"
    with control.open_log(log) as out:
        out.write(b"http://192.168.1.20:8443 setup code 123456\n")
    if ON_POSIX:
        assert folder.stat().st_mode & 0o777 == 0o700
        assert log.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(control, "LOG_LIMIT", 10)
    with control.open_log(log) as out:
        out.write(b"new run\n")
    assert log.read_bytes() == b"new run\n"
    assert (folder / "server.log.1").read_bytes().startswith(b"http://192.168.1.20")


def test_the_sign_in_log_is_emptied_in_place(tmp_path, monkeypatch):
    from ninaivu.desktop import autostart, control

    monkeypatch.setitem(control._CONTROL_DIRS, tmp_path, tmp_path / ".ninaivu-control")
    folder = tmp_path / ".ninaivu-control"
    folder.mkdir(mode=0o755)
    log = folder / "autostart.log"
    log.write_bytes(b"x" * 64)
    monkeypatch.setattr(control, "LOG_LIMIT", 10)
    autostart.tidy_log(tmp_path)
    assert log.exists() and log.stat().st_size == 0
    assert not (folder / "autostart.log.1").exists()
    if ON_POSIX:
        assert folder.stat().st_mode & 0o777 == 0o700
        assert log.stat().st_mode & 0o777 == 0o600


# -- 9. how long the records are kept ---------------------------------------------------------

def test_old_sign_in_records_and_archive_logs_are_removed(cfg):
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    now = time.time()
    conn.execute("INSERT INTO audit(at, user_id, action, detail) VALUES(?, NULL, "
                 "'login_failed', 'two-years-old')", (now - 2 * 365 * 86400,))
    conn.execute("INSERT INTO audit(at, user_id, action, detail) VALUES(?, NULL, "
                 "'login_failed', 'last-week')", (now - 7 * 86400,))
    conn.commit()
    logs = Path(cfg.state_dir) / "archive-logs"
    logs.mkdir()
    for i in range(60):
        path = logs / f"run-{i:03d}-full.log"
        path.write_text("a folder name")
        os.utime(path, (now - (60 - i) * 3600,) * 2)
    (logs / "keep-me.txt").write_text("not a run log")

    removed = backup.tidy_records(cfg, now=now)

    assert removed == {"audit": 1, "archive_logs": 10}
    details = [r[0] for r in conn.execute("SELECT detail FROM audit")]
    assert details == ["last-week"]
    left = sorted(p.name for p in logs.glob("run-*.log"))
    assert len(left) == backup.ARCHIVE_LOGS_KEEP and left[0] == "run-010-full.log"
    assert (logs / "keep-me.txt").exists()


# -- 10. Hugging Face's usage reports --------------------------------------------------------

def test_hugging_face_reports_are_off_from_the_moment_ai_is_imported():
    env = {k: v for k, v in os.environ.items() if not k.startswith("HF_")}
    code = ("import os, ninaivu.ai; print(os.environ.get('HF_HUB_DISABLE_TELEMETRY'), "
            "os.environ.get('HF_HUB_DISABLE_IMPLICIT_TOKEN'), "
            "os.environ.get('HF_HUB_OFFLINE'))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=120, check=True,
                         cwd=str(Path(__file__).resolve().parents[1])).stdout.split()
    # Offline stays for when the weights are here: a first download must work.
    assert out == ["1", "1", "None"]


# -- 11. Windows folder permissions ------------------------------------------------------------

@pytest.fixture()
def windows(monkeypatch, tmp_path):
    from ninaivu.server import config as config_mod

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(config_mod, "_PRIVATE_ON_WINDOWS", set())
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Users" / "amma"))
    monkeypatch.setenv("USERNAME", "amma")
    monkeypatch.setenv("USERDOMAIN", "HOMEPC")
    return SimpleNamespace(calls=calls, config=config_mod)


def test_a_state_folder_on_another_drive_is_made_private_once(windows, tmp_path):
    folder = tmp_path / "D" / "Ninaivu"
    folder.mkdir(parents=True)
    windows.config.private_on_windows(folder)
    windows.config.private_on_windows(folder)
    assert len(windows.calls) == 1
    command, kwargs = windows.calls[0]
    assert command == ["icacls", str(folder), "/inheritance:r", "/grant:r",
                       "HOMEPC\\amma:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"]
    assert kwargs["timeout"] and not kwargs.get("shell")


def test_a_folder_under_the_profile_is_left_as_it_is(windows, tmp_path):
    folder = tmp_path / "Users" / "amma" / "AppData" / "Ninaivu"
    folder.mkdir(parents=True)
    windows.config.private_on_windows(folder)
    assert windows.calls == []


def test_icacls_failing_is_only_logged(windows, tmp_path, monkeypatch, caplog):
    def broken(command, **kwargs):
        raise OSError("icacls is not there")

    monkeypatch.setattr(subprocess, "run", broken)
    folder = tmp_path / "E" / "Ninaivu"
    folder.mkdir(parents=True)
    windows.config.private_on_windows(folder)              # no exception
    assert "could not make" in caplog.text
