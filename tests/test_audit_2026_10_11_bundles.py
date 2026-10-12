"""External audit 2026-10-11, SEC-01: a state bundle written away from the
state folder must not carry the household's secrets.

A scheduled backup to another disk already left out the cloud key, the Google
sign-in and the Gemini key. It still carried the mail password and the
notification webhook in ``config.json``, and the HTTPS certificate authority's
private key, in a plain gzip tar. These tests plant unmistakable sentinel
values in every place a secret lives and read every byte of the bundle for
them, then restore it both onto the same computer and onto a new one.
"""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from ninaivu.cloud import index_copy
from ninaivu.storage import backup

SMTP = "SENTINEL-SMTP-PASSWORD-7f3a"
WEBHOOK = "https://hooks.example.invalid/SENTINEL-WEBHOOK-TOKEN-91c2"
CA_KEY = "-----BEGIN PRIVATE KEY-----\nSENTINEL-CA-KEY-55d0\n-----END PRIVATE KEY-----\n"
LEAF_KEY = "-----BEGIN PRIVATE KEY-----\nSENTINEL-LEAF-KEY-0b19\n-----END PRIVATE KEY-----\n"
LEGACY_KEY = "SENTINEL-LEGACY-CA-KEY-e4e4"
GOOGLE = "SENTINEL-GOOGLE-REFRESH-3c3c"
CLOUD = "SENTINEL-CLOUD-KEY-a1a1"
GEMINI = "SENTINEL-GEMINI-KEY-d2d2"
SENTINELS = (SMTP, "SENTINEL-WEBHOOK-TOKEN-91c2", "SENTINEL-CA-KEY-55d0", "SENTINEL-LEAF-KEY-0b19",
             LEGACY_KEY, GOOGLE, CLOUD, GEMINI)


def _plant(cfg) -> Path:
    cfg.notify_smtp_password = SMTP
    cfg.notify_webhook = WEBHOOK
    cfg.notify_webhook_format = "ntfy"
    cfg.notify_smtp_host = "mail.example.invalid"
    cfg.save()
    state = Path(cfg.state_dir)
    tls = state / "tls"
    tls.mkdir(exist_ok=True)
    (tls / "ninaivu-ca.key").write_text(CA_KEY)
    (tls / "ninaivu-ca.crt").write_text("CA CERTIFICATE")
    (tls / "ninaivu.key").write_text(LEAF_KEY)
    (tls / "ninaivu.crt").write_text("LEAF CERTIFICATE")
    (state / "ninaivu-ca.key").write_text(LEGACY_KEY)
    (state / "google.json").write_text(json.dumps({"refresh_token": GOOGLE}))
    (state / "cloud-encryption.json").write_text(json.dumps({"key": CLOUD}))
    (state / "gemini.json").write_text(json.dumps({"api_key": GEMINI}))
    return state


def _every_byte(bundle: Path) -> str:
    """The whole of every member, as text: a sentinel anywhere is found."""
    out = []
    with tarfile.open(bundle) as tar:
        for member in tar.getmembers():
            if member.isfile():
                out.append(member.name)
                out.append(tar.extractfile(member).read().decode("utf-8", "replace"))
    return "\n".join(out)


def _manifest(bundle: Path) -> dict:
    with tarfile.open(bundle) as tar:
        return json.load(tar.extractfile("state/backup_manifest.json"))


def _extract(bundle: Path, into: Path) -> Path:
    with tarfile.open(bundle) as tar:
        tar.extractall(into, filter="data")
    return into / "state"


# -- what goes out -----------------------------------------------------------------------

def test_a_scheduled_bundle_on_another_disk_holds_none_of_the_secrets(scanned, tmp_path):
    cfg, _, _ = scanned
    _plant(cfg)
    cfg.backup_dir = str(tmp_path / "usb-disk" / "ninaivu")
    made = backup.BackupKeeper(cfg).run()
    assert made is not None and made.parent == Path(cfg.backup_dir)

    text = _every_byte(made)
    leaked = [s for s in SENTINELS if s in text]
    assert leaked == [], f"secrets in a bundle away from home: {leaked}"
    assert "state/index.db" in text and "state/config.json" in text

    manifest = _manifest(made)
    assert manifest["left_out"] == sorted(backup.KEPT_HOME)
    assert set(backup.TLS_MATERIAL) <= set(manifest["left_out"])
    assert manifest[backup.BLANKED_HOME] == ["notify_smtp_password", "notify_webhook"]
    assert backup.verify_bundle(made)["ok"]


