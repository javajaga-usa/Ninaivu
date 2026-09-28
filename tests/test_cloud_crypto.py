"""V1 compatibility, bounded reads, authentication, and safe publication."""

import builtins
import hashlib
import tracemalloc

import pytest

pytest.importorskip("cryptography")
from cryptography.exceptions import InvalidTag
from ninaivu.cloud import crypto


PASSWORD = "local test passphrase"


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 1024 * 1024 - 1,
                                  1024 * 1024, 1024 * 1024 + 1, 2 * 1024 * 1024 + 7])
def test_streaming_matches_existing_v1_bytes(tmp_path, monkeypatch, size):
    # Deterministic randomness is confined to this compatibility test.
    monkeypatch.setattr(crypto.secrets, "token_bytes", lambda n: bytes(range(n)))
    original = (bytes(range(256)) * (size // 256 + 1))[:size]
    source, encrypted, restored = [tmp_path / name for name in ("source", "encrypted", "restored")]
    source.write_bytes(original)
    legacy = crypto.encrypt_bytes(original, PASSWORD)
    assert crypto.encrypt_file_to_file(source, encrypted, PASSWORD) == len(legacy)
    assert encrypted.read_bytes() == legacy
    assert crypto.decrypt_bytes(encrypted.read_bytes(), PASSWORD) == original
    assert crypto.decrypt_file_to_file(encrypted, restored, PASSWORD) == size
    assert restored.read_bytes() == original


@pytest.mark.parametrize("damage", ["password", "magic", "salt", "nonce", "ciphertext", "tag", "truncate", "append"])
def test_authentication_failure_preserves_destination(tmp_path, damage):
    content = bytearray(crypto.encrypt_bytes(b"private photograph" * 100, PASSWORD))
    offsets = {"magic": 0, "salt": len(crypto.MAGIC),
               "nonce": len(crypto.MAGIC) + crypto.SALT_LEN,
               "ciphertext": len(crypto.MAGIC) + crypto.SALT_LEN + crypto.NONCE_LEN,
               "tag": len(content) - 1}
    if damage in offsets:
        content[offsets[damage]] ^= 1
    elif damage == "truncate":
        del content[-1:]
    elif damage == "append":
        content.extend(b"extra")
    source, dest = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(content)
    dest.write_bytes(b"existing destination")
    with pytest.raises((InvalidTag, ValueError)):
        crypto.decrypt_file_to_file(source, dest, "wrong" if damage == "password" else PASSWORD)
    assert dest.read_bytes() == b"existing destination"
    assert not list(tmp_path.glob(".ninaivu-crypto-*"))


@pytest.mark.parametrize("size", [0, 1, len(crypto.MAGIC),
                                  len(crypto.MAGIC) + crypto.SALT_LEN + crypto.NONCE_LEN,
                                  len(crypto.MAGIC) + crypto.SALT_LEN + crypto.NONCE_LEN + 15])
def test_truncated_input_does_not_publish_plaintext(tmp_path, size):
    source, dest = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(crypto.encrypt_bytes(b"", PASSWORD)[:size])
    with pytest.raises(ValueError):
        crypto.decrypt_file_to_file(source, dest, PASSWORD)
    assert not dest.exists()
    assert not list(tmp_path.glob(".ninaivu-crypto-*"))


def test_file_helpers_only_make_bounded_reads(tmp_path, monkeypatch):
    source, encrypted, restored = [tmp_path / name for name in ("source", "encrypted", "restored")]
    original = b"photo" * 1000
    source.write_bytes(original)
    monkeypatch.setattr(crypto, "FILE_CHUNK_BYTES", 64)
    read_sizes = []

    class Reader:
        def __init__(self, path, mode):
            self.file = builtins.open(path, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def read(self, size=-1):
            assert 0 < size <= 64, "unbounded input read"
            read_sizes.append(size)
            return self.file.read(size)

    monkeypatch.setattr(crypto, "open", Reader, raising=False)
    crypto.encrypt_file_to_file(source, encrypted, PASSWORD)
    crypto.decrypt_file_to_file(encrypted, restored, PASSWORD)
    assert restored.read_bytes() == original
    assert len(read_sizes) > 100


@pytest.mark.parametrize("operation", ["encrypt_file_to_file", "decrypt_file_to_file"])
@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_output_failure_preserves_destination(tmp_path, monkeypatch, operation, failure):
    source, dest = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(crypto.encrypt_bytes(b"photo", PASSWORD) if operation.startswith("decrypt") else b"photo")
    dest.write_bytes(b"original")

    def broken(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(crypto.os, failure, broken)
    with pytest.raises(OSError):
        getattr(crypto, operation)(source, dest, PASSWORD)
    assert dest.read_bytes() == b"original"
    assert not list(tmp_path.glob(".ninaivu-crypto-*"))


def test_same_path_round_trip(tmp_path):
    path = tmp_path / "photo"
    original = b"my photograph" * 100
    path.write_bytes(original)
    crypto.encrypt_file_to_file(path, path, PASSWORD)
    crypto.decrypt_file_to_file(path, path, PASSWORD)
    assert path.read_bytes() == original


@pytest.mark.parametrize("operation", ["encrypt_file_to_file", "decrypt_file_to_file"])
def test_gcm_size_limit_preserves_destination(tmp_path, monkeypatch, operation):
    source, dest = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(crypto.encrypt_bytes(b"12345", PASSWORD) if operation.startswith("decrypt") else b"12345")
    dest.write_bytes(b"original")
    monkeypatch.setattr(crypto, "MAX_FILE_BYTES", 4)
    with pytest.raises(ValueError, match="size limit"):
        getattr(crypto, operation)(source, dest, PASSWORD)
    assert dest.read_bytes() == b"original"
    assert not list(tmp_path.glob(".ninaivu-crypto-*"))


def test_large_file_has_bounded_python_memory(tmp_path):
    source, encrypted, restored = [tmp_path / name for name in ("source", "encrypted", "restored")]
    chunk = b"x" * (1024 * 1024)
    with source.open("wb") as output:
        for _ in range(32):
            output.write(chunk)
    tracemalloc.start()
    try:
        crypto.encrypt_file_to_file(source, encrypted, PASSWORD)
        crypto.decrypt_file_to_file(encrypted, restored, PASSWORD)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 12 * 1024 * 1024, f"Python allocations peaked at {peak} bytes"
    for path in (source, restored):
        # Chunked rather than hashlib.file_digest, which arrived in 3.11 and
        # would make this file the only thing in the suite that needs it.
        summed = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                summed.update(block)
        digest = summed.hexdigest()
        if path == source:
            expected = digest
        else:
            assert digest == expected


@pytest.mark.parametrize("operation", ["encrypt_file_to_file", "decrypt_file_to_file"])
def test_missing_crypto_dependency_does_not_touch_output(tmp_path, monkeypatch, operation):
    dest = tmp_path / "destination"
    dest.write_bytes(b"original")
    monkeypatch.setattr(crypto, "CRYPTO_OK", False)
    with pytest.raises(RuntimeError, match="cryptography package"):
        getattr(crypto, operation)(tmp_path / "source", dest, PASSWORD)
    assert dest.read_bytes() == b"original"
    assert not list(tmp_path.glob(".ninaivu-crypto-*"))


@pytest.mark.parametrize("operation", ["encrypt_file_to_file", "decrypt_file_to_file"])
def test_interrupted_input_removes_partial_output(tmp_path, monkeypatch, operation):
    source, dest = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(crypto.encrypt_bytes(b"photo" * 1000, PASSWORD)
                       if operation.startswith("decrypt") else b"photo" * 1000)
    dest.write_bytes(b"original")
    monkeypatch.setattr(crypto, "FILE_CHUNK_BYTES", 64)

    class BrokenReader:
        def __init__(self, path, mode):
            self.file = builtins.open(path, mode)
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def read(self, size):
            self.calls += 1
            if self.calls == 3:
                raise OSError("source disconnected")
            return self.file.read(size)

    monkeypatch.setattr(crypto, "open", BrokenReader, raising=False)
    with pytest.raises(OSError, match="source disconnected"):
        getattr(crypto, operation)(source, dest, PASSWORD)
    assert dest.read_bytes() == b"original"
    assert not list(tmp_path.glob(".ninaivu-crypto-*"))
