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
from ..server import features
from ..server import auth
from ..server.auth import current_user, require_admin
from ..storage import db, resume
from ..words import refusal
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
    "sherpa_onnx": "pip install -r requirements/requirements-speech.txt",
    "tokenizers": "pip install -r requirements/requirements-image-engine.txt",
    "llama_cpp": "pip install -r requirements/requirements-text-model.txt",
}


def _packages() -> dict[str, Any]:
    info: dict[str, Any] = {}
    for name in ("onnxruntime", "tokenizers", "torch", "open_clip", "transformers", "diffusers",
                 "accelerate"):
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
    onnx_ready = ai.onnx_search_ready(cfg)
    engine_name = str(getattr(engine, "name", "") or "")
    from ..media import ocr as ocr_mod  # noqa: PLC0415
    faces_ready = model_catalog.installed("faces") and not model_catalog.missing_packages("faces")
    # An engine started with AI off still answers with a name ("none"); only a
    # real model means videos and photographs are being described.
    search_on = bool(running) and str(running).lower() not in ("none", "off", "loading", "heuristic-v1")
    return {
        "models": models,
        "needed": needed_models(cfg),
        "queued": model_catalog.downloads.queued(),
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
        # while Ninaivu runs needs a restart before search uses it. So is the
        # on-device engine's copy of it, which runs without PyTorch.
        "restart_for_search": ((siglip_ready and "SigLIP2" not in running)
                               or (ai_on and onnx_ready and engine_name != "onnx")),
        # The on-device AI engine (Advanced settings), for the page to say
        # what it does with these models.
        "engine": _engine_state(cfg, engine_name, onnx_ready),
    }


