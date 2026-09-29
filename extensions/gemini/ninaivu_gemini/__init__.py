"""Gemini for Sudar: edits, descriptions and plans from Google's models.

This is an extension, not part of the core, because turning it on means a
photograph leaves the house: a re-encoded copy of the picture (1024 px, no
metadata) is sent to Google's Generative Language API with the person's
instruction, and what Google returns is shown as a separate preview. The
original is never changed and nothing is stored on Google's side by Ninaivu.

It is off until an administrator turns it on in the console (System →
Extensions) and gives it an API key, and it never runs unless a request from
the Sudar page names it as the provider.
"""
from __future__ import annotations

from typing import Any

from ninaivu.words import said

NAME = "gemini"
TITLE = "Gemini"
SUMMARY = said("Edits, descriptions and adjustment plans from Google's Gemini models, for the Sudar photo studio. Needs a Google API key.")
DATA_LEAVES_THE_MACHINE = True
DESTINATION = said("a re-encoded copy of the photograph (1024 px, without metadata) and your instruction go to Google's Generative Language API")
DOWNLOADS = ""


def register(app, face: str) -> None:
    from .api import gemini_admin_bp, gemini_bp
    # The playground routes are for family members and live on the family
    # app; the key is set on the console. ``create_app`` builds one app with
    # everything, and a blueprint registered twice is an error, so each goes
    # on once.
    if gemini_bp.name not in app.blueprints:
        app.register_blueprint(gemini_bp)
    if face == "admin" or "admin" in app.blueprints:      # the console, or the all-in-one app
        if gemini_admin_bp.name not in app.blueprints:
            app.register_blueprint(gemini_admin_bp)


class _Provider:
    """What Sudar's own routes may hand an edit to (see ninaivu/extensions.py)."""

    def capabilities(self) -> dict[str, Any]:
        from . import gemini
        return gemini.capabilities()

    def is_available(self) -> bool:
        from . import gemini
        return gemini.is_available()

    def plan_adjustments(self, prompt: str, current: dict[str, Any],
                         image_bytes: bytes | None = None) -> dict[str, Any]:
        from . import gemini
        return gemini.plan_adjustments(prompt, current, image_bytes)

    def generate_image_edit(self, prompt: str, image_bytes: bytes,
                            options: dict[str, Any] | None = None) -> bytes:
        from . import gemini
        return gemini.generate_image_edit(prompt, image_bytes, options)


image_provider = _Provider()
