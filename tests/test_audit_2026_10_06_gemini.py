"""The Gemini extension, from the audit of 6 October 2026 (A-31).

"Analyze" read a photograph by joining its folder and name without the
containment check every other route makes, and what it sends to Google must
never carry the place the photograph was taken.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from conftest import FAMILY, login

gemini_media = pytest.importorskip("ninaivu_gemini.gemini")


@pytest.fixture()
def cfg(cfg, monkeypatch):
    from ninaivu import extensions
    monkeypatch.setenv(extensions.DEV_MODULES_VAR, "ninaivu_gemini")
    extensions.discover(refresh=True)
    cfg.extensions = ["gemini"]
    cfg.outside_ai_for_family = True
    return cfg


def test_analyze_does_not_read_outside_the_library(app, people, scanned, monkeypatch, tmp_path):
    _, conn, _ = scanned
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    sent = []
    monkeypatch.setattr(gemini_media, "analyze_image",
                        lambda image, opts: sent.append(image) or {"caption": ""})
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (32, 32), "red").save(outside)
    row = conn.execute("SELECT id, root FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET rel_path=?, visibility=0 WHERE id=?",
                 ("../outside.jpg", row["id"]))
    conn.commit()
    family = login(app.test_client(), *FAMILY)
    answer = family.post("/api/ai-playground/gemini/analyze", json={"media_id": row["id"]})
    assert answer.status_code == 404
    assert sent == []


def test_what_goes_to_google_carries_no_place(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    calls = []
    monkeypatch.setattr(gemini_media, "_call_interactions_api",
                        lambda key, model, inputs: calls.append(inputs) or {
                            "output_text": '{"caption": "x", "tags": []}'})
    image = Image.new("RGB", (64, 48), "blue")
    exif = image.getexif()
    exif[0x8825] = {1: "N", 2: (13.0, 4.0, 57.0), 3: "E", 4: (80.0, 16.0, 14.0)}
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    assert b"Exif" in buffer.getvalue()
    gemini_media.analyze_image(buffer.getvalue())
    import base64
    picture = next(part for part in calls[0] if part["type"] == "image")
    assert b"Exif" not in base64.b64decode(picture["data"])
