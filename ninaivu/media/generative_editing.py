"""Opt-in local image-to-image inference; no model download during a request.

Compatible with any diffusers-format checkpoint for StableDiffusionInstructPix2PixPipeline —
the original timbrooks/instruct-pix2pix, or the MagicBrush fine-tune of it (recommended; see
tools/setup_ai_models.py and docs/local-ai-editing.md). Same architecture and speed, trained
on human-annotated precise edits instead of the original's synthetic ones, so it follows
instructions more accurately. It is not a stronger model for style transfer or edits over a
large region of the photo — that remains a known limitation of this model family on CPU.
"""
from __future__ import annotations

import io
import os
import threading
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from .ai_editing import check_prompt, local_setting

_slot = threading.BoundedSemaphore(1)

#: Bounds for the two knobs worth tuning per machine: bigger/more accurate vs. faster.
#: CPU time scales roughly with side^2 * steps, so both matter for how long a request takes.
_MAX_SIDE_RANGE = (256, 768)
_STEPS_RANGE = (4, 30)


def _bounded_int(name, default, low, high):
    configured = os.environ.get(f'NINAIVU_IMAGE_EDIT_{name}', local_setting(f'image_edit_{name.lower()}'))
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def max_side():
    return _bounded_int('MAX_SIDE', 512, *_MAX_SIDE_RANGE)


def steps():
    return _bounded_int('STEPS', 12, *_STEPS_RANGE)


def generation_options(options=None):
    """Validate bounded per-request controls before loading expensive model weights."""
    if options is None:
        options = {}
    if not isinstance(options, dict) or set(options) - {'quality', 'fidelity', 'seed', 'negative_prompt'}:
        raise ValueError('Invalid generation options.')
    quality = options.get('quality', 'balanced')
    fidelity = options.get('fidelity', 'preserve')
    if quality not in ('draft', 'balanced', 'detailed') or fidelity not in ('preserve', 'balanced', 'creative'):
        raise ValueError('Choose a supported quality and fidelity preset.')
    seed = options.get('seed', 42)
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError('Seed must be a whole number between 0 and 2147483647.')
    negative = options.get('negative_prompt', '')
    if not isinstance(negative, str) or len(negative) > 500:
        raise ValueError('The avoid prompt must be at most 500 characters.')
    count = {'draft': max(4, steps() // 2), 'balanced': steps(), 'detailed': min(30, max(20, steps()))}[quality]
    return dict(num_inference_steps=count, image_guidance_scale={'preserve': 2.0, 'balanced': 1.5, 'creative': 1.1}[fidelity],
                guidance_scale=7.0, negative_prompt=negative.strip(), seed=seed)


def generate(prompt, image_bytes, options=None):
    prompt = check_prompt(prompt)
    settings = generation_options(options)
    configured = os.environ.get('NINAIVU_IMAGE_EDIT_MODEL', local_setting('image_model'))
    model_path = Path(configured)
    if not configured or not (model_path / 'model_index.json').is_file():
        raise RuntimeError('Generative AI is not installed. Configure a local InstructPix2Pix-compatible '
                            'model folder; no photo was sent to an external service.')
    side = max_side()
    with Image.open(io.BytesIO(image_bytes)) as source:
        # A generous cap on the *received* preview; it is then downsized to `side` regardless.
        if source.format != 'PNG' or source.width * source.height > max(1024 * 1024, 2 * side * side):
            raise ValueError(f'Use a PNG preview up to {side} pixels on a side.')
        image = source.convert('RGB')
    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    original_size = image.size
    # Pad instead of stretching the photo to the model's multiple-of-eight grid.
    padded = Image.new('RGB', (((image.width + 7) // 8) * 8, ((image.height + 7) // 8) * 8))
    padded.paste(image)
    if padded.width > image.width:
        padded.paste(image.crop((image.width - 1, 0, image.width, image.height)).resize((padded.width - image.width, image.height)), (image.width, 0))
    if padded.height > image.height:
        padded.paste(padded.crop((0, image.height - 1, padded.width, image.height)).resize((padded.width, padded.height - image.height)), (0, image.height))
    image = padded
    if not _slot.acquire(blocking=False):
        raise RuntimeError('Another generative edit is running. Try again when it finishes.')
    try:
        try:
            import torch
            from diffusers import StableDiffusionInstructPix2PixPipeline, EulerAncestralDiscreteScheduler
        except ImportError as error:
            raise RuntimeError('Install the optional requirements-ai-editing.txt dependencies to use generative editing.') from error
        # Local-only loading, including the bundled safety checker. Do not disable it.
        # use_safetensors=True is deliberate: this pipeline never unpickles a .bin/.ckpt
        # checkpoint, which can execute arbitrary code on load. Point image_model at a
        # folder that only has .safetensors weights (tools/setup_ai_models.py does this).
        from ..utils.resources import budget
        torch.set_num_threads(budget()['compute_threads'])
        # The graphics card when it can really run a kernel, else the CPU. Half
        # precision only on the card: it halves a 5.5 GB pipeline into the memory
        # an integrated GPU actually has, while CPU float16 is slower than float32.
        from ..ai import usable_device
        device = usable_device(torch)
        dtype = torch.float16 if device == 'cuda' else torch.float32
        pipeline = StableDiffusionInstructPix2PixPipeline.from_pretrained(
            str(model_path.resolve()), local_files_only=True, use_safetensors=True,
            dtype=dtype)  # `torch_dtype` is deprecated from diffusers 0.40
        if pipeline.safety_checker is None or pipeline.feature_extractor is None:
            raise RuntimeError('This local model is missing its image safety checker. Install the complete model before enabling generation.')
        pipeline.scheduler = EulerAncestralDiscreteScheduler.from_config(pipeline.scheduler.config)
        pipeline.enable_attention_slicing()
        pipeline.to(device)
        pixels = pipeline.image_processor.pil_to_numpy(image)
        _, unsafe = pipeline.run_safety_checker(pixels, torch.device(device), dtype)
        if unsafe is None or any(unsafe):
            raise ValueError('This image cannot be used for generative editing. Ordinary photo adjustments remain available.')
        with torch.inference_mode():
            seed = settings.pop('seed')
            result = pipeline(prompt=prompt, image=image, **settings,
                              generator=torch.Generator('cpu').manual_seed(seed))
        if result.nsfw_content_detected is None or any(result.nsfw_content_detected):
            raise ValueError('The generated result was blocked by the local image safety checker. Try another photography request.')
        output = io.BytesIO()
        metadata = PngInfo()
        metadata.add_text('Ninaivu generation', f"seed={seed}; steps={settings['num_inference_steps']}; image_guidance={settings['image_guidance_scale']}")
        result.images[0].crop((0, 0, *original_size)).save(output, 'PNG', pnginfo=metadata)
        return output.getvalue()
    finally:
        _slot.release()
