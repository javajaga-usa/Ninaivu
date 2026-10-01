"""The AI Playground's endpoints: planning, generating, segmenting, inpainting,
and background jobs.

Split out of ``api.py``. They register on the same blueprint, so both faces
serve them exactly as before; ``api.py`` imports this module at its end.

The heavy end — generative edits with a diffusion model, and every job on a
ComfyUI server — is the creative-studio extension's (``extensions.studio()``).
Without it these routes still do what the core can on a small machine: plan
with the local language model, cut out a subject, remove an object with the
small models under AI models, upscale and restore with the same.
"""
from __future__ import annotations

import json
from flask import Response, abort, jsonify, request

from ..server.auth import current_user, may_send_photos_out, require_family
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
            if not may_send_photos_out(current_user(), _cfg()):
                return jsonify(error='Sending photographs to an outside service is kept to the administrator.'), 403
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
    from ..media.ai_editing import model_name
    from ..media import inpaint, segmentation
    from .. import extensions
    try:
        language = model_name()
    except ValueError:
        language = ''
    cfg = _cfg()
    # The heavy end, when the creative-studio extension is on; nothing
    # otherwise, and the core answers with what runs on this machine.
    studio = extensions.studio()
    heavy = studio.capabilities(cfg) if studio is not None else {}
    server_remove = heavy.get('object_removal_provider') == 'ai-server'
    server_jobs = list(heavy.get('server_jobs') or [])
    # Extensions that can take an edit (Gemini, say): each says whether it is
    # ready and what it is called. None of them is ever the default provider —
    # a request has to name one — so the gallery can say truthfully that a
    # preview stays on this machine unless the person picks otherwise.
    # Only offered to somebody who may use them (auth.may_send_photos_out).
    allowed = may_send_photos_out(current_user(), cfg)
    outside = {name: p.capabilities() for name, p in extensions.image_providers().items()
               if allowed and p.is_available()}
    gemini_caps = outside.get('gemini') or {}
    gemini_on = bool(gemini_caps.get('gemini_enabled'))
    image_side = heavy.get('image_edit_max_side')
    from ..media import onnx_tools
    local_jobs = [kind for kind in onnx_tools.ENHANCE_TOOLS
                  if kind not in server_jobs and onnx_tools.available(kind)]
    # Every Enhance tool, set up or not, with where it would run and what it
    # needs when it is not: the Playground shows the card either way, so a
    # family that has not downloaded a model sees the tool and who to ask
    # rather than nothing at all.
    enhance_tools = {}
    for kind in onnx_tools.ENHANCE_TOOLS:
        if kind in server_jobs:
            enhance_tools[kind] = {'provider': 'ai-server', 'ready': True}
        elif kind in local_jobs:
            enhance_tools[kind] = {'provider': 'local', 'ready': True}
        else:
            enhance_tools[kind] = {'provider': None, 'ready': False, 'model': onnx_tools.label(kind),
                                   'by_hand': kind == 'colorize'}
    lama = onnx_tools.available('lama')
    image_provider = heavy.get('image_provider')
    return jsonify(
        language_model=language,
        image_model=bool(heavy.get('image_model')) or gemini_on,
        image_edit_max_side=image_side,
        # Where a generative edit or object removal will run, so the Playground
        # can say truthfully where the preview is going.
        image_provider=image_provider,
        object_removal_provider='ai-server' if server_remove else ('local-ai' if lama else 'local'),
        # Every job that would run on the AI server now. The Playground runs
        # these as background jobs, so it can show the queue and progress.
        server_jobs=server_jobs,
        # Enhance tools that run on this machine with a downloaded model, for
        # the jobs no AI server workflow has taken.
        local_jobs=local_jobs,
        enhance_tools=enhance_tools,
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
            if not may_send_photos_out(current_user(), _cfg()):
                return jsonify(error='Sending photographs to an outside service is kept to the administrator.'), 403
            result = outside.generate_image_edit(data['prompt'], image, data.get('options'))
            return Response(result, mimetype='image/png', headers={'Cache-Control': 'no-store'})
        studio = extensions.studio()
        if studio is None:
            return jsonify(error='Generative edits need the Creative Studio extension, which is '
                                 'not switched on. Light, colour, crops and looks work without it.'), 503
        result = studio.edit(_cfg(), data['prompt'], image, data.get('options'))
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
    from .. import extensions
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
        image = base64.b64decode(data['image'], validate=True)
        mask = base64.b64decode(data['mask'], validate=True)
        studio = extensions.studio()
        result = studio.remove(_cfg(), image, mask) if studio is not None else None
        if result is None:
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
    from .. import extensions
    from ..media import jobs
    from ..media.jobs import JobError
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
        image = base64.b64decode(data['image'], validate=True)
        if kind in ('remove', 'inpaint'):
            if not isinstance(data.get('mask'), str):
                raise ValueError('Paint over the area first.' if kind == 'inpaint'
                                 else 'Paint over the object to remove before applying.')
            if kind == 'inpaint' and not isinstance(data.get('prompt'), str):
                raise ValueError('Say what should be drawn in the painted area.')
            data = {**data, 'mask': base64.b64decode(data['mask'], validate=True)}
        elif kind != 'edit' and set(data) - {'kind', 'image'}:
            raise ValueError('This tool takes only the photo.')
        # The AI server first, when the creative-studio extension is on and a
        # workflow is assigned to this job; then the small models here.
        studio = extensions.studio()
        work = studio.job(cfg, kind, image, data) if studio is not None else None
        if work is None and kind in onnx_tools.ENHANCE_TOOLS and onnx_tools.available(kind):
            tool = {'upscale': onnx_tools.upscale, 'restore': onnx_tools.restore,
                    'colorize': onnx_tools.colorize}[kind]
            work = lambda report: tool(image, report)  # noqa: E731
        if work is None:
            return jsonify(error='That tool is not set up on the AI server or this machine.'), 404
        job_id = jobs.start(current_user().id, kind, work)
    except (ValueError, TypeError, binascii.Error) as error:
        return jsonify(error=str(error)), 400
    except JobError as error:
        return jsonify(error=str(error)), 503
    return jsonify(id=job_id, status=jobs.status(job_id, current_user().id)), 202


@bp.get('/api/ai-playground/server-jobs/<job_id>')
@require_family
def server_job_status(job_id: str):
    from ..media import jobs
    state = jobs.status(job_id, current_user().id)
    if state is None:
        abort(404)
    return jsonify(state)


@bp.get('/api/ai-playground/server-jobs/<job_id>/result')
@require_family
def server_job_result(job_id: str):
    from ..media import jobs
    result = jobs.take_result(job_id, current_user().id)
    if result is None:
        abort(404)
    return Response(result, mimetype='image/png', headers={'Cache-Control': 'no-store'})


