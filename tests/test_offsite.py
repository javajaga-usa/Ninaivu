"""An encrypted copy away from home: to a folder or any S3-compatible service."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from ninaivu.cloud import keyring, s3
from ninaivu.cloud.offsite import MANIFEST, Offsite
from ninaivu.storage import db

AWS = dict(access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
           region="us-east-1", when=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc))


def test_requests_are_signed_as_amazons_own_examples_are():
    """The two worked examples in AWS's Signature Version 4 documentation."""
    got = s3.sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
                  s3.EMPTY_SHA256, **AWS)
    assert got["Authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    body = b"Welcome to Amazon S3."
    got = s3.sign("PUT", "https://examplebucket.s3.amazonaws.com/test%24file.text",
                  {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
                  hashlib.sha256(body).hexdigest(), **AWS)
    assert got["Authorization"].endswith(
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd")


class FakeS3(BaseHTTPRequestHandler):
    """Just enough of S3: objects, multipart uploads, and a check that every
    request was signed and its body matches the SHA-256 it was signed with."""

    store: dict[str, bytes] = {}
    uploads: dict[str, dict[int, bytes]] = {}

    def log_message(self, *args):
        pass

    def _checked(self) -> bytes:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        assert self.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=key/")
        assert self.headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
        return body

    def _key(self) -> tuple[str, dict]:
        parts = urlsplit(self.path)
        return parts.path, parse_qs(parts.query, keep_blank_values=True)

    def _answer(self, status=200, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_PUT(self):
        body = self._checked()
        key, query = self._key()
        if "partNumber" in query:
            self.uploads[query["uploadId"][0]][int(query["partNumber"][0])] = body
            return self._answer(headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})
        self.store[key] = body
        self._answer()

    def do_POST(self):
        body = self._checked()
        key, query = self._key()
        if "uploads" in query:
            upload = f"u{len(self.uploads) + 1}"
            self.uploads[upload] = {}
            return self._answer(body=f"<InitiateMultipartUploadResult><UploadId>{upload}</UploadId>"
                                     "</InitiateMultipartUploadResult>".encode())
        parts = self.uploads.pop(query["uploadId"][0])
        numbers = [int(n) for n in re.findall(rb"<PartNumber>(\d+)</PartNumber>", body)]
        self.store[key] = b"".join(parts[n] for n in numbers)
        self._answer(body=b"<CompleteMultipartUploadResult/>")

    def do_GET(self):
        self._checked()
        key, _ = self._key()
        if key not in self.store:
            return self._answer(404, b"<Error><Code>NoSuchKey</Code><Message>not there</Message></Error>")
        self._answer(body=self.store[key])

    def do_HEAD(self):
        key, _ = self._key()
        if key not in self.store:
            return self._answer(404)
        self._answer(body=self.store[key])         # its length, and no body, for a HEAD

    def do_DELETE(self):
        self._checked()
        self._answer(204)


@pytest.fixture()
def bucket():
    FakeS3.store, FakeS3.uploads = {}, {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_a_large_file_goes_up_in_parts(bucket, tmp_path, monkeypatch):
    monkeypatch.setattr(s3, "MULTIPART_ABOVE", 1000)
    monkeypatch.setattr(s3, "PART", 400)
    client = s3.S3(endpoint=bucket, bucket="photos", access_key="key", secret_key="secret")
    big = tmp_path / "big.bin"
    big.write_bytes(bytes(range(256)) * 10)
    client.put_file("a/big.bin", big)
    assert FakeS3.store["/photos/a/big.bin"] == big.read_bytes()
    back = tmp_path / "back.bin"
    client.get_file("a/big.bin", back)
    assert back.read_bytes() == big.read_bytes()
    with pytest.raises(s3.S3Error) as caught:
        client.get("missing")
    assert caught.value.status == 404


@pytest.fixture()
def lib(scanned, tmp_path):
    cfg, conn, _ = scanned
    keyring.create(cfg.state_dir, "correct horse battery", "correct horse battery")
    cfg.offsite_kind = "folder"
    cfg.offsite_folder = str(tmp_path / "friends-disk")
    Path(cfg.offsite_folder).mkdir()
    offsite = Offsite(cfg, lambda: db.connect(cfg.db_path))
    return {"cfg": cfg, "conn": conn, "offsite": offsite, "tmp": tmp_path}


def run(offsite, work=None):
    (work or offsite.start)()
    offsite.join(60)
    return offsite.status()


def library_files(cfg) -> dict[str, bytes]:
    root = Path(cfg.active_root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and not p.name.endswith(".txt")}


def test_everything_goes_encrypted_under_names_that_say_nothing(lib):
    status = run(lib["offsite"])
    assert status["kept"] == status["files"] > 0 and not status["error"]
    base = Path(lib["cfg"].offsite_folder) / "ninaivu-offsite"
    stored = [p for p in base.rglob("*.ninaivu") if p.name != MANIFEST]
    assert len(stored) == status["files"]
    names = " ".join(str(p) for p in base.rglob("*"))
    assert "shot" not in names and "secret" not in names, "no file name reaches the provider"
    originals = set(library_files(lib["cfg"]).values())
    assert not any(p.read_bytes() in originals for p in stored)
    assert all(b"JFIF" not in p.read_bytes()[:64] for p in stored)
    assert (base / "README.txt").is_file()
    again = run(lib["offsite"])
    assert again["sent"] == 0, "only what changed is sent again"


def test_it_all_comes_back_by_name_here_and_with_only_the_recovery_file(lib):
    run(lib["offsite"])
    out = lib["tmp"] / "restored"
    run(lib["offsite"], lambda: lib["offsite"].restore(str(out)))
    assert library_files(lib["cfg"]) == {p.relative_to(out).as_posix(): p.read_bytes()
                                         for p in out.rglob("*") if p.is_file()}
    # On a computer with nothing of Ninaivu's but the recovery file:
    from ninaivu.cli import offsite_restore as tool
    recovery = lib["tmp"] / "recovery.json"
    recovery.write_text(json.dumps(keyring.recovery_document(keyring.load(lib["cfg"].state_dir))))
    elsewhere = lib["tmp"] / "elsewhere"
    code = tool.main(["--recovery", str(recovery), str(Path(lib["cfg"].offsite_folder)), str(elsewhere)])
    assert code == 0
    assert library_files(lib["cfg"]) == {p.relative_to(elsewhere).as_posix(): p.read_bytes()
                                         for p in elsewhere.rglob("*") if p.is_file()}


def test_nothing_is_ever_deleted_from_it(lib):
    run(lib["offsite"])
    base = Path(lib["cfg"].offsite_folder) / "ninaivu-offsite"
    before = sorted(p.name for p in base.rglob("*.ninaivu"))
    victim = next(Path(lib["cfg"].active_root).rglob("shot1.jpg"))
    victim.unlink()
    lib["conn"].execute("UPDATE assets SET trashed=1 WHERE filename='shot1.jpg'")
    lib["conn"].commit()
    run(lib["offsite"])
    after = sorted(p.name for p in base.rglob("*.ninaivu"))
    assert set(before) <= set(after) and set(after) - set(before) <= {"manifest.prev.ninaivu"}


def test_without_a_key_or_inside_the_library_it_refuses(lib):
    lib["cfg"].offsite_folder = str(Path(lib["cfg"].active_root) / "misc")
    assert "inside the library" in lib["offsite"].problem()
    keyring.path(lib["cfg"].state_dir).unlink()
    assert "backup key" in lib["offsite"].problem()
    with pytest.raises(ValueError):
        lib["offsite"].start()


def test_the_same_goes_to_a_bucket(lib, bucket):
    cfg = lib["cfg"]
    cfg.offsite_kind, cfg.offsite_endpoint, cfg.offsite_bucket = "s3", bucket, "family"
    cfg.offsite_access_key, cfg.offsite_prefix = "key", "home"
    lib["offsite"].save_secret("secret")
    assert lib["offsite"].test()["ok"]
    status = run(lib["offsite"])
    assert status["kept"] == status["files"] and not status["error"], status
    assert f"/family/home/{MANIFEST}" in FakeS3.store
    assert not any("shot" in key for key in FakeS3.store)
    out = lib["tmp"] / "from-bucket"
    run(lib["offsite"], lambda: lib["offsite"].restore(str(out)))
    assert len([p for p in out.rglob("*") if p.is_file()]) == status["files"]


def test_the_console_keeps_the_secret_to_itself(scanned, tmp_path):
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        client = login(create_admin_app(services).test_client(), *ADMIN)
        data = client.post("/api/offsite", json={"kind": "s3", "endpoint": "https://s3.example.com",
                                                 "bucket": "b", "access_key": "id",
                                                 "secret_key": "very-secret"}).get_json()
        assert data["secret_saved"] is True and "very-secret" not in json.dumps(data)
        assert "very-secret" not in (cfg.config_path.read_text() if cfg.config_path.exists() else "")
        secret_file = Path(cfg.state_dir) / "offsite-secret.json"
        if os.name != "nt":                 # POSIX permission bits
            assert oct(secret_file.stat().st_mode & 0o777) == "0o600"
        assert client.post("/api/offsite", json={"endpoint": "s3.example.com"}).status_code == 400
        assert client.post("/api/offsite", json={"kind": "ftp"}).status_code == 400
        assert client.post("/api/offsite/start").status_code == 409, "no backup key yet"
        folder = Path(cfg.active_root) / "misc"
        assert client.post("/api/offsite/restore", json={"folder": str(folder)}).status_code == 400
    finally:
        services.stop(timeout=5.0)
