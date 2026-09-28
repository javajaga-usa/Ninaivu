"""Authenticated encryption for Ninaivu's backup files.

V1 streams AES-256-GCM with a PBKDF2 key derived per file from a passphrase.
V2, which encrypted Drive backup uses, streams AES-256-GCM with one stored key
named in the header (see ninaivu.cloud.keyring). Both publish only completed,
authenticated output.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    CRYPTO_OK = True
except Exception:  # pragma: no cover
    CRYPTO_OK = False


MAGIC = b"NINAIVU_ENC_V1\x00"
SALT_LEN = 16
NONCE_LEN = 12
PBKDF2_ROUNDS = 100_000
TAG_LEN = 16
FILE_CHUNK_BYTES = 1024 * 1024
# GCM's per-message plaintext limit (2**39 - 256 bits).
MAX_FILE_BYTES = (1 << 36) - 32


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive 256-bit AES key from passphrase and salt."""
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, PBKDF2_ROUNDS, dklen=32)


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    """Encrypt raw bytes with passphrase using AES-256-GCM."""
    if not CRYPTO_OK:
        raise RuntimeError("cryptography package is required for zero-knowledge cloud encryption")
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, data, MAGIC)
    return MAGIC + salt + nonce + ciphertext


def decrypt_bytes(encrypted_data: bytes, passphrase: str) -> bytes:
    """Decrypt raw bytes with passphrase."""
    if not CRYPTO_OK:
        raise RuntimeError("cryptography package is required for zero-knowledge cloud encryption")
    if not encrypted_data.startswith(MAGIC):
        raise ValueError("Invalid encrypted payload (bad magic)")
    offset = len(MAGIC)
    salt = encrypted_data[offset:offset + SALT_LEN]
    offset += SALT_LEN
    nonce = encrypted_data[offset:offset + NONCE_LEN]
    offset += NONCE_LEN
    ciphertext = encrypted_data[offset:]
    key = derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, MAGIC)


