"""The Extras tab — console only, never the family port.

What Ninaivu can run without but is better with: ffmpeg, iPhone photo support,
a second EXIF reader, reading the words in photographs. Each one was a line in
the release notes and a command to find, on every machine the household ran
Ninaivu on. Here they are a button.

**On installing things from a web page.** The AI models tab refuses to install
Python packages, and is right to: a server that installs whatever a request
names is a server that can be talked into installing anything. This is a
different thing, and the difference is the whole design — the request carries
an id from a fixed list, the id chooses a pinned requirement written in
Ninaivu's own source, and nothing a request says reaches the command line. It
is administrator-only, like everything else on this port, and it installs into
the environment Ninaivu is already running in rather than the system Python.

A tool like ffmpeg is not ours to redistribute, so it comes from the machine's
own package manager, which is also what will keep it up to date. Where there
is no package manager to use, the tab says where to download it instead of
pretending it can be fetched.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from ..media import components
from ..server import auth
from ..server.auth import current_user, require_admin
from ..storage import db
from flask import current_app

components_bp = Blueprint("components", __name__)


def _cfg():
    return current_app.config["MV_CONFIG"]


@components_bp.get("/api/admin/components")
@require_admin
def listing():
    """Everything optional, whether it is here, and how it would be fetched."""
    return jsonify({
        "components": components.describe_all(),
        "manager": components.available_manager() or "",
        "manager_label": (components.MANAGERS[components.available_manager()]["label"]
                          if components.available_manager() else ""),
    })


@components_bp.post("/api/admin/components/<component_id>/install")
@require_admin
def install(component_id: str):
    """Fetch one. The id chooses the command; the request never carries it."""
    try:
        components.catalogue_entry(component_id)
    except KeyError:
        return jsonify({"error": "There is nothing by that name to install."}), 404

    started, refusal = components.install(component_id)
    if not started:
        return jsonify({"error": refusal, **components.describe(component_id)}), 409
    auth.audit(db.connect(_cfg().db_path), current_user().id,
               "install_component", component_id)
    return jsonify({"ok": True, **components.describe(component_id)}), 202
