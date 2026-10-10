"""The little of Amazon S3's API an off-site backup needs, signed by hand.

Backblaze B2, Wasabi, Cloudflare R2, MinIO, a Synology or TrueNAS box and
AWS itself all speak the same object API, so one client reaches all of them:
put an object (in parts, when it is large), read one back, ask whether one
is there. That is small enough to write against the standard library — the
alternative is a 90 MB SDK for four calls.

Requests are signed with AWS Signature Version 4, path-style
(``https://endpoint/bucket/key``), which every one of those services accepts.
The body's SHA-256 is always signed, so a file damaged on the way is refused
by the service rather than stored.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import http.client
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .tempfiles import create_new

__all__ = ["S3", "S3Error", "sign"]

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
#: Objects larger than this go up in parts of PART bytes (S3's single PUT
#: stops at 5 GB, and a failed part costs less to send again).
MULTIPART_ABOVE = 64 * 1024 * 1024
PART = 16 * 1024 * 1024


class S3Error(OSError):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def _quote(text: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(text, safe=safe)


def sign(method: str, url: str, headers: dict[str, str], payload_sha256: str, *,
         access_key: str, secret_key: str, region: str, when: _dt.datetime,
         service: str = "s3") -> dict[str, str]:
    """The headers to send, with ``Authorization`` — AWS Signature Version 4."""
    parts = urllib.parse.urlsplit(url)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    signed = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = stamp
    signed["x-amz-content-sha256"] = payload_sha256
    names = sorted(signed)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    canonical_query = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query))
    canonical = "\n".join([
        method,
        parts.path or "/",                 # already encoded, once, by whoever built the URL
        canonical_query,
        "".join(f"{name}:{signed[name]}\n" for name in names),
        ";".join(names),
        payload_sha256,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope,
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode("utf-8"), day), region), service),
                "aws4_request")
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = {k: v for k, v in signed.items() if k != "host"}
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                            f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return out


@dataclass
class S3:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    timeout: float = 120.0
    opener: Callable[..., Any] = urllib.request.urlopen

    def _url(self, key: str = "", query: str = "") -> str:
        base = self.endpoint.rstrip("/")
        path = f"/{_quote(self.bucket)}" + (f"/{_quote(key, safe='/-_.~')}" if key else "")
        return f"{base}{path}" + (f"?{query}" if query else "")

    def request(self, method: str, key: str = "", *, query: str = "", body: bytes = b"",
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        url = self._url(key, query)
        digest = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        sent = sign(method, url, dict(headers or {}), digest, access_key=self.access_key,
                    secret_key=self.secret_key, region=self.region,
                    when=_dt.datetime.now(_dt.timezone.utc))
        request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None,
                                         method=method, headers=sent)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            # The body is read on its own: a timeout there must still come out
            # as an S3Error, not escape from inside this handler.
            try:
                detail = exc.read()[:2000].decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                detail = ""
            message = _xml_text(detail, "Message") or _xml_text(detail, "Code") or exc.reason
            raise S3Error(exc.code, _said(str(message))) from None
        except urllib.error.URLError as exc:
            raise S3Error(0, f"could not reach {self.endpoint}: {exc.reason}") from None
        except (TimeoutError, ConnectionError) as exc:
            raise S3Error(0, f"could not reach {self.endpoint}: {exc}") from None
        except http.client.HTTPException as exc:
            # A broken answer (cut short, or garbled) is a network failure like
            # any other, and the off-site copy only catches OSError per file.
            raise S3Error(0, f"the answer from {self.endpoint} was cut short: {exc}") from None

    # -- the calls ------------------------------------------------------------

    def exists(self, key: str) -> int | None:
        """The object's size, or None when it is not there."""
        try:
            _, headers, _ = self.request("HEAD", key)
        except S3Error as exc:
            if exc.status == 404:
                return None
            raise
        return int({k.lower(): v for k, v in headers.items()}.get("content-length", 0))

    def put(self, key: str, data: bytes) -> None:
        self.request("PUT", key, body=data, headers={"Content-Type": "application/octet-stream"})

    def get(self, key: str) -> bytes:
        return self.request("GET", key)[2]

    def put_file(self, key: str, path: Path, on_bytes: Callable[[int], None] | None = None,
                 stop: Callable[[], bool] | None = None) -> None:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size <= MULTIPART_ABOVE:
                self.put(key, handle.read())
                if on_bytes:
                    on_bytes(size)
                return
            self._multipart(key, handle, on_bytes, stop)

    def _multipart(self, key: str, handle: BinaryIO, on_bytes, stop) -> None:
        _, _, body = self.request("POST", key, query="uploads=",
                                  headers={"Content-Type": "application/octet-stream"})
        upload_id = _xml_text(body.decode("utf-8", "replace"), "UploadId")
        if not upload_id:
            raise S3Error(0, "the service did not start a multipart upload")
        done: list[tuple[int, str]] = []
        try:
            number = 0
            while chunk := handle.read(PART):
                if stop and stop():
                    raise InterruptedError("stopped")
                number += 1
                query = f"partNumber={number}&uploadId={_quote(upload_id)}"
                _, headers, _ = self.request("PUT", key, query=query, body=chunk)
                etag = {k.lower(): v for k, v in headers.items()}.get("etag", "")
                done.append((number, etag))
                if on_bytes:
                    on_bytes(len(chunk))
            listing = "".join(f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in done)
            _, _, answer = self.request(
                "POST", key, query=f"uploadId={_quote(upload_id)}",
                body=f"<CompleteMultipartUpload>{listing}</CompleteMultipartUpload>".encode(),
                headers={"Content-Type": "application/xml"})
            # S3 answers the completion 200 at once and may still fail it,
            # in the body: an <Error> there means no object was made.
            failure = _error_in(answer)
            if failure:
                raise S3Error(500, f"the service could not put the parts together: {failure}")
        except BaseException:
            try:
                self.request("DELETE", key, query=f"uploadId={_quote(upload_id)}")
            except S3Error:
                pass
            raise

    def get_file(self, key: str, path: Path) -> None:
        url = self._url(key)
        sent = sign("GET", url, {}, EMPTY_SHA256, access_key=self.access_key,
                    secret_key=self.secret_key, region=self.region,
                    when=_dt.datetime.now(_dt.timezone.utc))
        request = urllib.request.Request(url, method="GET", headers=sent)
        try:
            with self.opener(request, timeout=self.timeout) as response, create_new(path) as out:
                while chunk := response.read(4 * 1024 * 1024):
                    out.write(chunk)
        except urllib.error.HTTPError as exc:
            raise S3Error(exc.code, str(exc.reason)) from None
        except urllib.error.URLError as exc:
            raise S3Error(0, f"could not reach {self.endpoint}: {exc.reason}") from None
        except (TimeoutError, ConnectionError) as exc:
            raise S3Error(0, f"could not reach {self.endpoint}: {exc}") from None
        except http.client.HTTPException as exc:
            raise S3Error(0, f"the answer from {self.endpoint} was cut short: {exc}") from None


def _error_in(body: bytes) -> str:
    """The message of an ``<Error>`` document, or "" when *body* is not one."""
    text = body.decode("utf-8", "replace").strip()
    try:
        root = ET.fromstring(text) if text else None
    except ET.ParseError:
        return ""
    if root is None or root.tag.split("}")[-1] != "Error":
        return ""
    return _said(_xml_text(text, "Message") or _xml_text(text, "Code") or "unknown error")


def _said(message: str) -> str:
    """What the service said, fit to show on the console: one line, printable
    characters only, and no longer than an error message needs to be. The
    ``<Message>`` is whatever answered at the address — not necessarily S3 —
    and the console's "test" button shows it verbatim."""
    text = "".join(ch if ch.isprintable() else " " for ch in message)
    return " ".join(text.split())[:200]


def _xml_text(text: str, tag: str) -> str:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ""
    for element in root.iter():
        if element.tag.split("}")[-1] == tag:
            return element.text or ""
    return ""
