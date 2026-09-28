"""The JSON object a request sent, or a 400 that says what was wrong."""

from __future__ import annotations

from typing import Any

from flask import abort, request


def json_object() -> dict[str, Any]:
    """The request body as a dict — an empty one when nothing was sent.

    Endpoints read their settings with ``data.get(...)``. Read as
    ``request.get_json(silent=True) or {}``, a body of ``[1]``, ``"on"`` or
    ``5`` reached that ``.get`` and answered 500: the server's fault, by its
    own account, for what was a malformed request.
    """
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        abort(400, description="Send the settings as a JSON object.")
    return data
