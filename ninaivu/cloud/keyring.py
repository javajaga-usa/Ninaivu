"""The key encrypted Drive backups are made with.

Created once, from a passphrase the administrator chooses, and kept on this
machine so uploads can run unattended — a key that has to be typed in after
every restart is a backup that silently stops. That protects the photographs
from anyone who can read the Drive account; it does not protect them from
someone who has this machine, who has the photographs anyway.

Two ways back, for the day this machine is gone:

* the **recovery file**, downloaded from the console, which holds the key
  itself — keep it somewhere that is not this machine and not that Drive;
* the **passphrase**, with ``ninaivu-encryption.json`` from the Drive folder,
  which holds the salt and settings the key was derived with but not the key.

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
import time
from pathlib import Path
from typing import Any

from ..utils import kdf

KEY_FILE = "cloud-encryption.json"
MIN_PASSPHRASE = 12
#: scrypt settings: about 32 MB and a fraction of a second, once per key.
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


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
    if target.exists():
        raise ValueError("An encryption key already exists. Backups made with it would "
                         "become unreadable if it were replaced.")
    salt = secrets.token_bytes(16)
    key = derive(passphrase, salt)
    record = {"version": 1, "kdf": "scrypt", **SCRYPT, "salt": _b64(salt), "key": _b64(key),
              "key_id": key_id(key).hex(), "created_at": time.time(), "params_remote_id": ""}
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written owner-only and published by rename, so there is no moment the
    # key exists at the default permissions or half-written.
    temporary = target.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(temporary, target)
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
    target = path(state_dir)
    temporary = target.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(temporary, target)


def public_params(record: dict[str, Any]) -> dict[str, Any]:
    """What goes in the Drive folder: enough to re-derive with the passphrase, no key."""
    return {"format": "ninaivu-encrypted-backup", "version": 1, "kdf": record["kdf"],
            "n": record["n"], "r": record["r"], "p": record["p"],
            "salt": record["salt"], "key_id": record["key_id"],
            "note": "Files ending .ninaivu in this folder are encrypted. Decrypt them with "
                    "tools/cloud_decrypt.py and the recovery file, or this file and the passphrase."}


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
