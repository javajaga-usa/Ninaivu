"""Local image tools on ONNX models: object removal, upscaling, face restoration, colourising.

Each runs on this machine with a model from :mod:`model_catalog`, using the
graphics card through DirectML where the model works there and the CPU where
it does not. Measured on an AMD Radeon 840M: Real-ESRGAN ×4 of a 256×320 image
1.0 s on DirectML against 12.4 s on the CPU; GFPGAN 0.34 s against 1.3 s. LaMa
fails on DirectML (an unsupported MatMul) and runs on the CPU in about 2 s.

Colourising runs on a DDColor ONNX file. The catalogue cannot pin one yet, so
it is the one model placed by hand: ``ddcolor.onnx`` in the models folder's
``onnx`` subfolder, or wherever ``NINAIVU_COLORIZE_MODEL`` (or the
``colorize_model`` setting) points. ``colorize_model_path`` says where it is
looked for, and the Playground says the same when the tool is not set up.

Nothing is downloaded here and no photograph leaves the machine.
"""
from __future__ import annotations

import io
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import model_catalog
from .ai_editing import local_setting
from .safe_image import open_untrusted

#: Models known not to run on DirectML; they go straight to the CPU.
CPU_ONLY = {"lama"}
#: The longest input side accepted, and the upscale input cap (×4 → 8192 px).
MAX_INPUT_SIDE = 4096
MAX_UPSCALE_INPUT = 2048
#: Real-ESRGAN runs in tiles so a large photo fits in graphics memory.
TILE = 256
TILE_OVERLAP = 16
#: DDColor sees the photograph's lightness as a square of this many pixels.
COLORIZE_INPUT = 512
#: The file names a hand-placed colourising model is looked for under, in the
#: models folder's ``onnx`` subfolder, in this order.
COLORIZE_FILES = ("ddcolor.onnx", "ddcolor_artistic.onnx")

#: The Enhance tools the Playground offers, each with the model it runs on.
#: "colorize" is not a catalogue entry (see the module docstring), so its
#: label lives here rather than in the catalogue.
ENHANCE_TOOLS = ("upscale", "restore", "colorize")
LABELS = {"colorize": "Colourise (DDColor)"}

_sessions: dict[str, Any] = {}
_lock = threading.Lock()
#: One inference per model at a time; sessions are thread-safe, memory is not.
_running = {model_id: threading.Lock() for model_id in ("lama", "upscale", "restore", "colorize")}


def colorize_model_path() -> Path | None:
    """Where the colourising model is, or None: the environment or the setting
    first, then the usual names in the models folder."""
    configured = os.environ.get("NINAIVU_COLORIZE_MODEL", local_setting("colorize_model"))
    if configured:
        return Path(configured).expanduser()
    for name in COLORIZE_FILES:
        candidate = model_catalog.models_root() / "onnx" / name
        if candidate.is_file():
            return candidate
    return None


def _onnxruntime_present() -> bool:
    import importlib.util
    return importlib.util.find_spec("onnxruntime") is not None


def available(model_id: str) -> bool:
    if model_id == "colorize":
        path = colorize_model_path()
        return bool(path and path.is_file()) and _onnxruntime_present()
    return model_catalog.installed(model_id) and not model_catalog.missing_packages(model_id)


def label(model_id: str) -> str:
    return LABELS.get(model_id) or str(model_catalog.MODELS[model_id]["label"])


def _model_path(model_id: str) -> str:
    if model_id == "colorize":
        return str(colorize_model_path())
    return str(model_catalog.file_path(model_catalog.MODELS[model_id]["files"][0]))