def test_the_settings_that_are_not_secrets_still_go(scanned, tmp_path):
    cfg, _, _ = scanned
    _plant(cfg)
    made = backup.snapshot(cfg.state_dir, tmp_path / "out", leave_out=backup.KEPT_HOME,
                           blank_secrets=True)
    with tarfile.open(made) as tar:
        settings = json.load(tar.extractfile("state/config.json"))
    assert settings["notify_smtp_host"] == "mail.example.invalid"
    assert settings["notify_webhook_format"] == "ntfy", "a body shape is not a secret"
    assert settings["notify_smtp_password"] == "" and settings["notify_webhook"] == ""


def test_a_bundle_in_the_state_folder_is_still_the_full_one(scanned):
    cfg, _, _ = scanned
    _plant(cfg)
    made = backup.BackupKeeper(cfg).run()
    assert made is not None
    text = _every_byte(made)
    assert all(s in text for s in SENTINELS), "the full-recovery bundle lost something"
    assert backup.BLANKED_HOME not in _manifest(made)


def test_settings_that_cannot_be_read_stop_the_bundle_rather_than_go_out_whole(scanned, tmp_path):
    cfg, _, _ = scanned
    state = _plant(cfg)
    (state / "config.json").write_text("{ not json " + SMTP)
    out = tmp_path / "out"
    assert backup.snapshot(state, out, leave_out=backup.KEPT_HOME, blank_secrets=True) is None
    assert not list(out.glob("*.tar.gz")) and not list(out.glob(".*.tmp"))


def test_the_command_writes_the_full_bundle_unless_asked_not_to(scanned, tmp_path, monkeypatch, capsys):
    from ninaivu.cli import backup_restore as tool

    cfg, _, _ = scanned
    _plant(cfg)
    monkeypatch.setattr(tool.Config, "load", classmethod(lambda cls: cfg))

    full = tool.create_backup(tmp_path / "full")
    assert full is not None and all(s in _every_byte(full) for s in SENTINELS)
    assert "--without-secrets" in capsys.readouterr().out

    lean = tool.create_backup(tmp_path / "lean", without_secrets=True)
    assert lean is not None
    assert [s for s in SENTINELS if s in _every_byte(lean)] == []


# -- what comes back ---------------------------------------------------------------------

def test_restored_onto_the_same_computer_it_keeps_its_own_secrets_and_certificates(scanned, tmp_path):
    from ninaivu.cli import backup_restore as tool

    cfg, _, _ = scanned
    state = _plant(cfg)
    made = backup.snapshot(state, tmp_path / "out", leave_out=backup.KEPT_HOME, blank_secrets=True)
    extracted = _extract(made, tmp_path / "x")
    candidate = tmp_path / "new"

    tool._prepare_replacement(state, extracted, candidate)
    settings = json.loads((candidate / "config.json").read_text())
    assert settings["notify_smtp_password"] == SMTP
    assert settings["notify_webhook"] == WEBHOOK
    assert settings["notify_webhook_format"] == "ntfy"
    assert (candidate / "tls" / "ninaivu-ca.key").read_text() == CA_KEY
    assert (candidate / "tls" / "ninaivu.crt").read_text() == "LEAF CERTIFICATE"
    assert (candidate / "ninaivu-ca.key").read_text() == LEGACY_KEY
    for name, value in (("google.json", GOOGLE), ("cloud-encryption.json", CLOUD),
                        ("gemini.json", GEMINI)):
        assert value in (candidate / name).read_text()


def test_restored_onto_a_new_computer_it_asks_again_rather_than_mixing(scanned, tmp_path):
    from ninaivu.cli import backup_restore as tool

    cfg, _, _ = scanned
    state = _plant(cfg)
    made = backup.snapshot(state, tmp_path / "out", leave_out=backup.KEPT_HOME, blank_secrets=True)
    extracted = _extract(made, tmp_path / "x")
    fresh, candidate = tmp_path / "fresh-state", tmp_path / "new"
    fresh.mkdir()

    tool._prepare_replacement(fresh, extracted, candidate)
    settings = json.loads((candidate / "config.json").read_text())
    assert settings["notify_smtp_password"] == "" and settings["notify_webhook"] == ""
    assert settings["notify_smtp_host"] == "mail.example.invalid"
    assert not (candidate / "tls").exists(), "a new computer makes its own authority"
    assert (candidate / "index.db").is_file()


@pytest.mark.parametrize("key, secret", [
    ("notify_smtp_password", True), ("notify_webhook", True), ("offsite_secret", True),
    ("notify_webhook_format", False), ("notify_smtp_host", False), ("offsite_access_key", False),
])
def test_which_settings_count_as_secrets(key, secret):
    assert index_copy.secret_setting(key) is secret
