"""Tests for Google Gemini Image Processing AI integration for Photos and media."""
from __future__ import annotations

import base64
import io
import json
import pytest
from PIL import Image

# The extension package, or nothing here runs: the Gemini code is no longer in
# the core. ``pip install -e extensions/gemini``, or set NINAIVU_EXTENSION_MODULES
# with extensions/gemini on the path, as CI does.
gemini_media = pytest.importorskip("ninaivu_gemini.gemini")


@pytest.fixture()
def cfg(cfg, monkeypatch):
    """The core's config with the Gemini extension switched on."""
    from ninaivu import extensions
    monkeypatch.setenv(extensions.DEV_MODULES_VAR, "ninaivu_gemini")
    extensions.discover(refresh=True)
    cfg.extensions = ["gemini"]
    return cfg


def _make_test_png(width: int = 64, height: int = 64, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Config & Capabilities
# ---------------------------------------------------------------------------

def test_gemini_credentials_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("NINAIVU_GEMINI_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert gemini_media.get_api_key() == ""
    assert not gemini_media.is_available()

    monkeypatch.setenv("GOOGLE_API_KEY", "google-key-123")
    assert gemini_media.get_api_key() == "google-key-123"

    monkeypatch.setenv("NINAIVU_GEMINI_KEY", "ninaivu-key-456")
    assert gemini_media.get_api_key() == "ninaivu-key-456"

    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-789")
    assert gemini_media.get_api_key() == "gemini-key-789"
    assert gemini_media.is_available()


def test_gemini_model_names_and_capabilities(monkeypatch):
    monkeypatch.delenv("NINAIVU_GEMINI_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("NINAIVU_GEMINI_VISION_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert gemini_media.image_model_name() == "gemini-3.1-flash-image"
    assert gemini_media.vision_model_name() == "gemini-3.8-flash"

    caps = gemini_media.capabilities()
    assert caps["gemini_enabled"] is False
    assert caps["gemini_image_model"] == "gemini-3.1-flash-image"
    assert caps["gemini_vision_model"] == "gemini-3.8-flash"

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("NINAIVU_GEMINI_IMAGE_MODEL", "gemini-3-pro-image")
    monkeypatch.setenv("NINAIVU_GEMINI_VISION_MODEL", "gemini-flash-latest")

    assert gemini_media.image_model_name() == "gemini-3-pro-image"
    assert gemini_media.vision_model_name() == "gemini-flash-latest"

    caps2 = gemini_media.capabilities()
    assert caps2["gemini_enabled"] is True
    assert caps2["gemini_image_model"] == "gemini-3-pro-image"
    assert caps2["gemini_vision_model"] == "gemini-flash-latest"


# ---------------------------------------------------------------------------
# Generative Image Editing
# ---------------------------------------------------------------------------

def test_generate_image_edit_unconfigured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("NINAIVU_GEMINI_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Gemini API key is not configured"):
        gemini_media.generate_image_edit("Make it vintage", _make_test_png())


def test_generate_image_edit_safety_filter(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(ValueError, match="outside the supported safe photography edits"):
        gemini_media.generate_image_edit("undress this person", _make_test_png())


def test_generate_image_edit_success(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    generated_png = _make_test_png(128, 128, "green")
    b64_out = base64.b64encode(generated_png).decode("utf-8")

    def mock_call(api_key, model, inputs):
        assert api_key == "test-key"
        assert model == "gemini-3.1-flash-image"
        assert len(inputs) == 2
        assert inputs[0]["type"] == "text"
        assert inputs[0]["text"] == "Add dramatic sunset lighting"
        assert inputs[1]["type"] == "image"
        return {"output_image": {"data": b64_out, "mime_type": "image/png"}}

    monkeypatch.setattr(gemini_media, "_call_interactions_api", mock_call)
    result = gemini_media.generate_image_edit("Add dramatic sunset lighting", _make_test_png())
    assert isinstance(result, bytes)
    with Image.open(io.BytesIO(result)) as img:
        assert img.format == "PNG"
        assert img.size == (128, 128)
        assert "provider=gemini" in img.info.get("Ninaivu generation", "")


def test_generate_image_edit_no_image_returned(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def mock_call(api_key, model, inputs):
        return {"output_text": "I could not edit this photo"}

    monkeypatch.setattr(gemini_media, "_call_interactions_api", mock_call)
    with pytest.raises(RuntimeError, match="Gemini did not return an image"):
        gemini_media.generate_image_edit("Turn into oil painting", _make_test_png())


# ---------------------------------------------------------------------------
# Multimodal Visual Analysis
# ---------------------------------------------------------------------------

def test_analyze_image_unconfigured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("NINAIVU_GEMINI_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Gemini API key is not configured"):
        gemini_media.analyze_image(_make_test_png())


def test_analyze_image_success(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    sample_response = {
        "caption": "A bright red square demonstrating color accuracy in studio lighting.",
        "tags": ["studio", "red", "vibrant", "lighting", "minimalist"],
        "critique": "Good color saturation and clean framing. Consider slight contrast boost.",
        "adjustments": {
            "contrast": 15,
            "exposure": -5,
            "shadows": 20,
            "highlights": -10,
            "saturation": 999,  # Should be clamped to 100
            "invalid_slider": 50,  # Should be ignored
        },
    }

    def mock_call(api_key, model, inputs):
        assert model == "gemini-3.8-flash"
        # Wrap in markdown backticks to test cleaning
        return {"output_text": f"```json\n{json.dumps(sample_response)}\n```"}

    monkeypatch.setattr(gemini_media, "_call_interactions_api", mock_call)
    res = gemini_media.analyze_image(_make_test_png())
    assert "bright red square" in res["caption"]
    assert "studio" in res["tags"]
    assert "red" in res["tags"]
    assert res["adjustments"]["contrast"] == 15
    assert res["adjustments"]["saturation"] == 100.0
    assert "invalid_slider" not in res["adjustments"]
    assert "Google Gemini" in res["provider"]


def test_analyze_image_malformed_json_handling(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def mock_call(api_key, model, inputs):
        return {"output_text": "Here is the result: {\"caption\": \"A nice landscape\", \"tags\": [\"nature\"], \"adjustments\": {}} extra text"}

    monkeypatch.setattr(gemini_media, "_call_interactions_api", mock_call)
    res = gemini_media.analyze_image(_make_test_png())
    assert res["caption"] == "A nice landscape"
    assert res["tags"] == ["nature"]


# ---------------------------------------------------------------------------
# Intelligent Editing Planning
# ---------------------------------------------------------------------------

def test_plan_adjustments_success(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    sample_plan = {
        "summary": "Warm golden hour look with softened highlights.",
        "unsupported": False,
        "adjustments": {
            "warmth": 25,
            "highlights": -15,
            "shadows": 10,
            "crop": "square",
        },
    }

    def mock_call(api_key, model, inputs):
        return {"output_text": json.dumps(sample_plan)}

    monkeypatch.setattr(gemini_media, "_call_interactions_api", mock_call)
    plan = gemini_media.plan_adjustments("Give this a warm golden hour look and crop to square", {"warmth": 0}, _make_test_png())
    assert plan["patch"] == {"warmth": 25.0, "highlights": -15.0, "shadows": 10.0, "crop": "square"}
    assert "golden hour" in plan["summary"]
    assert "Gemini" in plan["provider"]


def test_plan_adjustments_unsupported_request(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    sample_plan = {
        "summary": "Adding a flying dragon requires generative editing.",
        "unsupported": True,
        "adjustments": {},
    }

    def mock_call(api_key, model, inputs):
        return {"output_text": json.dumps(sample_plan)}

    monkeypatch.setattr(gemini_media, "_call_interactions_api", mock_call)
    with pytest.raises(ValueError, match="Adding a flying dragon requires generative editing"):
        gemini_media.plan_adjustments("Add a dragon in the sky", {})


# ---------------------------------------------------------------------------
# API Routes Integration
# ---------------------------------------------------------------------------

def test_api_capabilities_includes_gemini(as_family, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("NINAIVU_GEMINI_KEY", raising=False)
    res = as_family.get("/api/ai-playground/capabilities")
    assert res.status_code == 200
    data = res.get_json()
    assert "gemini_enabled" in data
    assert data["gemini_enabled"] is False

    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    res2 = as_family.get("/api/ai-playground/capabilities")
    data2 = res2.get_json()
    assert data2["gemini_enabled"] is True
    assert data2["gemini_image_model"] == "gemini-3.1-flash-image"
    assert data2["gemini_vision_model"] == "gemini-3.8-flash"
    assert data2["image_model"] is True


def test_api_generate_with_gemini_provider(as_family, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    test_img = _make_test_png(48, 48, "purple")
    b64_img = base64.b64encode(test_img).decode("utf-8")

    out_img = _make_test_png(48, 48, "yellow")
    monkeypatch.setattr(gemini_media, "generate_image_edit", lambda prompt, image, opts: out_img)

    res = as_family.post(
        "/api/ai-playground/generate",
        json={"prompt": "Turn yellow", "image": b64_img, "provider": "gemini"},
    )
    assert res.status_code == 200
    assert res.mimetype == "image/png"
    assert res.data == out_img


def test_api_plan_with_gemini_provider(as_family, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    monkeypatch.setattr(
        gemini_media,
        "plan_adjustments",
        lambda prompt, current, img: {"patch": {"warmth": 20}, "summary": "Warmed up", "provider": "Gemini"},
    )

    res = as_family.post(
        "/api/ai-playground/plan",
        json={"prompt": "Make it warmer", "current": {}, "provider": "gemini"},
    )
    assert res.status_code == 200
    assert res.get_json() == {"patch": {"warmth": 20}, "summary": "Warmed up", "provider": "Gemini"}


def test_dedicated_gemini_endpoints(as_family, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    test_img = _make_test_png(32, 32, "blue")
    b64_img = base64.b64encode(test_img).decode("utf-8")

    # Generate endpoint
    monkeypatch.setattr(gemini_media, "generate_image_edit", lambda prompt, image, opts: test_img)
    res_gen = as_family.post(
        "/api/ai-playground/gemini/generate",
        json={"prompt": "Add stars", "image": b64_img},
    )
    assert res_gen.status_code == 200
    assert res_gen.data == test_img

    # Analyze endpoint
    analysis_sample = {
        "caption": "A blue square in high key.",
        "tags": ["blue", "square", "minimal"],
        "critique": "Clean contrast.",
        "adjustments": {"contrast": 10},
        "model": "gemini-3.8-flash",
        "provider": "Google Gemini · gemini-3.8-flash",
    }
    monkeypatch.setattr(gemini_media, "analyze_image", lambda image, opts: analysis_sample)
    res_ana = as_family.post(
        "/api/ai-playground/gemini/analyze",
        json={"image": b64_img},
    )
    assert res_ana.status_code == 200
    assert res_ana.get_json() == analysis_sample

    # Plan endpoint
    plan_sample = {"patch": {"contrast": 10}, "summary": "Increased contrast", "provider": "Gemini"}
    monkeypatch.setattr(gemini_media, "plan_adjustments", lambda prompt, current, img: plan_sample)
    res_plan = as_family.post(
        "/api/ai-playground/gemini/plan",
        json={"prompt": "Boost contrast", "current": {}},
    )
    assert res_plan.status_code == 200
    assert res_plan.get_json() == plan_sample


def test_dedicated_gemini_endpoints_blocked_for_guest(as_guest):
    res_gen = as_guest.post("/api/ai-playground/gemini/generate", json={})
    assert res_gen.status_code == 403

    res_ana = as_guest.post("/api/ai-playground/gemini/analyze", json={})
    assert res_ana.status_code == 403

    res_plan = as_guest.post("/api/ai-playground/gemini/plan", json={})
    assert res_plan.status_code == 403


# -- Gemini "analyze" read any library file whole into memory (audit of 25 Sept) --

def test_gemini_is_asked_about_photographs_only(app, people, monkeypatch):
    """A video's id was read into memory whole — gigabytes, for one request —
    before anything checked that it was a photograph."""
    from conftest import FAMILY, login

    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    sent = []
    monkeypatch.setattr(gemini_media, "analyze_image",
                        lambda image, opts: sent.append(len(image)) or {"caption": ""})
    conn = people["conn"]
    video, photo = [r["id"] for r in conn.execute("SELECT id FROM assets ORDER BY id LIMIT 2")]
    conn.execute("UPDATE assets SET kind='video' WHERE id=?", (video,))
    conn.commit()
    family = login(app.test_client(), *FAMILY)
    family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"

    refused = family.post("/api/ai-playground/gemini/analyze", json={"media_id": video})
    assert refused.status_code == 400 and sent == []
    allowed = family.post("/api/ai-playground/gemini/analyze", json={"media_id": photo})
    assert allowed.status_code == 200 and len(sent) == 1
