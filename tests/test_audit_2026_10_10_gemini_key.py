"""Audit of 10 October 2026, the follow-up to L12: where the Gemini key lives.

The key typed into the console went into the models' ``settings.json`` — a
file outside the state folder, so the backup never carried it, the private
permissions the state folder gets on Windows never reached it, and on a
machine whose models sit on a shared or external disk it was readable by
whoever had the disk. The audit made every write of that file owner-only and
deferred the move. The key now has a file of its own in the state folder,
``gemini.json``, kept the way ``google.json`` and ``cloud-encryption.json``
are: owner-only from its first byte, in the backup, left out of a bundle
written to another disk, and never in the copy of the index sent to Drive. A
key found in the old file is moved in once, and read from there in the
meantime when the models folder cannot be written.
"""
from __future__ import annotations

import json
import logging
import os
import tarfile
from pathlib import Path

import pytest

from ninaivu.cloud import index_copy
from ninaivu.media import model_catalog
from ninaivu.storage import backup, db

gemini = pytest.importorskip("ninaivu_gemini.gemini")

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
KEY = "AIzaSyD-example-key-1234567890abcdWXYZ"


@pytest.fixture()
def cfg(cfg, monkeypatch):
    """The core's config with the Gemini extension switched on."""
    from ninaivu import extensions
    monkeypatch.setenv(extensions.DEV_MODULES_VAR, "ninaivu_gemini")
    extensions.discover(refresh=True)
    cfg.extensions = ["gemini"]
    return cfg


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A state folder and a models folder of this test's own, no key in the
    environment, and the extension's once-only log notes starting empty.
    Returns the state folder; the models folder is the one the suite's
    autouse fixture already points the catalogue at."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(gemini, "state_dir", lambda: state)
    monkeypatch.setattr(gemini, "_said", set())
    for name in gemini._ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    return state


def _old_file_with(key: str, **more) -> Path:
    target = model_catalog.settings_path()
    model_catalog.write_settings(target, {"gemini_api_key": key, **more})
    return target


def _read_only(path, data):
    """What writing the models' settings does on a read-only share."""
    raise PermissionError("read-only share")


# --- where a saved key lands -------------------------------------------------------

def test_a_saved_key_lands_in_the_state_folder_and_not_with_the_models(home):
    gemini.save_api_key(KEY)
    kept = home / gemini.KEY_FILE
    assert json.loads(kept.read_text())["api_key"] == KEY
    assert not model_catalog.settings_path().exists()
    assert not kept.with_name(kept.name + ".tmp").exists()
    assert gemini.key_status() == {"set": True, "source": "console", "variable": "",
                                   "hint": "…WXYZ"}


@POSIX
def test_the_key_file_is_owner_only_from_the_start(home):
    gemini.save_api_key(KEY)
    assert oct((home / gemini.KEY_FILE).stat().st_mode & 0o777) == "0o600"
    # And stays so when a key is saved over an existing file.
    gemini.save_api_key(KEY[:-1] + "Q")
    assert oct((home / gemini.KEY_FILE).stat().st_mode & 0o777) == "0o600"


def test_removing_the_key_leaves_a_file_that_says_there_is_none(home):
    gemini.save_api_key(KEY)
    gemini.save_api_key("")
    assert (home / gemini.KEY_FILE).is_file()
    assert KEY not in (home / gemini.KEY_FILE).read_text()
    assert gemini.key_status()["set"] is False


def test_the_console_saves_into_the_servers_own_state_folder(cfg, as_admin, monkeypatch):
    """Inside a request the state folder is the server's, not the default one."""
    for name in gemini._ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gemini, "check_api_key", lambda key: (True, ""))
    answer = as_admin.post("/api/admin/gemini", json={"key": KEY})
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json()["hint"] == "…WXYZ"
    assert json.loads((Path(cfg.state_dir) / gemini.KEY_FILE).read_text())["api_key"] == KEY
    assert not model_catalog.settings_path().exists()


# --- a key kept the old way is moved, once -----------------------------------------

def test_a_key_in_the_models_settings_is_moved_into_the_state_folder(home, caplog):
    old = _old_file_with(KEY, segmentation_model="/models/rmbg.onnx")
    caplog.set_level(logging.INFO, logger=gemini.log.name)
    assert gemini.stored_key() == KEY
    assert json.loads((home / gemini.KEY_FILE).read_text())["api_key"] == KEY
    # Taken out of the old file; the model paths beside it are kept.
    assert json.loads(old.read_text()) == {"segmentation_model": "/models/rmbg.onnx"}
    assert any("moved the Gemini key" in r.getMessage() for r in caplog.records)
    assert KEY not in caplog.text


def test_the_old_file_is_looked_in_only_while_there_is_no_key_file(home):
    _old_file_with(KEY)
    assert gemini.stored_key() == KEY
    # A key that turns up in the old file later — a settings file copied
    # over from another machine, say — is not taken: the move happened.
    old = _old_file_with("AIzaSomebodyElsesKeyFromAnotherMachine1234")
    assert gemini.stored_key() == KEY
    assert "gemini_api_key" in json.loads(old.read_text())


