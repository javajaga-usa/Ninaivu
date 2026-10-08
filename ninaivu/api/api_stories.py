"""Voice stories: recorded about a photograph, played back with it.

The storage, and why a story has no visibility of its own, are in
ninaivu/storage/stories.py. Here the one rule is that every route reaches a
story through its item and puts that item through ``_guard`` — the check the
original file, the thumbnail and the faces already go through — before saying
anything about it. Somebody who may not open the photograph gets the same 404
for its stories as for a story that does not exist, so not even the fact that
one was told leaks out: not for an administrator-only item, not for one in a
folder outside a family member's, not for one in the bin.

Listening is for whoever may open the item, guests included, as they may look
at it. Telling one is for family members and administrators; taking one away
for whoever told it, or an administrator.

A share link carries its items' stories too, read-only, under the link's own
token-bearing addresses: the person it was sent to has no account here, and
the voice is a large part of why the photograph was sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import abort, jsonify, request, send_file

from ..server.auth import current_user, require_family
from ..storage import db, stories
from .api import _cfg, _conn, _guard, bp
from .api_share import _share_asset_or_404


def _folder() -> Path:
    return stories.folder_for(_cfg().state_dir)


def _public(story: dict[str, Any], src: str, *, mine: bool = False,
            may_delete: bool = False) -> dict[str, Any]:
    """What the page is told about one story. Not who made it, by id: the
    speaker is the name it was told under, which is what the household reads."""
    return {
        "id": story["id"],
        "speaker": story.get("speaker") or "",
        "text": story.get("text") or "",
        "duration": story.get("duration"),
        "mime": story.get("mime"),
        "created_at": story.get("created_at"),
        "src": src,
        "mine": mine,
        "can_delete": may_delete,
    }


def _mine(story: dict[str, Any]) -> bool:
    user = current_user()
    return bool(user.id) and story.get("created_by") == user.id


def _for_viewer(story: dict[str, Any]) -> dict[str, Any]:
    user = current_user()
    mine = _mine(story)
    return _public(story, f"/api/stories/{story['id']}/audio", mine=mine,
                   may_delete=not user.is_guest and (mine or user.is_admin))


def _story_or_404(story_id: int) -> dict[str, Any]:
    """The story, if this viewer may open the item it is about. 404 otherwise,
    whichever of the two is missing."""
    conn = _conn()
    story = stories.get(conn, story_id)
    if not story:
        abort(404)
    _guard(db.get_asset(conn, int(story["asset_id"]), current_user().id))
    return story


def _send(story: dict[str, Any]):
    """The recording itself, ranged so a phone can seek in it and start it
    before it has all arrived."""
    name = str(story.get("file") or "")
    if not stories._FILE_NAME.match(name):                      # noqa: SLF001
        abort(404)
    path = _folder() / name
    if not path.is_file():
        abort(404)
    mime = story.get("mime") if story.get("mime") in stories.AUDIO_TYPES else None
    if mime is None:
        abort(404)
    response = send_file(path, conditional=True, mimetype=mime, max_age=3600)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Vary"] = "Cookie"
    return response


@bp.get("/api/asset/<int:asset_id>/stories")
def asset_stories(asset_id: int):
    """The stories told about one item, oldest first."""
    conn = _conn()
    _guard(db.get_asset(conn, asset_id, current_user().id))
    stories.sweep(conn, _folder())
    return jsonify({
        "stories": [_for_viewer(s) for s in stories.for_asset(conn, asset_id)],
        "can_add": not current_user().is_guest,
        "max_bytes": stories.MAX_BYTES,
    })


@bp.post("/api/asset/<int:asset_id>/stories")
@require_family
def add_story(asset_id: int):
    """Keep one recording about an item: multipart, the sound as ``audio``,
    with ``text``, ``speaker`` and ``duration`` beside it, each optional."""
    conn = _conn()
    _guard(db.get_asset(conn, asset_id, current_user().id))
    # Refused before the body is read where the browser said how big it is.
    if (request.content_length or 0) > stories.MAX_BYTES + 256 * 1024:
        abort(413, description="A story can be at most 25 MB.")
    upload = request.files.get("audio")
    if upload is None:
        abort(400, description="Send the recording as 'audio'.")
    declared = stories.declared_type(upload.mimetype or upload.content_type, upload.filename)
    if declared is None:
        abort(415, description="A story has to be a sound recording.")
    data = upload.stream.read(stories.MAX_BYTES + 1)
    if len(data) > stories.MAX_BYTES:
        abort(413, description="A story can be at most 25 MB.")
    if not data:
        abort(400, description="The recording is empty.")
    found = stories.sniff(data[:64])
    if found is None:
        abort(415, description="That file is not a sound recording Ninaivu can keep.")
    try:
        text = stories.clean_text(request.form.get("text"), stories.MAX_TEXT)
        speaker = stories.clean_text(request.form.get("speaker"), stories.MAX_SPEAKER)
    except ValueError:
        abort(400, description=f"The words can be at most {stories.MAX_TEXT} characters, "
                               f"and the name {stories.MAX_SPEAKER}.")
    user = current_user()
    story = stories.add(
        conn, _folder(), asset_id, data, mime=found, created_by=user.id or None,
        speaker=speaker or (user.display_name or "")[:stories.MAX_SPEAKER],
        text=text, duration=stories.clean_duration(request.form.get("duration")))
    return jsonify({"story": _for_viewer(story)}), 201


@bp.get("/api/stories/<int:story_id>/audio")
def story_audio(story_id: int):
    return _send(_story_or_404(story_id))


@bp.delete("/api/stories/<int:story_id>")
@require_family
def delete_story(story_id: int):
    """Take a story away. Whoever told it may, and an administrator."""
    story = _story_or_404(story_id)
    if not (_mine(story) or current_user().is_admin):
        abort(403, description="Only whoever told this story, or an administrator, "
                               "can delete it.")
    stories.delete(_conn(), story_id, _folder())
    return jsonify({"ok": True})


# --- on a share link -------------------------------------------------------

@bp.get("/api/share/<token>/stories/<int:asset_id>")
def shared_stories(token: str, asset_id: int):
    """An item's stories, for whoever holds a link that shows it."""
    _share_asset_or_404(token, asset_id)
    return jsonify({"stories": [
        _public(s, f"/api/share/{token}/story/{s['id']}")
        for s in stories.for_asset(_conn(), asset_id)]})


@bp.get("/api/share/<token>/story/<int:story_id>")
def shared_story_audio(token: str, story_id: int):
    story = stories.get(_conn(), story_id)
    if not story:
        abort(404)
    _share_asset_or_404(token, int(story["asset_id"]))
    return _send(story)
