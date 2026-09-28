"""scrypt on every Python Ninaivu runs on.

``hashlib.scrypt`` exists only when Python was built against OpenSSL 1.1 or
later. The python3 in Apple's command line tools — the one the macOS launcher
offers to install — is built against LibreSSL and has no ``hashlib.scrypt`` at
all, so creating the first profile there was a 500. The cryptography package
carries its own OpenSSL and covers that Python. Both are the same RFC 7914
function, so a hash made by either verifies with the other.
"""
from __future__ import annotations

import hashlib


def scrypt(secret: bytes, *, salt: bytes, n: int, r: int, p: int, dklen: int,
           maxmem: int = 0) -> bytes:
    """``hashlib.scrypt`` where Python has it, cryptography's where it does not.

    *maxmem* only reaches hashlib; cryptography sizes its own allowance from
    *n* and *r*.
    """
    native = getattr(hashlib, "scrypt", None)
    if native is not None:
        return native(secret, salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=maxmem)
    try:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise RuntimeError(
            "This Python has no scrypt (it is built against LibreSSL) and the "
            "cryptography package is not installed; install it with "
            "'python3 -m pip install cryptography'.") from None
    return Scrypt(salt=salt, length=dklen, n=n, r=r, p=p).derive(secret)
