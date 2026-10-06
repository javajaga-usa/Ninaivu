"""The key encrypted Drive backups are made with.

Created once, from a passphrase the administrator chooses, and kept on this
machine so uploads can run unattended — a key that has to be typed in after
every restart is a backup that silently stops. That protects the photographs
from anyone who can read the Drive account; it does not protect them from
someone who has this machine, who has the photographs anyway.

Two ways back, for the day this machine is gone:

* the **recovery file**, downloaded from the console, which holds the key
  itself — keep it somewhere that is not this machine and not that Drive;
* the **passphrase**, with ``ninaivu-encryption.json`` — the salt and
  settings the key was derived with, not the key — which is left in the
  Drive folder and beside the off-site copy's manifest. A key made later,
  or brought in from a recovery file, gets its own
  ``ninaivu-encryption-<key id>.json`` where one for another key is there.

A recovery file can also be brought back in (:func:`import_recovery`): on a
rebuilt machine that is how the off-site copy and the Drive backups made
before carry on with the key they were made with.

Lose the recovery file *and* forget the passphrase, and the backups cannot be
read by anyone, Ninaivu included. The console says so before a key is made.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from ..utils import kdf

KEY_FILE = "cloud-encryption.json"
MIN_PASSPHRASE = 12
#: scrypt settings for a new key: about 128 MB and under a second, once per
#: key (current guidance is at least N=2^17). A key made with the older
#: N=2^15 keeps it: the settings are stored with every key and its params.
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def derive(passphrase: str, salt: bytes, params: dict[str, int] | None = None) -> bytes:
    settings = {**SCRYPT, **(params or {})}
    return kdf.scrypt(passphrase.encode("utf-8"), salt=salt, n=settings["n"],
                      r=settings["r"], p=settings["p"], dklen=32,
                      maxmem=256 * 1024 * 1024)


def key_id(key: bytes) -> bytes:
    """Eight bytes that name a key without revealing anything about it."""
    return hmac.new(key, b"ninaivu backup key id", hashlib.sha256).digest()[:8]


def path(state_dir: Path | str) -> Path:
    return Path(state_dir) / KEY_FILE


def create(state_dir: Path | str, passphrase: Any, confirm: Any) -> dict[str, Any]:
    """Make the key. ValueError if the passphrase is unusable or a key exists."""
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    if passphrase != confirm:
        raise ValueError("The two passphrases do not match.")
    target = path(state_dir)
    if target.exists() or target.is_symlink():
        if load(state_dir) is not None:
            raise ValueError("An encryption key already exists. Backups made with it would "
                             "become unreadable if it were replaced.")
        _set_aside(target, "damaged")
    salt = secrets.token_bytes(16)
    key = derive(passphrase, salt)
    record = {"version": 1, "kdf": "scrypt", **SCRYPT, "salt": _b64(salt), "key": _b64(key),
              "key_id": key_id(key).hex(), "created_at": time.time(), "params_remote_id": ""}
    _write(target, record)
    return record


def _write(target: Path, record: dict[str, Any]) -> None:
    """Owner-only, synced, and published by rename: there is no moment the key
    exists at the default permissions or half-written, and a power cut just
    after cannot leave an empty key file behind. The temporary name is a fresh
    one each time, made exclusively, so a stale or planted one is never reused."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-",
                                             suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    if os.name != "nt":
        try:
            folder = os.open(target.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(folder)
        except OSError:
            pass
        finally:
            os.close(folder)


def _set_aside(target: Path, why: str) -> Path:
    """Move a key file out of the way, kept, under a name that says why."""
    kept = target.with_name(f"{target.stem}.{why}-{time.strftime('%Y%m%d-%H%M%S')}"
                            f"-{secrets.token_hex(3)}{target.suffix}")
    os.replace(target, kept)
    return kept


def import_recovery(state_dir: Path | str, document: Any, *,
                    replace: bool = False) -> dict[str, Any]:
    """Make the key in a recovery file this machine's key.

    For a rebuilt machine: the off-site copy and the Drive backups were made
    with the old key, and carry on with it once it is back. The same key
    already here is no change. A different key already here is replaced only
    with *replace*, and is kept beside, not deleted — whatever was sent with
    it needs it. ValueError for a file that is not a recovery file.
    """
    if not isinstance(document, dict):
        raise ValueError("That is not a Ninaivu recovery file.")
    try:
        key = base64.b64decode(str(document["key"]), validate=True)
        params = {name: int(document[name]) for name in ("n", "r", "p")}
        salt = str(document["salt"])
        base64.b64decode(salt, validate=True)
    except (KeyError, TypeError, ValueError):
        raise ValueError("That is not a Ninaivu recovery file (it has no key, "
                         "or the key's settings are missing).") from None
    if len(key) != 32 or key_id(key).hex() != str(document.get("key_id") or ""):
        raise ValueError("That recovery file is damaged: its key does not match its key id.")
    target = path(state_dir)
    existing = load(state_dir)
    if existing is not None and existing["key_id"] == key_id(key).hex():
        return existing
    if existing is not None and not replace:
        raise ValueError(f"This computer already has a different backup key "
                         f"({existing['key_id']}). Choose to replace it to use the "
                         f"imported key ({key_id(key).hex()}) from now on; the current "
                         f"one is kept beside it, not deleted.")
    if target.exists() or target.is_symlink():
        _set_aside(target, "replaced" if existing is not None else "damaged")
    record = {"version": 1, "kdf": str(document.get("kdf") or "scrypt"), **params,
              "salt": salt, "key": _b64(key), "key_id": key_id(key).hex(),
              "created_at": float(document.get("created_at") or time.time()),
              "imported_at": time.time(), "params_remote_id": ""}
    _write(target, record)
    return record


def load(state_dir: Path | str) -> dict[str, Any] | None:
    try:
        record = json.loads(path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        key = base64.b64decode(record["key"])
        if len(key) != 32 or key_id(key).hex() != record["key_id"]:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return record


def key_material(record: dict[str, Any]) -> tuple[bytes, bytes]:
    key = base64.b64decode(record["key"])
    return key, key_id(key)


def note_params_uploaded(state_dir: Path | str, remote_id: str) -> None:
    record = load(state_dir)
    if record is None:
        return
    record["params_remote_id"] = remote_id
    _write(path(state_dir), record)


def public_params(record: dict[str, Any]) -> dict[str, Any]:
    """What goes in the Drive folder: enough to re-derive with the passphrase, no key."""
    return {"format": "ninaivu-encrypted-backup", "version": 1, "kdf": record["kdf"],
            "n": record["n"], "r": record["r"], "p": record["p"],
            "salt": record["salt"], "key_id": record["key_id"],
            "note": "Files ending .ninaivu in this folder are encrypted. Decrypt them with "
                    "tools/cloud_decrypt.py (or an off-site copy with ninaivu offsite-restore) "
                    "and the recovery file, or this file and the passphrase."}


def params_name(record: dict[str, Any]) -> str:
    """The settings file named for one key, for a folder that holds another's."""
    return f"ninaivu-encryption-{record['key_id']}.json"


def recovery_document(record: dict[str, Any]) -> dict[str, Any]:
    """The recovery file: the key itself, and how to use it."""
    return {**public_params(record), "key": record["key"], "created_at": record["created_at"],
            "note": "KEEP THIS FILE SAFE AND PRIVATE. It decrypts every Ninaivu backup made with "
                    "this key: python tools/cloud_decrypt.py --recovery <this file> <downloaded "
                    "folder or file> <output folder>. Store it somewhere other than this computer "
                    "and other than the Google Drive account the backups are in."}


def key_from(recovery: dict[str, Any] | None = None, passphrase: str | None = None,
             params: dict[str, Any] | None = None) -> bytes:
    """The key, from a recovery file, or from the passphrase and the Drive params."""
    if recovery is not None:
        key = base64.b64decode(recovery["key"])
        expected = recovery.get("key_id")
    elif passphrase is not None and params is not None:
        key = derive(passphrase, base64.b64decode(params["salt"]),
                     {name: int(params[name]) for name in ("n", "r", "p")})
        expected = params.get("key_id")
    else:
        raise ValueError("Give a recovery file, or the passphrase with ninaivu-encryption.json.")
    if expected and key_id(key).hex() != expected:
        raise ValueError("That is not the key these backups were made with "
                         "(wrong passphrase, or a different recovery file).")
    return key
