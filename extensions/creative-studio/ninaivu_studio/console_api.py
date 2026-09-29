"""The AI server tab's endpoints — console only, never the family port.

These set the address photographs are sent to for heavy image edits, so like
Cloud and Archive they are registered on the admin app alone: on port 5000 they
do not exist.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .ai_server import service, workflows
from .ai_server.comfyui import AIServerError, Client, network_scope, normalise_url
from ninaivu.server import auth
from ninaivu.server.auth import current_user, require_admin
from ninaivu.storage import db
from ninaivu.api._body import json_object

log = logging.getLogger(__name__)

ai_server_bp = Blueprint("ai_server", __name__)


def _cfg():
    return current_app.config["MV_CONFIG"]


def _settings(cfg) -> dict[str, Any]:
    return {
        "url": cfg.ai_server_url,
        "enabled": bool(cfg.ai_server_enabled),
        "edit_workflow": cfg.ai_server_edit_workflow,
        "remove_workflow": cfg.ai_server_remove_workflow,
        "jobs": {purpose: getattr(cfg, setting) for purpose, setting in service.JOB_SETTINGS.items()},
        "timeout": service.timeout(cfg),
        "max_side": service.max_side(cfg),
        "network": network_scope(cfg.ai_server_url) if cfg.ai_server_url else None,
        "active": {purpose: service.assigned(cfg, purpose) is not None
                   for purpose in workflows.PURPOSES},
    }


def _payload(cfg, node_types: set[str] | None = None) -> dict[str, Any]:
    return {
        "settings": _settings(cfg),
        "workflows": [workflows.describe(entry, node_types) for entry in workflows.all_entries(cfg)],
        "purposes": {key: {"label": value["label"], "required": list(value["required"])}
                     for key, value in workflows.PURPOSES.items()},
        # JSON objects arrive with their keys sorted; this is the order to show.
        "purpose_order": list(workflows.PURPOSES),
        "placeholders": list(workflows.PLACEHOLDERS),
        "limits": {"timeout": list(service.TIMEOUT_RANGE), "max_side": list(service.MAX_SIDE_RANGE)},
    }


def _bounded(value: Any, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ValueError(f"{what} must be a whole number.")
    if not low <= int(value) <= high:
        raise ValueError(f"{what} must be between {low} and {high}.")
    return int(value)


@ai_server_bp.get("/api/admin/ai-server")
@require_admin
def ai_server_state():
    return jsonify(_payload(_cfg()))


@ai_server_bp.post("/api/admin/ai-server")
@require_admin
def save_ai_server():
    """Change the settings. The whole request is validated before any of it applies."""
    cfg = _cfg()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Send the settings as a JSON object."}), 400
    allowed = {"url", "enabled", "edit_workflow", "remove_workflow", "jobs", "timeout", "max_side"}
    if set(data) - allowed:
        return jsonify({"error": f"Unknown settings: {', '.join(sorted(set(data) - allowed))}."}), 400
    try:
        changes: dict[str, Any] = {}
        if "url" in data:
            changes["ai_server_url"] = normalise_url(data["url"]) if data["url"] else ""
            # This extension promises that nothing leaves the house; an AI
            # server on the internet would break that promise silently.
            if changes["ai_server_url"] and network_scope(changes["ai_server_url"]) == "public":
                raise ValueError("That address is on the internet. The AI server has to be "
                                 "on the home network (or your Tailscale network).")
        if "enabled" in data:
            if not isinstance(data["enabled"], bool):
                raise ValueError("enabled must be true or false.")
            changes["ai_server_enabled"] = data["enabled"]
        wanted: dict[str, Any] = {}
        if "jobs" in data:
            if not isinstance(data["jobs"], dict) or set(data["jobs"]) - set(workflows.PURPOSES):
                raise ValueError("jobs must map each job to a workflow id.")
            wanted.update(data["jobs"])
        for key, purpose in (("edit_workflow", "edit"), ("remove_workflow", "remove")):
            if key in data:
                wanted[purpose] = data[key]
        for purpose, chosen in wanted.items():
            chosen = chosen or ""
            if not isinstance(chosen, str):
                raise ValueError("A workflow id must be text.")
            if chosen:
                entry = workflows.load(cfg, chosen)
                if entry is None or entry["purpose"] != purpose:
                    raise ValueError(f"No saved {workflows.PURPOSES[purpose]['label'].lower()} "
                                     f"workflow is called {chosen!r}.")
            changes[service.JOB_SETTINGS[purpose]] = chosen
        if "timeout" in data:
            changes["ai_server_timeout"] = _bounded(data["timeout"], *service.TIMEOUT_RANGE, "The timeout")
        if "max_side" in data:
            changes["ai_server_max_side"] = _bounded(data["max_side"], *service.MAX_SIDE_RANGE, "The preview size")
        url = changes.get("ai_server_url", cfg.ai_server_url)
        if changes.get("ai_server_enabled", cfg.ai_server_enabled) and not url:
            raise ValueError("Set the AI server's address before turning it on.")
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    for key, value in changes.items():
        setattr(cfg, key, value)
    if changes:
        cfg.save()
        auth.audit(db.connect(cfg.db_path), current_user().id, "ai_server",
                   ", ".join(sorted(k.replace("ai_server_", "") for k in changes)))
    return jsonify({"ok": True, **_payload(cfg)})


@ai_server_bp.post("/api/admin/ai-server/test")
@require_admin
def test_ai_server():
    """Reach the server; describe it; check every saved workflow's nodes exist there.

    Tests the address in the request if one is given — so it can be tried before
    it is saved — and the saved one otherwise.
    """
    cfg = _cfg()
    data = json_object()
    try:
        url = normalise_url(data.get("url") or cfg.ai_server_url)
        if network_scope(url) == "public":
            raise ValueError("That address is on the internet. The AI server has to be "
                             "on the home network (or your Tailscale network).")
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    client = Client(url, timeout=10)
    try:
        stats = client.system_stats()
        node_types = client.node_types()
    except AIServerError as error:
        return jsonify({"ok": False, "url": url, "network": network_scope(url), "error": str(error)})
    return jsonify({"ok": True, "url": url, "network": network_scope(url), "server": stats,
                    "node_count": len(node_types), **_payload(cfg, node_types)})


@ai_server_bp.post("/api/admin/ai-server/workflows")
@require_admin
def save_workflow():
    cfg = _cfg()
    if request.content_length and request.content_length > workflows.MAX_WORKFLOW_BYTES + 4096:
        return jsonify({"error": "That workflow is larger than Ninaivu accepts (2 MB)."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - {"name", "purpose", "workflow"}:
        return jsonify({"error": "Send name, purpose and workflow."}), 400
    try:
        entry = workflows.save(cfg, data.get("name"), data.get("purpose"), data.get("workflow"))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    auth.audit(db.connect(cfg.db_path), current_user().id, "ai_server_workflow",
               f"saved {entry['id']} ({entry['purpose']})")
    return jsonify({"ok": True, "workflow": workflows.describe(entry), **_payload(cfg)})


@ai_server_bp.delete("/api/admin/ai-server/workflows/<workflow_id>")
@require_admin
def delete_workflow(workflow_id: str):
    cfg = _cfg()
    if not workflows.delete(cfg, workflow_id):
        return jsonify({"error": "No saved workflow has that id."}), 404
    # A job pointing at a workflow that no longer exists would silently fall
    # back to the local model; clear it so the console says so.
    cleared = False
    for key in service.JOB_SETTINGS.values():
        if getattr(cfg, key) == workflow_id:
            setattr(cfg, key, "")
            cleared = True
    if cleared:
        cfg.save()
    auth.audit(db.connect(cfg.db_path), current_user().id, "ai_server_workflow", f"deleted {workflow_id}")
    return jsonify({"ok": True, **_payload(cfg)})
