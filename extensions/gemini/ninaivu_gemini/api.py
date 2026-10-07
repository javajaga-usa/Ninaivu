"""The routes the Gemini extension adds.

Three under the Sudar playground (an edit, a description, a plan) and two on
the console for the API key. All family-only or admin-only, all with the same
size limits the core's own playground routes have. The Sudar page calls the
playground ones by these paths, so they keep them.
"""
from __future__ import annotations

import json

from flask import Blueprint, Response, abort, jsonify, request

from ninaivu.api._body import json_object
from ninaivu.server import auth
from ninaivu.server.auth import current_user, require_admin, require_outside_ai

from . import gemini

gemini_bp = Blueprint("ninaivu_gemini", __name__)
gemini_admin_bp = Blueprint("ninaivu_gemini_admin", __name__)


@gemini_bp.post('/api/ai-playground/gemini/generate')
@require_outside_ai
def gemini_generate_image():
    import base64
    import binascii
    if request.content_length and request.content_length > 6_000_000:
        abort(413)
    raw = request.stream.read(6_000_001)
    if len(raw) > 6_000_000:
        abort(413)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not {'prompt', 'image'} <= set(data) or not isinstance(data['image'], str):
            raise ValueError('Invalid generative editing request.')
        image = base64.b64decode(data['image'], validate=True)
        result = gemini.generate_image_edit(data['prompt'], image, data.get('options'))
        return Response(result, mimetype='image/png', headers={'Cache-Control': 'no-store'})
    except (ValueError, TypeError, binascii.Error) as error:
        return jsonify(error=str(error)), 400
    except RuntimeError as error:
        return jsonify(error=str(error)), 503


@gemini_bp.post('/api/ai-playground/gemini/analyze')
@require_outside_ai
def gemini_analyze_photo():
    import base64
    import binascii
    from ninaivu.api.api import _asset_file, _conn, _guard
    from ninaivu.storage import db
    if request.content_length and request.content_length > 10_000_000:
        abort(413)
    raw = request.stream.read(10_000_001)
    if len(raw) > 10_000_000:
        abort(413)
    try:
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise ValueError('Invalid request.')
        image_bytes = None
        if data.get('image'):
            image_bytes = base64.b64decode(data['image'], validate=True)
        elif data.get('media_id'):
            asset_id = int(data['media_id'])
            row = _guard(db.get_asset(_conn(), asset_id))
            # A photograph, and one of a size a photograph is. Read whole into
            # memory, a video id could hold gigabytes there for one request.
            if (row.get('kind') or '') != 'picture':
                raise ValueError('Gemini describes photographs only.')
            # Hidden is where documents live — a passport, a statement, a
            # medical letter — and no AI reads them, least of all one outside
            # the house (db.AI_MAY_READ). An administrator who wants one
            # described can show it first.
            if int(row.get('visibility') or 0) >= 2:
                raise ValueError('Hidden items are never sent to Gemini. Show the photograph '
                                 'first if it should be described.')
            # Resolved the way /api/file resolves it: never outside its
            # library folder. What goes to Google is a 1024-pixel JPEG made
            # from the pixels alone (gemini.analyze_image), so no EXIF, and
            # with it no place, leaves with it, whoever is asking.
            file_path = _asset_file(row)
            if file_path.stat().st_size > 200_000_000:
                raise ValueError('That photograph is too large to send.')
            image_bytes = file_path.read_bytes()
        else:
            raise ValueError('Provide either image data or media_id.')
        analysis = gemini.analyze_image(image_bytes, data.get('options'))
        return jsonify(analysis)
    except (ValueError, TypeError, binascii.Error) as error:
        return jsonify(error=str(error)), 400
    except RuntimeError as error:
        return jsonify(error=str(error)), 503


@gemini_bp.post('/api/ai-playground/gemini/plan')
@require_outside_ai
def gemini_plan_edits():
    import base64
    import binascii
    if request.content_length and request.content_length > 12000:
        abort(413)
    try:
        raw = request.stream.read(12001)
        if len(raw) > 12000:
            abort(413)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) - {'prompt', 'current', 'image'}:
            raise ValueError('Invalid editing request.')
        img_bytes = None
        if payload.get('image'):
            img_bytes = base64.b64decode(payload['image'], validate=True)
        result = gemini.plan_adjustments(payload.get('prompt', ''), payload.get('current', {}), img_bytes)
        return jsonify(result)
    except (ValueError, TypeError, binascii.Error) as error:
        return jsonify(error=str(error)), 400
    except RuntimeError as error:
        return jsonify(error=str(error)), 503


# ---------------------------------------------------------------------------
# The console: the API key
# ---------------------------------------------------------------------------
#
# Nothing here ever sends the key back — not to the page, not into the audit
# log — only whether there is one, where it came from, and its last four
# characters.

@gemini_admin_bp.get("/api/admin/gemini")
@require_admin
def gemini_status():
    return jsonify(gemini.key_status())


@gemini_admin_bp.post("/api/admin/gemini")
@require_admin
def gemini_key():
    from ninaivu.api.admin_api import _conn
    data = json_object()
    key = data.get("key")
    if not isinstance(key, str):
        return jsonify(error="Send the key as text, or an empty one to remove it."), 400

    if gemini.key_status()["source"] == "environment":
        # Honest rather than silently ignored: the environment wins, so a key
        # saved here would be written down and never used.
        return jsonify(error="A key is already set in this server's environment, "
                             "which takes precedence. Change it there."), 409

    key = key.strip()
    if key:
        good, why = gemini.check_api_key(key)
        if not good:
            return jsonify(error=why), 400
    try:
        gemini.save_api_key(key)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    auth.audit(_conn(), current_user().id, "settings",
               "set the Gemini key" if key else "removed the Gemini key")
    return jsonify(gemini.key_status())
