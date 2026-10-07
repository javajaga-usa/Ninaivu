"""The data security audit of 7 October 2026 (DS-02): the Gemini extension.

The photo editor sends pixels, not a file id, so the refusal PR #42 put on
hidden items never applied to it. The editor now names the item, and Gemini
refuses a hidden one.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

gemini_media = pytest.importorskip("ninaivu_gemini.gemini")


@pytest.fixture()
def cfg(cfg, monkeypatch):
    from ninaivu import extensions
    monkeypatch.setenv(extensions.DEV_MODULES_VAR, "ninaivu_gemini")
    extensions.discover(refresh=True)
    cfg.extensions = ["gemini"]
    return cfg


def _png() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_gemini_refuses_a_hidden_photograph_from_the_editor(people, as_admin, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    sent = []
    monkeypatch.setattr(gemini_media, "analyze_image",
                        lambda image, opts: sent.append(image) or {"caption": ""})
    conn = people["conn"]
    secret = conn.execute("SELECT id FROM assets WHERE filename='secret0.jpg'").fetchone()["id"]
    as_admin.post("/api/visibility", json={"ids": [secret], "visibility": "hidden"})
    answer = as_admin.post("/api/ai-playground/gemini/analyze",
                           json={"image": _png(), "media_id": secret})
    assert answer.status_code == 403, answer.get_json()
    assert "Hidden" in answer.get_json()["error"]
    assert sent == []


def test_gemini_still_describes_a_family_photograph_from_the_editor(people, as_admin, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    sent = []
    monkeypatch.setattr(gemini_media, "analyze_image",
                        lambda image, opts: sent.append(image) or {"caption": ""})
    conn = people["conn"]
    photo = conn.execute("SELECT id FROM assets WHERE filename='shot3.jpg'").fetchone()["id"]
    answer = as_admin.post("/api/ai-playground/gemini/analyze",
                           json={"image": _png(), "media_id": photo})
    assert answer.status_code == 200, answer.get_json()
    assert len(sent) == 1