@contextmanager
def _atomic_output(dest_path: Path | str) -> Iterator[BinaryIO]:
    """Publish only completed output; failures leave an existing file intact."""
    dest = Path(dest_path).absolute()
    dest.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(
                dir=dest.parent, prefix=".ninaivu-crypto-", suffix=".tmp",
                delete=False) as output:
            pending = Path(output.name)
            yield output
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, dest)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def encrypt_file_to_file(src_path: Path | str, dest_path: Path | str, passphrase: str) -> int:
    """Stream a V1 file with bounded memory. Return the encrypted byte size."""
    if not CRYPTO_OK:
        raise RuntimeError("cryptography package is required for zero-knowledge cloud encryption")
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    encryptor = Cipher(algorithms.AES(derive_key(passphrase, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    total = 0
    # Close the input before publishing, including when both paths are equal.
    with _atomic_output(dest_path) as output:
        with open(src_path, "rb") as source:
            output.write(MAGIC + salt + nonce)
            while chunk := source.read(FILE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise ValueError("File exceeds the AES-GCM per-message size limit")
                output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
    return len(MAGIC) + SALT_LEN + NONCE_LEN + total + TAG_LEN


def decrypt_file_to_file(src_path: Path | str, dest_path: Path | str, passphrase: str) -> int:
    """Stream V1 decryption; publish plaintext only after tag verification."""
    if not CRYPTO_OK:
        raise RuntimeError("cryptography package is required for zero-knowledge cloud encryption")
    total = 0
    with _atomic_output(dest_path) as output:
        with open(src_path, "rb") as source:
            header_size = len(MAGIC) + SALT_LEN + NONCE_LEN
            header = source.read(header_size)
            if len(header) != header_size or not header.startswith(MAGIC):
                raise ValueError("Invalid encrypted payload (bad or truncated header)")
            salt = header[len(MAGIC):len(MAGIC) + SALT_LEN]
            nonce = header[-NONCE_LEN:]
            decryptor = Cipher(algorithms.AES(derive_key(passphrase, salt)), modes.GCM(nonce)).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            pending = b""
            while chunk := source.read(FILE_CHUNK_BYTES):
                pending += chunk
                if len(pending) <= TAG_LEN:
                    continue
                ciphertext, pending = pending[:-TAG_LEN], pending[-TAG_LEN:]
                total += len(ciphertext)
                if total > MAX_FILE_BYTES:
                    raise ValueError("File exceeds the AES-GCM per-message size limit")
                output.write(decryptor.update(ciphertext))
            if len(pending) != TAG_LEN:
                raise ValueError("Invalid encrypted payload (truncated tag)")
            output.write(decryptor.finalize_with_tag(pending))
    return total


# ---------------------------------------------------------------------------
# V2: one stored key, for encrypted Drive backups
# ---------------------------------------------------------------------------
#
# V1 derives a key from the passphrase for every file — 100,000 PBKDF2 rounds
# each, which for a million photographs is days of CPU before a byte is sent.
# V2 uses a key derived once (see ninaivu.cloud.keyring) and names it in the
# header, so a recovery tool can tell at once whether it holds the right key:
#
#     MAGIC_V2 | key id (8) | nonce (12) | ciphertext | tag (16)
#
# The magic and key id are authenticated as associated data.

MAGIC_V2 = b"NINAIVU_ENC_V2\x00"
KEY_ID_LEN = 8
V2_OVERHEAD = len(MAGIC_V2) + KEY_ID_LEN + NONCE_LEN + TAG_LEN
V2_FILE_CHUNK_BYTES = 4 * 1024 * 1024


def encrypt_file_v2(src_path: Path | str, dest_path: Path | str, key: bytes, key_id: bytes) -> int:
    """Stream a V2 file with bounded memory. Return the encrypted byte size."""
    if not CRYPTO_OK:
        raise RuntimeError("cryptography package is required for encrypted cloud backup")
    if len(key) != 32 or len(key_id) != KEY_ID_LEN:
        raise ValueError("A V2 key is 32 bytes with an 8-byte id")
    nonce = secrets.token_bytes(NONCE_LEN)
    header = MAGIC_V2 + key_id + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC_V2 + key_id)
    total = 0
    with _atomic_output(dest_path) as output:
        with open(src_path, "rb") as source:
            output.write(header)
            while chunk := source.read(V2_FILE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise ValueError("File exceeds the AES-GCM per-message size limit")
                output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
    return total + V2_OVERHEAD


def read_key_id(src_path: Path | str) -> bytes | None:
    """The key id a V2 file was encrypted with, or None if it is not V2."""
    with open(src_path, "rb") as source:
        header = source.read(len(MAGIC_V2) + KEY_ID_LEN)
    if len(header) != len(MAGIC_V2) + KEY_ID_LEN or not header.startswith(MAGIC_V2):
        return None
    return header[len(MAGIC_V2):]


def decrypt_file_v2(src_path: Path | str, dest_path: Path | str, key: bytes) -> int:
    """Stream V2 decryption; publish plaintext only after the tag verifies."""
    if not CRYPTO_OK:
        raise RuntimeError("cryptography package is required for encrypted cloud backup")
    total = 0
    with _atomic_output(dest_path) as output:
        with open(src_path, "rb") as source:
            header_size = len(MAGIC_V2) + KEY_ID_LEN + NONCE_LEN
            header = source.read(header_size)
            if len(header) != header_size or not header.startswith(MAGIC_V2):
                raise ValueError("Not a Ninaivu encrypted backup file (bad or truncated header)")
            key_id = header[len(MAGIC_V2):len(MAGIC_V2) + KEY_ID_LEN]
            nonce = header[-NONCE_LEN:]
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()
            decryptor.authenticate_additional_data(MAGIC_V2 + key_id)
            pending = b""
            while chunk := source.read(V2_FILE_CHUNK_BYTES):
                pending += chunk
                if len(pending) <= TAG_LEN:
                    continue
                ciphertext, pending = pending[:-TAG_LEN], pending[-TAG_LEN:]
                total += len(ciphertext)
                output.write(decryptor.update(ciphertext))
            if len(pending) != TAG_LEN:
                raise ValueError("Invalid encrypted payload (truncated tag)")
            try:
                output.write(decryptor.finalize_with_tag(pending))
            except Exception as error:                  # cryptography's InvalidTag
                raise ValueError("The file was altered, or this is not its key") from error
    return total