def _session(model_id: str, *, cpu: bool = False):
    import onnxruntime as ort

    key = f"{model_id}:{'cpu' if cpu else 'auto'}"
    with _lock:
        if key in _sessions:
            return _sessions[key]
        path = _model_path(model_id)
        options = ort.SessionOptions()
        providers = ["CPUExecutionProvider"]
        if not cpu and model_id not in CPU_ONLY and "DmlExecutionProvider" in ort.get_available_providers():
            # DirectML's documented requirements for a session.
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
        _sessions[key] = ort.InferenceSession(path, options, providers=providers)
        return _sessions[key]


def _run(model_id: str, feeds: dict[str, np.ndarray]) -> np.ndarray:
    """Run once, on the graphics card if possible, falling back to the CPU for good."""
    if not available(model_id):
        if model_id == "colorize":
            raise RuntimeError("The colourising model is not installed. An administrator places a "
                               "DDColor ONNX file (ddcolor.onnx) in the AI models folder's onnx "
                               "subfolder; the console's AI models tab says where that is.")
        raise RuntimeError(f"The {label(model_id)} model is not installed. "
                           "An administrator can download it in the console's AI models tab.")
    with _running[model_id]:
        session = _session(model_id)
        try:
            return session.run(None, feeds)[0]
        except Exception:                               # noqa: BLE001 - a provider failure
            if session.get_providers()[0] == "CPUExecutionProvider":
                raise
            # The graphics card could not run it; use the CPU from now on.
            cpu_session = _session(model_id, cpu=True)
            with _lock:
                _sessions[f"{model_id}:auto"] = cpu_session
            return cpu_session.run(None, feeds)[0]


def _open(data: bytes, limit: int = MAX_INPUT_SIDE) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        if max(source.size) > limit:
            raise ValueError(f"Use a photo up to {limit} pixels on a side.")
        return source.convert("RGB")


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# CIE Lab, the way the colourising model speaks it (D65, L 0–100, a and b about ±127)
# ---------------------------------------------------------------------------

_XN, _ZN = 0.950456, 1.088754


def _linear(srgb: np.ndarray) -> np.ndarray:
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def _encode(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def _lab_f(t: np.ndarray) -> np.ndarray:
    return np.where(t > 0.008856, np.cbrt(t), 7.787 * t + 16 / 116)


def _lab_f_inverse(t: np.ndarray) -> np.ndarray:
    return np.where(t > 6 / 29, t ** 3, (t - 16 / 116) / 7.787)


def lightness(rgb: np.ndarray) -> np.ndarray:
    """CIE L* (0–100) of an sRGB image given as floats 0–1, shape (…, 3)."""
    linear = _linear(rgb)
    y = 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]
    return 116 * _lab_f(y) - 16


def grey_of_lightness(light: np.ndarray) -> np.ndarray:
    """The neutral sRGB grey (floats 0–1, shape (…, 3)) that has this L*."""
    y = _lab_f_inverse((light + 16) / 116)
    return np.repeat(_encode(y)[..., None], 3, axis=-1)


