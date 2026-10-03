"""The JSON object a request sent, or a 400 that says what was wrong."""

from __future__ import annotations

from typing import Any

from flask import abort, request

#: No settings or selection a screen sends comes near this (100,000 item ids
#: are under a megabyte). A body claiming more is refused before it is read
#: into memory, rather than parsed and then found wrong. The routes that take
#: a photograph as JSON (the AI Playground) read their own bodies with their
#: own limits and do not come through here.
JSON_MAX_BYTES = 4 * 1024 * 1024


def refuse_oversized_json() -> None:
    """A 413 for a JSON body larger than :data:`JSON_MAX_BYTES`, unread."""
    if (request.content_length or 0) > JSON_MAX_BYTES:
        abort(413, description="That is too large.")


def json_object() -> dict[str, Any]:
    """The request body as a dict — an empty one when nothing was sent.

    Endpoints read their settings with ``data.get(...)``. Read as
    ``request.get_json(silent=True) or {}``, a body of ``[1]``, ``"on"`` or
    ``5`` reached that ``.get`` and answered 500: the server's fault, by its
    own account, for what was a malformed request.
    """
    refuse_oversized_json()
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        abort(400, description="Send the settings as a JSON object.")
    return data
