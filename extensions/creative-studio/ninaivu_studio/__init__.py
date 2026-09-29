"""Creative Studio: generative edits with multi-gigabyte models.

This is an extension, not part of the core, because the core promises to run
on a small machine without a graphics card. Everything here needs the
opposite: a diffusion model on this computer (``NINAIVU_IMAGE_EDIT_MODEL``,
gigabytes of weights, a GPU to be usable), or a ComfyUI server elsewhere in
the house that Sudar's heavy jobs — generative edits, object removal,
upscaling, restoration, colourising — are handed to. Nothing leaves the
house: the AI server is an address on the home network the administrator
chose, and the console refuses one outside it.

It is off until an administrator turns it on (AI models → Extensions). Then
the console's AI server page appears, and Sudar's generate, remove and
enhance tools use what is set up there. Without it, Sudar still edits
photographs — light, colour, crops, looks — and removes objects with the
small local models the core carries.

The core's ``ai_server_*`` settings belong to this extension; they stay in
``Config`` so the All settings page can show them and a household that turns
the extension off keeps what it typed.
"""
from __future__ import annotations

from typing import Any

from ninaivu.words import said

NAME = "creative-studio"
TITLE = "Creative Studio"
SUMMARY = said("Generative edits, object removal, upscaling and restoration with large models — on this computer with a GPU, or on a ComfyUI server in the house.")
DATA_LEAVES_THE_MACHINE = False
DESTINATION = said("nothing leaves the house; an AI server, if set up, is an address on the home network that you chose")
DOWNLOADS = said("nothing by itself; a local image model is gigabytes, installed by hand")


def register(app, face: str) -> None:
    """The console's AI server page. Sudar's own routes stay in the core and
    ask ``studio`` below for what this extension can do."""
    from .console_api import ai_server_bp
    if face == "admin" or "admin" in app.blueprints:      # the console, or the all-in-one app
        if ai_server_bp.name not in app.blueprints:
            app.register_blueprint(ai_server_bp)


class _Studio:
    """What the core's Playground routes ask this extension for.

    Each method says "not mine" with ``None`` so the core carries on with
    what it has on this machine; see ``ninaivu/api/ai_playground_api.py``.
    """

    def capabilities(self, cfg) -> dict[str, Any]:
        import os
        from pathlib import Path

        from ninaivu.media.ai_editing import local_setting

        from . import generative_editing
        from .ai_server import service

        folder = os.environ.get("NINAIVU_IMAGE_EDIT_MODEL", local_setting("image_model"))
        local_image_model = bool(folder and (Path(folder) / "model_index.json").is_file())
        server_edit = service.assigned(cfg, "edit") is not None
        server_remove = service.assigned(cfg, "remove") is not None
        if server_edit:
            image_side = service.max_side(cfg)
        elif local_image_model:
            image_side = generative_editing.max_side()
        else:
            image_side = None
        return {
            "image_model": bool(server_edit or local_image_model),
            "image_edit_max_side": image_side,
            "image_provider": "ai-server" if server_edit else ("local" if local_image_model else None),
            "object_removal_provider": "ai-server" if server_remove else None,
            "server_jobs": service.active_jobs(cfg),
        }

    def edit(self, cfg, prompt: str, image: bytes, options: Any, report=None) -> bytes:
        from . import generative_editing
        from .ai_server import service

        entry = service.assigned(cfg, "edit")
        if entry is not None:
            return service.edit(cfg, entry, prompt, image, options, report)
        # On this machine, with the local model — which says so itself when
        # none is installed. Nothing here goes anywhere else.
        return generative_editing.generate(prompt, image, options)

    def remove(self, cfg, image: bytes, mask: bytes, report=None) -> bytes | None:
        from .ai_server import service

        entry = service.assigned(cfg, "remove")
        if entry is None:
            return None
        return service.remove(cfg, entry, image, mask, report)

    def job(self, cfg, kind: str, image: bytes, data: dict[str, Any]):
        """A callable ``work(report) -> bytes`` for *kind* on the AI server, or
        None when no workflow is assigned to it."""
        from ninaivu.media.ai_editing import check_prompt

        from . import generative_editing
        from .ai_server import service

        entry = service.assigned(cfg, kind)
        if entry is None:
            return None
        if kind == "edit":
            prompt = check_prompt(data.get("prompt"))
            options = data.get("options")
            generative_editing.generation_options(options)
            return lambda report: service.edit(cfg, entry, prompt, image, options, report)
        if kind == "remove":
            mask = data["mask"]
            return lambda report: service.remove(cfg, entry, image, mask, report)
        return lambda report: service.enhance(cfg, entry, image, report)


studio = _Studio()
