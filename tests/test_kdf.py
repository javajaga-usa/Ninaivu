"""scrypt without hashlib.scrypt: Apple's command line tools Python has none."""

import base64
import hashlib
import sys

import pytest

from ninaivu.cloud import keyring
from ninaivu.server import auth
from ninaivu.utils import kdf

# RFC 7914, section 12, third vector.
RFC_7914 = dict(secret=b"password", salt=b"NaCl", n=1024, r=8, p=16, dklen=64)
RFC_7914_KEY = bytes.fromhex(
    "fdbabe1c9d3472007856e7190d01e9fe7c6ad7cbc8237830e77376634b373162"
    "2eaf30d92e22a3886ff109279d9830dac727afb94a83ee6d8360cbdfa2cc0640")


@pytest.fixture
def libressl(monkeypatch):
    """A Python like /usr/bin/python3 on a Mac: hashlib with no scrypt."""
    pytest.importorskip("cryptography")
    monkeypatch.delattr(hashlib, "scrypt", raising=False)


def test_whichever_scrypt_this_python_has_is_the_standard_one():
    assert kdf.scrypt(**RFC_7914) == RFC_7914_KEY


def test_without_hashlib_scrypt_cryptography_gives_the_same_answer(libressl):
    assert kdf.scrypt(**RFC_7914) == RFC_7914_KEY


def test_the_first_profile_can_be_made_without_hashlib_scrypt(libressl):
    stored = auth.hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert auth.verify_password("correct horse battery", stored)
    assert not auth.verify_password("wrong horse battery", stored)


def test_a_backup_key_can_be_made_without_hashlib_scrypt(libressl, tmp_path):
    record = keyring.create(tmp_path, "a long enough passphrase", "a long enough passphrase")
    salt = base64.b64decode(record["salt"])
    assert keyring.derive("a long enough passphrase", salt) == base64.b64decode(record["key"])


def test_with_neither_the_error_says_what_to_install(monkeypatch):
    monkeypatch.delattr(hashlib, "scrypt", raising=False)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.kdf.scrypt", None)
    with pytest.raises(RuntimeError, match="pip install cryptography"):
        kdf.scrypt(**RFC_7914)
