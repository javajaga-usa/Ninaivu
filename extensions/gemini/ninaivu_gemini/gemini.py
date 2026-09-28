"""Google Gemini AI image processing and multimodal understanding for photos and media.

Provides:
- Generative text-to-image and image-to-image editing using Gemini image models
  (e.g. gemini-3.1-flash-image / gemini-3-pro-image).
- Multimodal photo analysis, detailed captioning, semantic tagging, and photographic
  critique using Gemini 3.8 Flash (gemini-3.8-flash).
- Intelligent non-destructive editing planning mapping user requests to Ninaivu sliders.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from ninaivu.media.ai_editing import (
    CROPS,
    LIMITS,
    check_prompt,
    local_setting,
    validate_adjustments,
)

DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_VISION_MODEL = "gemini-3.8-flash"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def get_api_key() -> str:
    """Retrieve Gemini API key from environment or local settings."""
    key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("NINAIVU_GEMINI_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or local_setting("gemini_api_key")
    )
    return key.strip() if isinstance(key, str) else ""


def image_model_name() -> str:
    """Configured image generation/editing model."""
    name = (
        os.environ.get("NINAIVU_GEMINI_IMAGE_MODEL")
        or local_setting("gemini_image_model")
        or DEFAULT_IMAGE_MODEL
    )
    return name.strip()


def vision_model_name() -> str:
    """Configured multimodal vision model."""
    name = (
        os.environ.get("NINAIVU_GEMINI_VISION_MODEL")
        or local_setting("gemini_vision_model")
        or DEFAULT_VISION_MODEL
    )
    return name.strip()


def is_available() -> bool:
    """Return True if Gemini is configured with a non-empty API key."""
    return bool(get_api_key())


#: Where a key set from the console is kept. The environment variables above
#: win over it, so a key an operator put in the service's environment is not
#: quietly replaced by one somebody typed into a web page.
_KEY_SETTING = "gemini_api_key"
_ENV_KEYS = ("GEMINI_API_KEY", "NINAIVU_GEMINI_KEY", "GOOGLE_API_KEY")


def key_status() -> dict[str, Any]:
    """Whether there is a key, where it came from — and never the key itself.

    Only the last four characters are ever shown, which is enough to tell two
    keys apart and not enough to use one.
    """
    for name in _ENV_KEYS:
        if (os.environ.get(name) or "").strip():
            key = os.environ[name].strip()
            return {"set": True, "source": "environment", "variable": name,
                    "hint": f"…{key[-4:]}" if len(key) >= 8 else ""}
    key = local_setting(_KEY_SETTING)
    key = key.strip() if isinstance(key, str) else ""
    if key:
        return {"set": True, "source": "console", "variable": "",
                "hint": f"…{key[-4:]}" if len(key) >= 8 else ""}
    return {"set": False, "source": "", "variable": "", "hint": ""}


def save_api_key(key: str) -> None:
    """Keep a key typed into the console, or remove it when given nothing.

    Written into the same settings file the model paths live in, which is
    already kept out of version control. The file is narrowed to its owner
    where the platform allows it; on Windows that is a no-op, and the file
    sits inside Ninaivu's own folder either way.
    """
    from ninaivu.media.model_catalog import settings_path                  # noqa: PLC0415

    key = (key or "").strip()
    if key and (len(key) > 200 or any(c.isspace() for c in key)):
        raise ValueError("That does not look like a Gemini API key.")
    target = settings_path()
    try:
        current = json.loads(target.read_text()) if target.is_file() else {}
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    if key:
        current[_KEY_SETTING] = key
    else:
        current.pop(_KEY_SETTING, None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(current, indent=2))
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def check_api_key(key: str,
                  opener: Any = urllib.request.urlopen) -> tuple[bool, str]:
    """Ask Google whether a key works, before anybody relies on it.

    Lists the models the key can reach: no photograph is sent and nothing is
    charged. Better to hear now that a key is mistyped than at the moment
    somebody asks for an edit and gets a refusal instead.
    """
    request = urllib.request.Request(
        f"{API_BASE}/models?pageSize=1",
        headers={"x-goog-api-key": key.strip(), "User-Agent": "Ninaivu/2.0"})
    try:
        with opener(request, timeout=20) as response:
            response.read()
        return True, ""
    except urllib.error.HTTPError as error:
        if error.code in (400, 401, 403):
            return False, "Google did not accept that key."
        if error.code == 429:
            return False, "Google is rate-limiting this key; it may still be fine."
        return False, f"Google answered with an error ({error.code})."
    except (urllib.error.URLError, OSError):
        return False, "Could not reach Google to check the key."


def capabilities() -> dict[str, Any]:
    """Summary of Gemini capabilities for photo & media processing."""
    available = is_available()
    return {
        "gemini_enabled": available,
        "gemini_image_model": image_model_name(),
        "gemini_vision_model": vision_model_name(),
        "gemini_has_key": available,
    }


def _call_interactions_api(api_key: str, model: str, inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute an interaction with the Gemini API via SDK or HTTP fallback."""
    # First try google-genai SDK if present
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        interaction = client.interactions.create(model=model, input=inputs)
        # Extract output fields
        result: dict[str, Any] = {"id": getattr(interaction, "id", "")}
        if getattr(interaction, "output_image", None):
            out_img = interaction.output_image
            result["output_image"] = {
                "data": getattr(out_img, "data", ""),
                "mime_type": getattr(out_img, "mime_type", "image/png"),
            }
        if getattr(interaction, "output_text", None):
            result["output_text"] = interaction.output_text
        return result
    except ImportError:
        pass
    except Exception as exc:
        err_msg = str(exc)
        # Re-raise descriptive error
        if "RESOURCE_EXHAUSTED" in err_msg or "429" in err_msg:
            raise RuntimeError("Gemini API rate limit or quota exceeded. Please try again shortly.") from exc
        if "PERMISSION_DENIED" in err_msg or "API_KEY_INVALID" in err_msg or "403" in err_msg:
            raise RuntimeError("Invalid Gemini API key or unauthorized request.") from exc
        raise RuntimeError(f"Gemini API request failed: {err_msg}") from exc

    # Fallback to direct HTTP request using urllib
    #
    # The key goes in a header, not in the URL. A URL is the part of a request
    # that gets written down — by proxies, by anything logging what was
    # fetched — and a key in its query string is a key in all of those places.
    url = f"{API_BASE}/interactions"
    payload = json.dumps({"model": model, "input": inputs}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Ninaivu/2.0",
                 "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            out: dict[str, Any] = {"id": data.get("id", "")}
            # Search for model_output in steps
            for step in data.get("steps", []):
                if step.get("type") == "model_output":
                    for part in step.get("content", []):
                        if part.get("type") == "image":
                            out["output_image"] = {
                                "data": part.get("data", ""),
                                "mime_type": part.get("mime_type", "image/png"),
                            }
                        elif part.get("type") == "text":
                            out["output_text"] = (out.get("output_text") or "") + part.get("text", "")
            return out
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
            msg = parsed.get("error", {}).get("message", raw)
        except Exception:
            msg = raw
        if error.code == 429:
            raise RuntimeError("Gemini API rate limit or quota exceeded. Please try again shortly.") from error
        if error.code in (401, 403):
            raise RuntimeError("Invalid Gemini API key or access denied.") from error
        raise RuntimeError(f"Gemini API error ({error.code}): {msg}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach Google Gemini service: {error.reason}") from error


def generate_image_edit(prompt: str, image_bytes: bytes, options: dict[str, Any] | None = None) -> bytes:
    """Perform generative image editing with Gemini image model.

    Takes instruction prompt + image bytes, returns generated PNG bytes.
    """
    key = get_api_key()
    if not key:
        raise RuntimeError("Gemini API key is not configured. Set GEMINI_API_KEY to enable Gemini image processing.")

    valid_prompt = check_prompt(prompt)
    model = image_model_name()
    side = 1024

    with Image.open(io.BytesIO(image_bytes)) as source:
        if source.format != "PNG" and source.format != "JPEG" and source.format != "WEBP":
            source = source.convert("RGB")
        image = source.convert("RGB")

    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    prep_buf = io.BytesIO()
    image.save(prep_buf, format="PNG")
    b64_image = base64.b64encode(prep_buf.getvalue()).decode("utf-8")

    inputs = [
        {"type": "text", "text": valid_prompt},
        {"type": "image", "data": b64_image, "mime_type": "image/png"},
    ]

    response = _call_interactions_api(key, model, inputs)
    img_data = response.get("output_image")
    if not img_data or not img_data.get("data"):
        raise RuntimeError("Gemini did not return an image for this request. Try adjusting your prompt.")

    raw_bytes = base64.b64decode(img_data["data"])
    with Image.open(io.BytesIO(raw_bytes)) as output_img:
        result_buf = io.BytesIO()
        meta = PngInfo()
        meta.add_text("Ninaivu generation", f"provider=gemini; model={model}; prompt={valid_prompt[:100]}")
        output_img.save(result_buf, "PNG", pnginfo=meta)
        return result_buf.getvalue()


def analyze_image(image_bytes: bytes, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze a photo or media frame using Gemini multimodal vision.

    Returns:
      caption: Natural language summary of the photograph
      tags: List of semantic keywords for discovery and search
      critique: Photographic assessment (exposure, lighting, composition)
      adjustments: Suggested slider values for Ninaivu
    """
    key = get_api_key()
    if not key:
        raise RuntimeError("Gemini API key is not configured. Set GEMINI_API_KEY to enable Gemini image analysis.")

    model = vision_model_name()
    side = 1024

    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    prep_buf = io.BytesIO()
    image.save(prep_buf, format="JPEG", quality=85)
    b64_image = base64.b64encode(prep_buf.getvalue()).decode("utf-8")

    prompt = (
        "Analyze this photograph carefully. Return ONLY a valid JSON object with the following keys:\n"
        "- caption: A descriptive 1-2 sentence caption of what is in the photograph.\n"
        "- tags: A list of 5-10 concise lowercase keywords describing objects, setting, mood, and lighting.\n"
        "- critique: A brief 1-2 sentence photography assessment of lighting, tone, and composition.\n"
        "- adjustments: An object with recommended non-destructive slider corrections from:\n"
        "    exposure (-100 to 100), contrast (-100 to 100), saturation (-100 to 100),\n"
        "    warmth (-100 to 100), shadows (-100 to 100), highlights (-100 to 100),\n"
        "    sharpness (0 to 100), noise (0 to 100), vignette (0 to 100).\n"
        "Keep adjustment magnitudes moderate (typically 10-25) and omit sliders that need no change.\n"
        "Return NO markdown backticks or commentary, just pure JSON."
    )

    inputs = [
        {"type": "text", "text": prompt},
        {"type": "image", "data": b64_image, "mime_type": "image/jpeg"},
    ]

    response = _call_interactions_api(key, model, inputs)
    text = response.get("output_text", "").strip()
    if not text:
        raise RuntimeError("Gemini did not return an analysis for this photograph.")

    # Clean potential markdown formatting
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                raise ValueError("Could not parse Gemini image analysis output.") from err
        else:
            raise ValueError("Gemini returned invalid analysis structure.") from err

    caption = str(data.get("caption", "")).strip()[:1000]
    tags_raw = data.get("tags", [])
    if isinstance(tags_raw, list):
        tags = [re.sub(r"[^\w\s-]", "", str(t).lower()).strip() for t in tags_raw if str(t).strip()][:15]
    else:
        tags = []

    critique = str(data.get("critique", "")).strip()[:1000]
    raw_adj = data.get("adjustments", {})
    adjustments: dict[str, float] = {}
    if isinstance(raw_adj, dict):
        for k, v in raw_adj.items():
            if k in LIMITS and isinstance(v, (int, float)):
                low, high = LIMITS[k]
                adjustments[k] = max(low, min(high, float(v)))

    return {
        "caption": caption,
        "tags": tags,
        "critique": critique,
        "adjustments": adjustments,
        "model": model,
        "provider": f"Google Gemini · {model}",
    }


def plan_adjustments(prompt: str, current: dict[str, Any], image_bytes: bytes | None = None) -> dict[str, Any]:
    """Plan non-destructive photo adjustments using Gemini multimodal reasoning.

    Translates user editing instruction into Ninaivu slider values.
    """
    key = get_api_key()
    if not key:
        raise RuntimeError("Gemini API key is not configured. Set GEMINI_API_KEY to enable Gemini AI planning.")

    checked_prompt = check_prompt(prompt)
    current = validate_adjustments(current)
    model = vision_model_name()

    system_desc = (
        "You plan non-destructive photo adjustments. Return ONLY a JSON object with:\n"
        '- summary: Brief 1-sentence description of the resulting edit.\n'
        '- unsupported: Boolean. Set true if the request requires generative object addition/removal/drawing;\n'
        '  otherwise false.\n'
        '- adjustments: Dictionary of absolute slider values for changed controls only.\n'
        'Limits: exposure, contrast, saturation, warmth, shadows, highlights each -100 to 100;\n'
        'sharpness, noise, vignette each 0 to 100; angle -10 to 10; crop one of [original, square, landscape, portrait].\n'
        'Do NOT output markdown blocks or backticks, return raw JSON only.'
    )

    inputs: list[dict[str, Any]] = [
        {"type": "text", "text": f"{system_desc}\n\nUser Request: {checked_prompt}\nCurrent Settings: {json.dumps(current)}"}
    ]

    if image_bytes:
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                img = source.convert("RGB")
            img.thumbnail((512, 512), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            inputs.append({
                "type": "image",
                "data": base64.b64encode(buf.getvalue()).decode("utf-8"),
                "mime_type": "image/jpeg",
            })
        except Exception:
            pass

    response = _call_interactions_api(key, model, inputs)
    text = response.get("output_text", "").strip()
    if not text:
        raise RuntimeError("Gemini did not return an editing plan.")

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        output = json.loads(text)
    except json.JSONDecodeError as err:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            output = json.loads(match.group(0))
        else:
            raise ValueError("Gemini returned an invalid editing plan structure.") from err

    if not isinstance(output, dict) or "adjustments" not in output:
        raise ValueError("Invalid plan structure received from Gemini.")

    raw_patch = output.get("adjustments", {})
    clean_patch: dict[str, Any] = {}
    if isinstance(raw_patch, dict):
        for k, v in raw_patch.items():
            if k == "crop" and v in CROPS:
                clean_patch["crop"] = v
            elif k in LIMITS and isinstance(v, (int, float)):
                low, high = LIMITS[k]
                clean_patch[k] = max(low, min(high, float(v)))

    unsupported = bool(output.get("unsupported", False))
    summary = str(output.get("summary", "")).strip()[:500]

    if unsupported:
        raise ValueError(summary or "This request requires generative editing or object removal.")

    return {
        "patch": clean_patch,
        "summary": summary or "Adjusted photo parameters.",
        "provider": f"Gemini · {model}",
    }