def test_a_removed_key_does_not_come_back_from_a_stale_copy(home, monkeypatch):
    """With the models folder read-only at the move, the old copy stays. The
    administrator then removes the key from the console; the next read must
    not find the stale copy and quietly put Gemini back on."""
    _old_file_with(KEY)
    monkeypatch.setattr(model_catalog, "write_settings", _read_only)
    assert gemini.stored_key() == KEY
    gemini.save_api_key("")
    assert gemini.stored_key() == ""
    assert gemini.key_status()["set"] is False


def test_a_read_only_models_folder_still_gives_up_the_key(home, monkeypatch, caplog):
    old = _old_file_with(KEY)
    calls = []

    def refuse(path, data):
        calls.append(path)
        raise PermissionError("read-only share")

    monkeypatch.setattr(model_catalog, "write_settings", refuse)
    caplog.set_level(logging.INFO, logger=gemini.log.name)
    assert gemini.stored_key() == KEY
    assert gemini.stored_key() == KEY
    # Read from the state folder from now on, with the old copy left where it was.
    assert json.loads((home / gemini.KEY_FILE).read_text())["api_key"] == KEY
    assert json.loads(old.read_text())["gemini_api_key"] == KEY
    # Said once, in words that say what to do, and without the key.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "by hand" in warnings[0].getMessage()
    assert KEY not in caplog.text
    assert len(calls) == 1


def test_the_old_copy_goes_the_first_time_the_folder_can_be_written(home, monkeypatch):
    old = _old_file_with(KEY)
    real = model_catalog.write_settings
    monkeypatch.setattr(model_catalog, "write_settings", _read_only)
    assert gemini.stored_key() == KEY
    assert "gemini_api_key" in json.loads(old.read_text())
    monkeypatch.setattr(model_catalog, "write_settings", real)
    gemini.save_api_key(KEY)
    assert "gemini_api_key" not in json.loads(old.read_text())


def test_nothing_to_move_means_nothing_written(home):
    """No old file, or one without a key: no key, and no key file made for
    it either — the next read is free to look again, and nothing is said."""
    assert gemini.stored_key() == ""
    assert not (home / gemini.KEY_FILE).exists()
    model_catalog.write_settings(model_catalog.settings_path(),
                                 {"segmentation_model": "/models/rmbg.onnx"})
    assert gemini.stored_key() == ""
    assert not (home / gemini.KEY_FILE).exists()


# --- kept the way the other credentials are ----------------------------------------

@pytest.fixture()
def state_with_index(home):
    """The state folder with a real index, which a bundle needs, and the key."""
    conn = db.init_db(home / "index.db")
    conn.commit()
    (home / "config.json").write_text('{"roots": ["/lib"]}', encoding="utf-8")
    gemini.save_api_key(KEY)
    return home


def _names(bundle: Path) -> set[str]:
    with tarfile.open(bundle) as tar:
        return set(tar.getnames())


def test_the_backup_carries_the_key_file(state_with_index, tmp_path):
    made = backup.snapshot(state_with_index, tmp_path / "out")
    assert made is not None
    assert f"state/{gemini.KEY_FILE}" in _names(made)
    assert backup.verify_bundle(made)["ok"]


def test_a_bundle_written_to_another_disk_leaves_the_key_at_home(state_with_index, tmp_path):
    """Like the cloud key and the Google sign-in: a bundle outside the state
    folder may sit on a disk that keeps no permissions, and a pasted key is
    pasted again more easily than it is revoked."""
    assert gemini.KEY_FILE in backup.KEPT_HOME
    assert gemini.KEY_FILE in backup.REPLACED_ONLY_IF_PRESENT
    made = backup.snapshot(state_with_index, tmp_path / "usb" / "out", leave_out=backup.KEPT_HOME)
    assert made is not None
    assert f"state/{gemini.KEY_FILE}" not in _names(made)


def test_a_restore_without_the_key_file_keeps_this_machines(home, tmp_path):
    from ninaivu.cli import backup_restore as tool

    gemini.save_api_key(KEY)
    extracted, candidate = tmp_path / "bundle", tmp_path / "new"
    extracted.mkdir()
    (extracted / "index.db").write_bytes(b"index from the bundle")
    (extracted / "backup_manifest.json").write_text(json.dumps(
        {"files": {}, "left_out": list(backup.KEPT_HOME)}))
    tool._prepare_replacement(home, extracted, candidate)
    assert json.loads((candidate / gemini.KEY_FILE).read_text())["api_key"] == KEY


def test_the_copy_of_the_index_sent_to_drive_never_carries_it(state_with_index, tmp_path):
    bundle = index_copy.make_bundle(state_with_index, tmp_path / "out")
    names = _names(bundle)
    assert "state/index.db" in names
    assert not any(gemini.KEY_FILE in name for name in names)
    with tarfile.open(bundle) as tar:
        raw = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
    assert KEY.encode() not in raw