def lab_to_rgb(light: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """sRGB floats 0–1, shape (…, 3), from L*, a*, b* planes."""
    fy = (light + 16) / 116
    x = _XN * _lab_f_inverse(fy + a / 500)
    y = _lab_f_inverse(fy)
    z = _ZN * _lab_f_inverse(fy - b / 200)
    linear = np.stack([3.2406 * x - 1.5372 * y - 0.4986 * z,
                       -0.9689 * x + 1.8758 * y + 0.0415 * z,
                       0.0557 * x - 0.2040 * y + 1.0570 * z], axis=-1)
    return _encode(linear)


# ---------------------------------------------------------------------------
# Object removal
# ---------------------------------------------------------------------------

def remove(image_bytes: bytes, mask_bytes: bytes) -> bytes:
    """Fill the painted area with LaMa, working on a region around it.

    LaMa works at 512×512. Shrinking a whole photo to that loses detail
    everywhere, so the model sees a square around the painted area with
    context on every side, and only the painted pixels (with a soft edge)
    are written back into the full-size photo.
    """
    image = _open(image_bytes)
    with open_untrusted(mask_bytes, MAX_INPUT_SIDE * MAX_INPUT_SIDE) as source:
        painted = source.convert("L").resize(image.size, Image.Resampling.NEAREST)
    mask = painted.point(lambda value: 255 if value > 24 else 0)
    box = mask.getbbox()
    if not box:
        raise ValueError("Paint over the object to remove before applying.")

    left, top, right, bottom = box
    side = max(right - left, bottom - top) * 2 + 64
    side = min(max(side, 256), max(image.size))
    cx, cy = (left + right) // 2, (top + bottom) // 2
    x0 = max(0, min(cx - side // 2, image.width - side))
    y0 = max(0, min(cy - side // 2, image.height - side))
    region = (x0, y0, min(image.width, x0 + side), min(image.height, y0 + side))

    crop = image.crop(region).resize((512, 512), Image.Resampling.LANCZOS)
    crop_mask = mask.crop(region).resize((512, 512), Image.Resampling.NEAREST)
    pixels = np.asarray(crop, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    holes = (np.asarray(crop_mask, dtype=np.float32) > 127).astype(np.float32)[None, None]
    output = _run("lama", {"image": pixels, "mask": holes})[0]
    filled = Image.fromarray(np.clip(output.transpose(1, 2, 0), 0, 255).astype(np.uint8))
    filled = filled.resize((region[2] - region[0], region[3] - region[1]), Image.Resampling.LANCZOS)

    blend = mask.crop(region).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(3))
    result = image.copy()
    result.paste(Image.composite(filled, image.crop(region), blend), region[:2])
    return _png(result)


# ---------------------------------------------------------------------------
# Upscaling
# ---------------------------------------------------------------------------

def upscale(image_bytes: bytes, on_progress=None) -> bytes:
    """Real-ESRGAN ×4, in overlapping tiles."""
    image = _open(image_bytes, MAX_UPSCALE_INPUT)
    pixels = np.asarray(image, dtype=np.float32) / 255.0
    height, width, _ = pixels.shape
    output = np.zeros((height * 4, width * 4, 3), dtype=np.float32)
    weight = np.zeros((height * 4, width * 4, 1), dtype=np.float32)
    step = TILE - TILE_OVERLAP
    starts_y = list(range(0, max(1, height - TILE_OVERLAP), step)) or [0]
    starts_x = list(range(0, max(1, width - TILE_OVERLAP), step)) or [0]
    total, done = len(starts_y) * len(starts_x), 0
    for y in starts_y:
        for x in starts_x:
            tile = pixels[y:y + TILE, x:x + TILE]
            result = _run("upscale", {"input": tile.transpose(2, 0, 1)[None].copy()})[0]
            result = np.clip(result.transpose(1, 2, 0), 0, 1)
            th, tw = result.shape[:2]
            output[y * 4:y * 4 + th, x * 4:x * 4 + tw] += result
            weight[y * 4:y * 4 + th, x * 4:x * 4 + tw] += 1
            done += 1
            if on_progress:
                on_progress({"stage": "running", "value": done, "max": total})
    output /= np.maximum(weight, 1)
    return _png(Image.fromarray((output * 255 + 0.5).astype(np.uint8)))


# ---------------------------------------------------------------------------
# Face restoration
# ---------------------------------------------------------------------------

def _faces(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Face boxes, largest first, with near-duplicates from the two cascades merged."""
    from . import upright

    boxes = sorted(upright.find_faces(image, min_size=max(32, min(image.size) // 25)),
                   key=lambda b: b[2] * b[3], reverse=True)
    kept: list[tuple[int, int, int, int]] = []
    for x, y, w, h in boxes:
        cx, cy = x + w / 2, y + h / 2
        if any(abs(cx - (kx + kw / 2)) < kw / 2 and abs(cy - (ky + kh / 2)) < kh / 2
               for kx, ky, kw, kh in kept):
            continue
        kept.append((x, y, w, h))
    return kept[:12]


def restore(image_bytes: bytes, on_progress=None) -> bytes:
    """GFPGAN on each face found, blended back with a soft oval edge.

    Crops are generous squares around each detected face rather than aligned
    to facial landmarks, so strongly turned faces restore less well than
    front-on ones.
    """
    image = _open(image_bytes)
    faces = _faces(image)
    if not faces:
        raise ValueError("No faces were found in this photo.")
    result = image.copy()
    for index, (x, y, w, h) in enumerate(faces, start=1):
        side = int(max(w, h) * 1.9)
        cx, cy = x + w // 2, y + h // 2 - int(h * 0.08)
        region = (cx - side // 2, cy - side // 2, cx - side // 2 + side, cy - side // 2 + side)
        crop = result.crop(region).resize((512, 512), Image.Resampling.LANCZOS)
        pixels = (np.asarray(crop, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0 - 0.5) / 0.5
        output = _run("restore", {"input": pixels.astype(np.float32)})[0]
        restored = Image.fromarray(
            ((np.clip(output.transpose(1, 2, 0), -1, 1) + 1) * 127.5 + 0.5).astype(np.uint8))
        restored = restored.resize((side, side), Image.Resampling.LANCZOS)

        oval = Image.new("L", (side, side), 0)
        inset = side // 7
        ImageDraw.Draw(oval).ellipse((inset, inset, side - inset, side - inset), fill=255)
        oval = oval.filter(ImageFilter.GaussianBlur(side // 14))
        # Paste handles regions that run off the photo's edge.
        result.paste(Image.composite(restored, result.crop(region), oval), region[:2])
        if on_progress:
            on_progress({"stage": "running", "value": index, "max": len(faces)})
    return _png(result)


# ---------------------------------------------------------------------------
# Colourising
# ---------------------------------------------------------------------------

def colorize(image_bytes: bytes, on_progress=None) -> bytes:
    """DDColor: colour for a black-and-white photograph, or fresh colour for a faded one.

    The model is shown the photograph's lightness alone, as a neutral grey
    square of ``COLORIZE_INPUT`` pixels, and answers with the colour (Lab a
    and b) it believes belongs there. That colour is scaled back up and laid
    under the photograph's *own* lightness at full size, so every edge, every
    grain and every face is the original's; only the hue is new. That is also
    why a colour photograph comes out re-coloured rather than ruined: its
    light is kept and its colour is replaced.
    """
    image = _open(image_bytes)
    width, height = image.size
    if on_progress:
        on_progress({"stage": "running", "value": 0, "max": 3})
    rgb = np.asarray(image, dtype=np.float32) / 255.0
    light = lightness(rgb)
    small = np.asarray(image.resize((COLORIZE_INPUT, COLORIZE_INPUT), Image.Resampling.LANCZOS),
                       dtype=np.float32) / 255.0
    grey = grey_of_lightness(lightness(small)).astype(np.float32)
    if on_progress:
        on_progress({"stage": "running", "value": 1, "max": 3})
    output = _run("colorize", {"input": grey.transpose(2, 0, 1)[None].copy()})[0]
    if output.shape[0] != 2:
        raise RuntimeError("The colourising model did not answer with colour (two Lab planes). "
                           "Use a DDColor ONNX export.")
    if on_progress:
        on_progress({"stage": "running", "value": 2, "max": 3})
    planes = []
    for plane in output:
        back = Image.fromarray(np.ascontiguousarray(plane, dtype=np.float32))
        planes.append(np.asarray(back.resize((width, height), Image.Resampling.BILINEAR), dtype=np.float32))
    coloured = lab_to_rgb(light, planes[0], planes[1])
    if on_progress:
        on_progress({"stage": "running", "value": 3, "max": 3})
    return _png(Image.fromarray((coloured * 255 + 0.5).astype(np.uint8)))
