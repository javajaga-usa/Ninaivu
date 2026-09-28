"""The AI Playground's endpoints: planning, generating, segmenting, inpainting,
and AI server jobs.

Split out of ``api.py``. They register on the same blueprint, so both faces
serve them exactly as before; ``api.py`` imports this module at its end.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Response, abort, jsonify, request

from ..server.auth import current_user, require_family
from .api import _cfg, bp


@bp.post('/api/ai-playground/plan')
@require_family
def plan_photo_edit():
    import base64
    import binascii
    from ..media.ai_editing import plan
    from .. import extensions
    if request.content_length and request.content_length > 12000:
        abort(413)
    try:
        raw = request.stream.read(12001)
        if len(raw) > 12000:
            abort(413)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) - {'prompt', 'current', 'provider', 'image'}:
            raise ValueError('Invalid editing request.')
        provider = payload.get('provider')
        if provider and provider not in ('builtin', 'local'):
            # An extension's planner, and only one that is installed and on.
            outside = extensions.image_provider(provider)
            if outside is None:
                return jsonify(error=f'{provider} is not switched on.'), 404
            img_bytes = None
            if payload.get('image'):
                img_bytes = base64.b64decode(payload['image'], validate=True)
            return jsonify(outside.plan_adjustments(payload.get('prompt', ''), payload.get('current', {}), img_bytes))
        return jsonify(plan(payload.get('prompt'), payload.get('current', {})))
    except (ValueError, TypeError, binascii.Error) as error:
        return jsonify(error=str(error)), 400
    except RuntimeError as error:
        return jsonify(error=str(error)), 503


@bp.get('/api/ai-playground/capabilities')
@require_family
def photo_edit_capabilities():
    from ..media.ai_editing import model_name, local_setting
    from ..media import generative_editing, inpaint, segmentation
    from .. import extensions
    try:
        language = model_name()
    except ValueError:
        language = ''
    from ..ai_server import service as ai_server
    folder = os.environ.get('NINAIVU_IMAGE_EDIT_MODEL', local_setting('image_model'))
    local_image_model = bool(folder and (Path(folder) / 'model_index.json').is_file())
    cfg = _cfg()
    server_edit = ai_server.assigned(cfg, 'edit') is not None
    server_remove = ai_server.assigned(cfg, 'remove') is not None
    # Extensions that can take an edit (Gemini, say): each says whether it is
    # ready and what it is called. None of them is ever the default provider —
    # a request has to name one — so the gallery can say truthfully that a
    # preview stays on this machine unless the person picks otherwise.
    outside = {name: p.capabilities() for name, p in extensions.image_providers().items()
               if p.is_available()}
    gemini_caps = outside.get('gemini') or {}
    gemini_on = bool(gemini_caps.get('gemini_enabled'))
    if server_edit:
        image_side = ai_server.max_side(cfg)
    elif local_image_model:
        image_side = generative_editing.max_side()
    else:
        image_side = None
    from ..media import onnx_tools
    local_jobs = [kind for kind in ('upscale', 'restore')
                  if kind not in ai_server.active_jobs(cfg) and onnx_tools.available(kind)]
    lama = onnx_tools.available('lama')
    image_provider = 'ai-server' if server_edit else ('local' if local_image_model else None)
    return jsonify(
        language_model=language,
        image_model=server_edit or local_image_model or gemini_on,
        image_edit_max_side=image_side,
        # Where a generative edit or object removal will run, so the Playground
        # can say truthfully where the preview is going.
        image_provider=image_provider,
        object_removal_provider='ai-server' if server_remove else ('local-ai' if lama else 'local'),
        # Every job that would run on the AI server now. The Playground runs
        # these as background jobs, so it can show the queue and progress.
        server_jobs=ai_server.active_jobs(cfg),
        # Enhance tools that run on this machine with a downloaded model, for
        # the jobs no AI server workflow has taken.
        local_jobs=local_jobs,
        segmentation_model=segmentation.available(),
        object_removal=server_remove or lama or inpaint.available(),
        # What the switched-on extensions offer, by name.
        providers=outside,
        gemini_enabled=gemini_on,
        gemini_image_model=gemini_caps.get('gemini_image_model', ''),
        gemini_vision_model=gemini_caps.get('gemini_vision_model', ''),
    )


@bp.post('/api/ai-playground/generate')
@require_family
def generate_photo_edit():
    import base64
    import binascii
    from ..media.generative_editing import generate
    from .. import extensions
    if request.content_length and request.content_length > 6_000_000:
        abort(413)
    raw = request.stream.read(6_000_001)
    if len(raw) > 6_000_000:
        abort(413)
    try:
        data = json.loads(raw)
        allowed_keys = {'prompt', 'image', 'options', 'provider'}
        if not isinstance(data, dict) or not {'prompt', 'image'} <= set(data) or set(data) - allowed_keys or not isinstance(data['image'], str):
            raise ValueError('Invalid generative editing request.')
        from ..ai_server import service as ai_server
        image = base64.b64decode(data['image'], validate=True)
        provider = data.get('provider')
        if provider and provider not in ('local', 'ai-server'):
            # Only an extension that is installed and on, and only when the
            # request names it. A request that names nothing never leaves
            # this machine — it used to fall through to Gemini whenever no
            # local model was installed, which sent the photograph to Google
            # without the person having chosen that.
            outside = extensions.image_provider(provider)
            if outside is None:
                return jsonify(error=f'{provider} is not switched on.'), 404
            result = outside.generate_image_edit(data['prompt'], image, data.get('options'))
            return Response(result, mimetype='image/png', headers={'Cache-Control': 'no-store'})
        server = ai_server.assigned(_cfg(), 'edit')
        if server is not None:
            result = ai_server.edit(_cfg(), server, data['prompt'], image, data.get('options'))
        else:
            # On this machine, with the local model — which says so itself
            # when none is installed. Nothing here goes anywhere else.
            result = generate(data['prompt'], image, data.get('options'))
        return Response(result, mimetype='image/png', headers={'Cache-Control': 'no-store'})
    except (ValueError, TypeError, binascii.Error, OSError) as error:
        return jsonify(error=str(error)), 400
    except RuntimeError as error:
        return jsonify(error=str(error)), 503


@bp.post('/api/ai-playground/segment')
@require_family
def segment_photo():
    """Return a foreground mask for the posted photo, for background removal/blur."""
    import base64
    import binascii
    from ..media.segmentation import mask as segmentation_mask
    if request.content_length and request.content_length > 6_000_000:
        abort(413)
    raw = request.stream.read(6_000_001)
    if len(raw) > 6_000_000:
        abort(413)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {'image'} or not isinstance(data['image'], str):
            raise ValueError('Invalid segmentation request.')
        result = segmentation_mask(base64.b64decode(data['image'], validate=True))
        return Response(result, mimetype='image/png', headers={'Cache-Control': 'no-store'})
    except (ValueError, TypeError, binascii.Error, OSError) as error:
        return jsonify(error=str(error)), 400
    except RuntimeError as error:
        return jsonify(error=str(error)), 503


@bp.post('/api/ai-playground/inpaint')
@require_family
def inpaint_photo():
    """Remove a family-painted selection from the posted photo via local inpainting."""
    import base64
    import binascii
    from ..media.inpaint import remove as remove_object
    if request.content_length and request.content_length > 10_000_000:
        abort(413)
    raw = request.stream.read(10_000_001)
    if len(raw) > 10_000_000:
        abort(413)
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {'image', 'mask'}
                or not isinstance(data['image'], str) or not isinstance(data['mask'], str)):
            raise ValueError('Invalid object-removal request.')
        from ..ai_server import service as ai_server
        image = base64.b64decode(data['image'], validate=True)
        mask = base64.b64decode(data['mask'], validate=True)
        server = ai_server.assigned(_cfg(), 'remove')
        if server is not None:
            result = ai_server.remove(_cfg(), server, image, mask)
        else:
            from ..media import onnx_tools
            if onnx_tools.available('lama'):
                result = onnx_tools.remove(image, mask)
            else:
                result = remove_object(image, mask)
        return Response(result, mimetype='image/png', headers={'Cache-Control': 'no-store'})
    except (ValueError, TypeError, binascii.Error, OSError) as error:
        return jsonify(error=str(error)), 400
    except RuntimeError as error:
        return jsonify(error=str(error)), 503

@bp.post('/api/ai-playground/server-jobs')
@require_family
def start_server_job():
    """Start an AI server job in the background; poll it, then fetch the result.

    ``kind`` is a job the administrator has assigned a workflow to. Checked and
    decoded here, before the job starts, so a bad request is a 400 at once
    rather than a failed job a moment later.
    """
    import base64
    import binascii
    from ..ai_server import jobs, service as ai_server
    from ..ai_server.comfyui import AIServerError
    from ..media.ai_editing import check_prompt
    from ..media.generative_editing import generation_options
    if request.content_length and request.content_length > 10_000_000:
        abort(413)
    raw = request.stream.read(10_000_001)
    if len(raw) > 10_000_000:
        abort(413)
    cfg = _cfg()
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or not isinstance(data.get('kind'), str)
                or set(data) - {'kind', 'image', 'mask', 'prompt', 'options'}
                or not isinstance(data.get('image'), str)):
            raise ValueError('Invalid AI server request.')
        kind = data['kind']
        from ..media import onnx_tools
        entry = ai_server.assigned(cfg, kind)
        local = entry is None and kind in ('upscale', 'restore') and onnx_tools.available(kind)
        if entry is None and not local:
            return jsonify(error='That tool is not set up on the AI server or this machine.'), 404
        image = base64.b64decode(data['image'], validate=True)
        if kind == 'edit':
            prompt = check_prompt(data.get('prompt'))
            options = data.get('options')
            generation_options(options)
            work = lambda report: ai_server.edit(cfg, entry, prompt, image, options, report)  # noqa: E731
        elif kind == 'remove':
            if not isinstance(data.get('mask'), str):
                raise ValueError('Paint over the object to remove before applying.')
            mask = base64.b64decode(data['mask'], validate=True)
            work = lambda report: ai_server.remove(cfg, entry, image, mask, report)  # noqa: E731
        else:
            if set(data) - {'kind', 'image'}:
                raise ValueError('This tool takes only the photo.')
            if local:
                tool = onnx_tools.upscale if kind == 'upscale' else onnx_tools.restore
                work = lambda report: tool(image, report)  # noqa: E731
            else:
                work = lambda report: ai_server.enhance(cfg, entry, image, report)  # noqa: E731
        job_id = jobs.start(current_user().id, kind, work)
    except (ValueError, TypeError, binascii.Error) as error:
        return jsonify(error=str(error)), 400
    except AIServerError as error:
        return jsonify(error=str(error)), 503
    return jsonify(id=job_id, status=jobs.status(job_id, current_user().id)), 202


@bp.get('/api/ai-playground/server-jobs/<job_id>')
@require_family
def server_job_status(job_id: str):
    from ..ai_server import jobs
    state = jobs.status(job_id, current_user().id)
    if state is None:
        abort(404)
    return jsonify(state)


@bp.get('/api/ai-playground/server-jobs/<job_id>/result')
@require_family
def server_job_result(job_id: str):
    from ..ai_server import jobs
    result = jobs.take_result(job_id, current_user().id)
    if result is None:
        abort(404)
    return Response(result, mimetype='image/png', headers={'Cache-Control': 'no-store'})


