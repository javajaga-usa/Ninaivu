"""A model whose usual source cannot be reached comes from its mirror.

A home network's web filter answered GitHub's media host with a plain-text
redirect to its own warning page. The face models failed with "[SSL:
WRONG_VERSION_NUMBER] wrong version number" on every try, so the People page
could never be set up, while Hugging Face, where OpenCV publishes the same
files, was reachable the whole time.
"""
from __future__ import annotations

import hashlib
import io
import ssl
import urllib.error

import pytest

from ninaivu.media import faces, model_catalog

BLOCKED = urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))


class _Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _entry(tmp_path, monkeypatch, content):
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(model_catalog.time, "sleep", lambda s: None)
    return {"url": "https://media.example.com/tiny.onnx",
            "mirrors": ["https://mirror.example.com/tiny.onnx"],
            "name": "tiny.onnx", "folder": "",
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def test_a_blocked_source_falls_back_to_the_mirror(tmp_path, monkeypatch):
    content = b"model bytes " * 200
    entry = _entry(tmp_path, monkeypatch, content)
    asked = []

    def opener(request, timeout):
        asked.append(request.full_url)
        if "media.example.com" in request.full_url:
            raise BLOCKED
        return _Response(content)

    seen = []
    path = model_catalog.download_file(entry, seen.append, opener)
    assert path.read_bytes() == content
    assert asked == [entry["url"], entry["mirrors"][0]]
    assert sum(seen) == len(content)


def test_a_source_that_sends_other_bytes_falls_back_too(tmp_path, monkeypatch):
    content = b"the real model" * 100
    entry = _entry(tmp_path, monkeypatch, content)

    def opener(request, timeout):
        if "media.example.com" in request.full_url:
            return _Response(b"x" * len(content))            # a filter's page, padded
        return _Response(content)

    seen = []
    path = model_catalog.download_file(entry, seen.append, opener)
    assert path.read_bytes() == content
    assert sum(seen) == len(content)                       # the wrong bytes taken back


def test_when_every_source_fails_the_download_still_fails(tmp_path, monkeypatch):
    content = b"abc" * 100
    entry = _entry(tmp_path, monkeypatch, content)

    def opener(request, timeout):
        raise BLOCKED

    with pytest.raises(urllib.error.URLError, match="WRONG_VERSION_NUMBER"):
        model_catalog.download_file(entry, None, opener)
    assert not list((tmp_path / "models").glob("*.part"))


def test_a_model_without_mirrors_is_fetched_once(tmp_path, monkeypatch):
    content = b"abc" * 100
    entry = {k: v for k, v in _entry(tmp_path, monkeypatch, content).items() if k != "mirrors"}
    asked = []

    def opener(request, timeout):
        asked.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        model_catalog.download_file(entry, None, opener)
    assert asked == [entry["url"]]


def test_the_face_models_have_a_pinned_hugging_face_mirror():
    for entry in model_catalog.MODELS["faces"]["files"]:
        (mirror,) = entry["mirrors"]
        assert mirror.startswith("https://huggingface.co/opencv/")
        assert "/resolve/main/" not in mirror                 # a commit, not a branch
        assert mirror.endswith("/" + entry["name"])
    # The People page's own downloader names the same places.
    pinned = {e["sha256"]: e["mirrors"] for e in model_catalog.MODELS["faces"]["files"]}
    for spec in faces.MODELS.values():
        assert list(spec["mirrors"]) == pinned[spec["sha256"]]


def test_the_people_page_downloader_uses_the_mirror(tmp_path, monkeypatch):
    content = b"face model" * 50
    spec = {"file": "tiny.onnx", "url": "https://media.example.com/tiny.onnx",
            "mirrors": ("https://mirror.example.com/tiny.onnx",),
            "sha256": hashlib.sha256(content).hexdigest()}
    monkeypatch.setattr(faces, "MODELS", {"detector": spec})
    monkeypatch.setattr(faces, "models_dir", lambda state_dir=None: tmp_path)

    def urlopen(request, timeout):
        if "media.example.com" in request.full_url:
            raise BLOCKED
        return _Response(content)

    monkeypatch.setattr(faces.urllib.request, "urlopen", urlopen)
    result = faces.fetch_models(tmp_path)
    assert result["ready"] and result["downloaded"] == ["tiny.onnx"] and not result["errors"]
    assert (tmp_path / "tiny.onnx").read_bytes() == content


def test_the_people_page_downloader_reports_every_source_when_all_fail(tmp_path, monkeypatch):
    spec = {"file": "tiny.onnx", "url": "https://media.example.com/tiny.onnx",
            "mirrors": ("https://mirror.example.com/tiny.onnx",), "sha256": "0" * 64}
    monkeypatch.setattr(faces, "MODELS", {"detector": spec})
    monkeypatch.setattr(faces, "models_dir", lambda state_dir=None: tmp_path)

    def urlopen(request, timeout):
        if "media.example.com" in request.full_url:
            raise BLOCKED
        return _Response(b"not it")

    monkeypatch.setattr(faces.urllib.request, "urlopen", urlopen)
    result = faces.fetch_models(tmp_path)
    assert not result["ready"]
    (error,) = result["errors"]
    assert "WRONG_VERSION_NUMBER" in error and "checksum mismatch" in error
    assert not (tmp_path / "tiny.onnx.part").exists()
