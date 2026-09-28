"""Running a Playground job on the AI server.

The two entry points mirror the local ones they stand in for —
``generative_editing.generate`` and ``inpaint.remove`` — so the endpoints can
choose between them without the Playground knowing which answered. Both take and
return PNG bytes, and both apply the same request checks as the local path:
Ninaivu's prompt filter runs before anything is uploaded.
"""
from __future__ import annotations

import io
import threading
from typing import Any

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from ..media.ai_editing import check_prompt
from ..media.safe_image import open_untrusted
from ..media.generative_editing import generation_options
from . import workflows
from .comfyui import AIServerError, Client, normalise_url

#: Jobs this Ninaivu sends at once. ComfyUI queues the rest itself, but a family
#: pressing Generate repeatedly should not stack up minutes of queue.
_slots = threading.BoundedSemaphore(2)

MAX_SIDE_RANGE = (512, 2048)
#: Largest preview accepted, whatever is sent on. Object removal in the
#: Playground always posts up to 1600 px, the local tool's limit.
ACCEPT_SIDE = 1600
TIMEOUT_RANGE = (30, 900)
#: Largest image accepted back from a photo-in, photo-out job (an upscale of a
#: 2048 px preview at 4x is 8192 px).
MAX_RESULT_SIDE = 8192

#: The setting that names each job's workflow.
JOB_SETTINGS = {purpose: f"ai_server_{purpose}_workflow" for purpose in workflows.PURPOSES}


def max_side(cfg) -> int:
    return max(MAX_SIDE_RANGE[0], min(MAX_SIDE_RANGE[1], int(cfg.ai_server_max_side or 1024)))


def timeout(cfg) -> int:
    return max(TIMEOUT_RANGE[0], min(TIMEOUT_RANGE[1], int(cfg.ai_server_timeout or 180)))


def assigned(cfg, purpose: str) -> dict[str, Any] | None:
    """The workflow serving *purpose*, if the AI server is on and one is set."""
    if not cfg.ai_server_enabled or not cfg.ai_server_url:
        return None
    workflow_id = getattr(cfg, JOB_SETTINGS[purpose], "") if purpose in JOB_SETTINGS else ""
    entry = workflows.load(cfg, workflow_id) if workflow_id else None
    if entry is None or entry.get("purpose") != purpose:
        return None
    return entry


def active_jobs(cfg) -> list[str]:
    """The jobs that would run on the AI server right now."""
    return [purpose for purpose in workflows.PURPOSES if assigned(cfg, purpose) is not None]


def edit(cfg, entry: dict[str, Any], prompt: Any, image_bytes: bytes,
         options: Any = None, on_progress=None) -> bytes:
    prompt = check_prompt(prompt)
    settings = generation_options(options)      # validates, as the local path does
    image = _open_preview(image_bytes, max(ACCEPT_SIDE, max_side(cfg)))
    values = {"prompt": prompt, "negative_prompt": settings["negative_prompt"],
              "seed": settings["seed"]}
    result = _run(cfg, entry, _fit(image, max_side(cfg)), None, values, on_progress)
    return _finish(result, image.size,
                   f"ai-server workflow={entry['id']}; seed={settings['seed']}")


def enhance(cfg, entry: dict[str, Any], image_bytes: bytes, on_progress=None) -> bytes:
    """Upscale, restore or colourise: the photograph in, a photograph out."""
    image = _open_preview(image_bytes, max(ACCEPT_SIDE, max_side(cfg)))
    result = _run(cfg, entry, _fit(image, max_side(cfg)), None,
                  {"prompt": "", "negative_prompt": "", "seed": 0}, on_progress)
    return _finish(result, None, f"ai-server workflow={entry['id']}")


def remove(cfg, entry: dict[str, Any], image_bytes: bytes, mask_bytes: bytes,
           on_progress=None) -> bytes:
    image = _open_preview(image_bytes, max(ACCEPT_SIDE, max_side(cfg)))
    working = _fit(image, max_side(cfg))
    side = max(ACCEPT_SIDE, max_side(cfg))
    with open_untrusted(mask_bytes, side * side) as source:
        painted = source.convert("L").resize(working.size, Image.Resampling.NEAREST)
    # White is the area to replace; a soft brush edge counts fully, as locally.
    mask = painted.point(lambda value: 255 if value > 24 else 0)
    if not mask.getbbox():
        raise ValueError("Paint over the object to remove before applying.")
    result = _run(cfg, entry, working, mask, {"prompt": "", "negative_prompt": "", "seed": 0},
                  on_progress)
    return _finish(result, image.size, f"ai-server workflow={entry['id']}")


def _open_preview(data: bytes, side: int) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        if source.format != "PNG" or max(source.size) > side:
            raise ValueError(f"Use a PNG preview up to {side} pixels on a side.")
        return source.convert("RGB")


def _fit(image: Image.Image, side: int) -> Image.Image:
    """A copy no longer than *side* on its long edge, for sending to the server."""
    if max(image.size) <= side:
        return image
    smaller = image.copy()
    smaller.thumbnail((side, side), Image.Resampling.LANCZOS)
    return smaller


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _run(cfg, entry: dict[str, Any], image: Image.Image, mask: Image.Image | None,
         values: dict[str, Any], on_progress=None) -> bytes:
    if not _slots.acquire(blocking=False):
        raise AIServerError("The AI server is already working on two edits from this Ninaivu. "
                            "Try again when one finishes.")
    try:
        client = Client(normalise_url(cfg.ai_server_url))
        values = dict(values, image=client.upload(_png(image)))
        if mask is not None:
            values["mask"] = client.upload(_png(mask))
        return client.run(workflows.fill(entry["workflow"], values), deadline=timeout(cfg),
                          on_progress=on_progress)
    finally:
        _slots.release()


def _finish(result: bytes, size: tuple[int, int] | None, note: str) -> bytes:
    """Check the server really sent an image; hand it back at *size*, if given."""
    try:
        with Image.open(io.BytesIO(result)) as produced:
            produced.load()
            image = produced.convert("RGB")
    except (OSError, ValueError) as error:
        raise AIServerError("The AI server sent back something that is not an image.") from error
    if size is None:
        if max(image.size) > MAX_RESULT_SIDE:
            image.thumbnail((MAX_RESULT_SIDE, MAX_RESULT_SIDE), Image.Resampling.LANCZOS)
    elif image.size != size:
        # Many models snap to their own resolution grid; the Playground expects
        # the preview it sent, at the same size, to compare and composite.
        image = image.resize(size, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    metadata = PngInfo()
    metadata.add_text("Ninaivu generation", note)
    image.save(output, "PNG", pnginfo=metadata)
    return output.getvalue()
