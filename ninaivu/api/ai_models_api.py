"""The AI models tab — console only, never the family port.

Lists the optional models in :mod:`ninaivu.media.model_catalog` and downloads
them on an administrator's request. Downloads run in the background; the tab
polls for progress. Packages are not installed from here — installing Python
packages from a web page is not something a server should do — so a model
whose packages are missing says which command to run.
"""
from __future__ import annotations

import importlib.util
from typing import Any

from flask import Blueprint, current_app, jsonify

from ..media import model_catalog
from ..server import auth
from ..server.auth import current_user, require_admin
from ..storage import db, resume
from ._body import json_object

ai_models_bp = Blueprint("ai_models", __name__)

#: What to run for each missing package, by the requirements file that has it.
INSTALL_HINTS = {
    "onnxruntime": "pip install -r requirements/requirements-ai-local.txt",
    "torch": "pip install \"torch>=2.0\" --index-url https://download.pytorch.org/whl/cpu",
    "open_clip": "pip install -r requirements/requirements-ai.txt",
    "transformers": "pip install -r requirements/requirements-ai.txt",
    "diffusers": "pip install -r requirements/requirements-ai-editing.txt",
    "accelerate": "pip install -r requirements/requirements-ai-editing.txt",
}


def _packages() -> dict[str, Any]:
    info: dict[str, Any] = {}
    for name in ("onnxruntime", "torch", "open_clip", "transformers", "diffusers", "accelerate"):
        info[name] = importlib.util.find_spec(name) is not None
    providers: list[str] = []
    if info["onnxruntime"]:
        try:
            import onnxruntime  # noqa: PLC0415
            providers = list(onnxruntime.get_available_providers())
        except Exception:                               # noqa: BLE001 - a broken install
            providers = []
    info["graphics_card"] = "DmlExecutionProvider" in providers
    return info


def _payload() -> dict[str, Any]:
    from .. import ai  # noqa: PLC0415
    engine = ai.get_engine()
    running = getattr(engine, "model_id", "") if engine is not None else ""
    models = []
    for model_id in model_catalog.MODELS:
        entry = model_catalog.describe(model_id)
        entry["install_hints"] = sorted({INSTALL_HINTS[p] for p in entry["missing_packages"]
                                         if p in INSTALL_HINTS})
        models.append(entry)
    cfg = current_app.config["MV_CONFIG"]
    ai_on = bool(cfg.ai_enabled) and (cfg.ai_engine or "auto").lower() in ("auto", "clip") \
        and (cfg.clip_model or "auto") == "auto"
    siglip_ready = ai_on and model_catalog.installed("siglip2") \
        and not model_catalog.missing_packages("siglip2")
    from ..media import ocr as ocr_mod  # noqa: PLC0415
    faces_ready = model_catalog.installed("faces") and not model_catalog.missing_packages("faces")
    # An engine started with AI off still answers with a name ("none"); only a
    # real model means videos and photographs are being described.
    search_on = bool(running) and str(running).lower() not in ("none", "off", "loading", "heuristic-v1")
    return {
        "models": models,
        "folder": str(model_catalog.models_root()),
        "packages": _packages(),
        "search_model": running,
        # What each of the scan's AI switches needs, and whether it is here.
        # The switches used to sit on another page, saying nothing about the
        # model each one waits for; a switch that is on with nothing behind
        # it looked like a feature that was working.
        "readiness": {
            "faces_enabled": {"ready": faces_ready, "needs": "Finding and grouping faces",
                              "model": "faces"},
            "video_keyframes": {"ready": search_on, "needs": "the AI search engine",
                                "model": "siglip2" if not search_on else ""},
            "ocr_enabled": {"ready": ocr_mod.available(), "needs": "the text reader (Extras, on the Home & extensions page)",
                            "model": ""},
            "place_names": {"ready": True, "needs": "", "model": ""},
        },
        # SigLIP 2 is picked up when the AI engine starts, so a download made
        # while Ninaivu runs needs a restart before search uses it.
        "restart_for_search": siglip_ready and "SigLIP2" not in running,
    }


@ai_models_bp.get("/api/admin/ai-models")
@require_admin
def ai_models():
    return jsonify(_payload())


def start_model_download(db_path, model_id: str, *, force: bool = False) -> bool:
    """Download a model, remembered until it is installed or has failed.

    A restart part way through a download of several hundred megabytes used to
    leave the model page saying nothing at all, as though it had never been
    asked for. Start-up carries a remembered download on; files already whole
    on disk are stepped over, so only what was missing is fetched again —
    unless *force*, which is what "Download again" asks for.
    """
    name = f"{MODEL_RESUME_PREFIX}{model_id}"
    resume.want(db.connect(db_path), name,
                {"model_id": model_id, "force": bool(force)})
    started = model_catalog.downloads.start(
        model_id, on_end=lambda _id: resume.done(db.connect(db_path), name),
        force=force)
    if not started and model_catalog.installed(model_id):
        resume.done(db.connect(db_path), name)
    return started


#: How a model download is remembered across a restart (storage/resume.py).
MODEL_RESUME_PREFIX = "model:"


@ai_models_bp.post("/api/admin/ai-models/<model_id>/download")
@require_admin
def download_model(model_id: str):
    """Download, update or fetch again — one route, because they are one job.

    `force` is "Download again": every file is fetched even when one of the
    right size is already there, which is the only way out of a model whose
    file is the right length and the wrong bytes.
    """
    if model_id not in model_catalog.MODELS:
        return jsonify({"error": "No model has that name."}), 404
    force = bool(json_object().get("force"))
    cfg = current_app.config["MV_CONFIG"]
    started = start_model_download(cfg.db_path, model_id, force=force)
    if started:
        auth.audit(db.connect(cfg.db_path), current_user().id, "ai_model_download",
                   f"{model_id} ({model_catalog.total_bytes(model_id)} bytes)"
                   + (" — asked for again" if force else ""))
    return jsonify({"ok": True, "started": started, **_payload()}), 202 if started else 200