def _engine_state(cfg: Any, engine_name: str, onnx_ready: bool) -> dict[str, Any]:
    from ..runtime import florence, siglip                 # noqa: PLC0415
    from ..storage import descriptions                     # noqa: PLC0415
    services = current_app.config.get("MV_SERVICES")
    describer = getattr(services, "describer", None)
    conn = db.connect(cfg.db_path)
    return {
        "enabled": features.on(cfg, "ai_runtime"),
        # "onnx" when search runs in the engine, without PyTorch.
        "search": engine_name,
        "search_waits_for": "" if onnx_ready else (siglip.ready() or ""),
        "descriptions": {
            "made": descriptions.count(conn),
            # Before any model is downloaded: what the suggested one would describe.
            "owed": descriptions.count_owed(
                conn, florence.version() or florence.version_of(florence.suggested_model())),
            "waits_for": florence.ready() or "",
            # Which description model is in use, and which one this
            # computer has the memory for (large from 24 GB).
            "model": florence.chosen_model() or "",
            "suggested": florence.suggested_model(),
            **({k: v for k, v in describer.status().items()
                if k in ("running", "done", "failed", "message", "per_picture")}
               if describer is not None else {}),
        },
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


# ---------------------------------------------------------------------------
# "Download all needed models"
# ---------------------------------------------------------------------------

#: The Playground's editing tools: small, and each makes a tool work. The
#: experimental generative model (5.5 GB) is left for somebody to ask for.
PLAYGROUND = ("lama", "upscale", "restore", "segmentation", "faceparse")


def _usable(model_id: str) -> bool:
    return model_catalog.installed(model_id) and not model_catalog.missing_packages(model_id)


def needed_models(cfg: Any) -> dict[str, Any]:
    """What this computer needs for the features that are on, one model per
    job: the gallery's own (faces, straightening), search and descriptions,
    voice stories, colour, writing and the Playground's tools.

    Of two sizes of one job (Florence-2 base or large, Whisper large-v3 or
    turbo) the one suited to this computer's memory, and none when either is
    here already. A model whose Python packages are missing is listed under
    ``skipped`` with them, rather than downloaded to sit unused.
    """
    from ..media import faces as faces_mod                   # noqa: PLC0415
    from ..runtime import florence                           # noqa: PLC0415
    wanted: list[str] = ["faces", "orientation"]
    if faces_mod.model_id_for(getattr(cfg, "face_model", "sface")) == faces_mod.ARCFACE_MODEL_ID:
        wanted.append("faces-arcface")
    engine_on = bool(getattr(cfg, "ai_runtime", False))
    if bool(getattr(cfg, "ai_enabled", True)) and not any(
            _usable(m) for m in ("siglip2", "search-onnx")):
        onnx_runs = not model_catalog.missing_packages("search-onnx")
        torch_runs = not model_catalog.missing_packages("siglip2")
        wanted.append("search-onnx" if engine_on and (onnx_runs or not torch_runs) else "siglip2")
    big = florence.suggested_model() == "captions-large"      # 24 GB of memory or more
    if engine_on:
        if getattr(cfg, "describe_photos", True) and not any(
                model_catalog.installed(m) for m in florence.MODELS):
            wanted.append(florence.suggested_model())
        if not any(model_catalog.installed(m) for m in ("speech", "speech-quick")):
            wanted.append("speech" if big else "speech-quick")
        if getattr(cfg, "colour_photos", False):
            wanted.append("colour")
        if getattr(cfg, "memory_films", False) or getattr(cfg, "ask_ninaivu", False) \
                or getattr(cfg, "photo_talk", False):
            wanted.append("text-model")
        if getattr(cfg, "photo_talk", False):
            wanted.append("vision-part")
    if getattr(cfg, "letters_aloud", False):
        wanted += ["voice-tamil", "voice-english"]
    wanted += PLAYGROUND

    ids: list[str] = []
    skipped: list[dict[str, Any]] = []
    for model_id in dict.fromkeys(wanted):
        if model_id not in model_catalog.MODELS or model_catalog.installed(model_id):
            continue
        missing = model_catalog.missing_packages(model_id)
        if missing:
            skipped.append({"id": model_id, "label": model_catalog.MODELS[model_id]["label"],
                            "packages": missing})
            continue
        ids.append(model_id)
    return {"ids": ids, "bytes": sum(model_catalog.total_bytes(m) for m in ids),
            "skipped": skipped}


def queue_model_downloads(db_path, model_ids: list[str]) -> list[str]:
    """Download *model_ids* one after another, each remembered across a
    restart from the moment it is asked for (so a restart carries the whole
    list on, not only the one that was downloading)."""
    conn = db.connect(db_path)
    for model_id in model_ids:
        if not model_catalog.installed(model_id):
            resume.want(conn, f"{MODEL_RESUME_PREFIX}{model_id}",
                        {"model_id": model_id, "force": False, "queued": True})
    return model_catalog.downloads.enqueue(
        model_ids, lambda m: start_model_download(db_path, m))


@ai_models_bp.post("/api/admin/ai-models/download-needed")
@require_admin
def download_needed():
    cfg = current_app.config["MV_CONFIG"]
    needed = needed_models(cfg)
    added = queue_model_downloads(cfg.db_path, needed["ids"])
    if added:
        auth.audit(db.connect(cfg.db_path), current_user().id, "ai_model_download",
                   f"all needed: {', '.join(added)} "
                   f"({sum(model_catalog.total_bytes(m) for m in added)} bytes)")
    return jsonify({"ok": True, "started": added, **_payload()}), 202 if added else 200


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def _let_go_of(model_id: str) -> None:
    """Whatever has *model_id* loaded lets go of it, so its files can be
    removed (Windows will not remove a file that is open) and the feature it
    served says the model is not here, rather than failing on a missing file.
    Each is best effort: a feature that cannot be told finds out the next
    time it looks, which every one of them does before it runs."""
    features.forget()
    services = current_app.config.get("MV_SERVICES")
    close_hosts = getattr(services, "close_model_host", None)
    if close_hosts is not None:
        try:
            close_hosts()           # voice stories, descriptions, writing, colour, voices
        except Exception:                                   # noqa: BLE001
            current_app.logger.debug("could not stop the model hosts", exc_info=True)
    from ..media import face_parser, onnx_tools, segmentation   # noqa: PLC0415
    try:
        with onnx_tools._lock:
            for key in [k for k in onnx_tools._sessions if k.split(":")[0] == model_id]:
                onnx_tools._sessions.pop(key, None)
        if model_id == "segmentation":
            with segmentation._session_lock:
                segmentation._session = segmentation._session_path = None
        if model_id == "faceparse":
            face_parser.forget()
    except Exception:                                       # noqa: BLE001
        current_app.logger.debug("could not let go of %s", model_id, exc_info=True)


def search_uses(model_id: str) -> bool:
    """Whether the search engine running now was loaded from *model_id*."""
    from .. import ai  # noqa: PLC0415
    engine = ai.get_engine()
    if engine is None:
        return False
    if model_id == "search-onnx":
        return isinstance(engine, ai.OnnxEngine)
    if model_id == "siglip2":
        return isinstance(engine, ai.ClipEngine) and "SigLIP2" in str(engine.model_id)
    return False


def reload_search(services: Any, cfg: Any) -> None:
    """Load the search engine again, as a restart would: with its model gone
    that is another model or the light engine, which keeps every search
    vector for when the model is back (ai.clip_choice). The gallery's word
    and tag search, browsing and the rest never needed it."""
    from .. import ai  # noqa: PLC0415
    try:
        engine = ai.build_engine(cfg, force=True)
        bound = ai.bind(engine, cfg)
        if services is not None:
            services.engine = bound
            scanner = getattr(services, "scanner", None)
            if scanner is not None:
                scanner.ai = bound
    except Exception:                                       # noqa: BLE001
        import logging  # noqa: PLC0415
        logging.getLogger(__name__).exception("could not load the search engine again")


@ai_models_bp.delete("/api/admin/ai-models/<model_id>")
@require_admin
def delete_model(model_id: str):
    """Remove a model from this computer. Nothing else depends on one: the
    feature it served says it is not installed until it is downloaded again."""
    if model_id not in model_catalog.MODELS:
        return jsonify({"error": "No model has that name."}), 404
    cfg = current_app.config["MV_CONFIG"]
    if model_catalog.downloads.state(model_id).get("status") == "downloading":
        return jsonify({"error": "This model is downloading. Delete it once the download has finished."}), 409
    reload = search_uses(model_id)
    _let_go_of(model_id)
    try:
        outcome = model_catalog.remove(model_id)
    except model_catalog.ModelBusy:
        return jsonify({"error": "This model is downloading. Delete it once the download has finished."}), 409
    resume.done(db.connect(cfg.db_path), f"{MODEL_RESUME_PREFIX}{model_id}")
    features.forget()
    if reload:
        import threading  # noqa: PLC0415
        threading.Thread(target=reload_search, name="ninaivu-search-reload",
                         args=(current_app.config.get("MV_SERVICES"), cfg), daemon=True).start()
    auth.audit(db.connect(cfg.db_path), current_user().id, "ai_model_delete",
               f"{model_id} ({outcome['freed_bytes']} bytes freed)"
               + (f"; not removed: {', '.join(outcome['failed'])}" if outcome["failed"] else ""))
    if outcome["failed"]:
        return jsonify({**refusal("Some of the files of this model are in use and were not removed: {files}. Stop Ninaivu and delete it again.",
                                  files=", ".join(outcome["failed"])),
                        **_payload()}), 409
    return jsonify({"ok": True, **outcome, "search_reloading": reload, **_payload()})
