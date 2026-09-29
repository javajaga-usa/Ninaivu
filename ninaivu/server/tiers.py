"""The two kinds of computer Ninaivu is tested on, and what each is given.

**Basic** is a small always-on box: 2 GB of memory and no graphics
processor — a mini PC, an old laptop, a single-board computer. It gets the
gallery, faces, orientation, places, the backup, and search on the light
engine: everything the core promises, and nothing that needs a large model.

**Full** is a computer with a graphics processor or Apple silicon. It gets
everything: the CLIP/SigLIP image model for tagging, natural-language search
and descriptions, video described from several moments, text read from
photographs, and the Creative Studio extension's heavy edits.

The tier is worked out at start from the memory and the graphics processor,
or set outright (``Config.hardware_tier``, ``NINAIVU_HARDWARE_TIER``). It
never turns a switch the household set: it decides the *defaults* — which
engine ``--ai auto`` means, how many moments of a video to describe, how
many photographs the model takes at once — and the Performance page says
which tier this computer is and why. CI runs the suite as both: Basic in a
2 GB memory cgroup, Full on an Apple-silicon runner with the model stack.
"""
from __future__ import annotations

import os
from typing import Any

TIERS = ("basic", "full")

#: With a graphics processor, at least this much memory to be Full.
FULL_MEMORY_BYTES = 6 * 1024 ** 3
#: Without one, the image model still runs on the processor — slowly, but it
#: runs — when it is installed and there is at least this much memory. A
#: household that installed it on a 16 GB PC, or the Docker image, had it
#: running before the tiers existed; classing them Basic took it away.
CPU_FULL_MEMORY_BYTES = 8 * 1024 ** 3

#: What each tier gives, in the words the Performance page uses.
GIVES: dict[str, list[str]] = {
    "basic": [
        "the gallery, albums, sharing and the family app",
        "faces, grouped and named on this machine",
        "sideways photographs found and put right",
        "places named from the photograph's location",
        "search by words in names, dates, places and people",
        "Mugil, the copy of the library in Google Drive",
    ],
    "full": [
        "everything Basic has",
        "tags, natural-language search and descriptions from the image model",
        "videos described from several moments, not one",
        "Creative Studio's generative edits, where a graphics processor is there",
    ],
}


def detect(memory_bytes: int | None, graphics: str | None, apple_silicon: bool,
           model_installed: bool = False) -> str:
    """Which tier a computer with these parts is."""
    if apple_silicon:
        return "full"
    if graphics in ("cuda", "mps") and (memory_bytes is None or memory_bytes >= FULL_MEMORY_BYTES):
        return "full"
    if model_installed and memory_bytes is not None and memory_bytes >= CPU_FULL_MEMORY_BYTES:
        return "full"
    return "basic"


def _model_installed() -> bool:
    """Is the image model's software here (torch and open_clip) — asked
    without importing either."""
    import importlib.util                                    # noqa: PLC0415
    return all(importlib.util.find_spec(name) is not None for name in ("torch", "open_clip"))


def chosen(cfg: Any) -> str | None:
    """A tier the household or the environment set outright, or None for auto."""
    for told in (os.environ.get("NINAIVU_HARDWARE_TIER"), getattr(cfg, "hardware_tier", None)):
        told = str(told or "").strip().lower()
        if told in TIERS:
            return told
    return None


def current(cfg: Any, engine: Any = None) -> dict[str, Any]:
    """The tier this computer is on, how it was decided, and what it gives."""
    from . import capacity                                   # noqa: PLC0415

    parts = capacity.machine()
    graphics = capacity.graphics(engine)
    installed = _model_installed()
    measured = detect(parts.get("memory_bytes"), graphics.get("available"),
                      bool(parts.get("apple_silicon")), installed)
    told = chosen(cfg)
    tier = told or measured
    if told:
        why = f"set to {told}" + (f"; this computer measures as {measured}" if told != measured else "")
    elif tier == "full":
        gb = (parts.get("memory_bytes") or 0) / 1024 ** 3
        if parts.get("apple_silicon"):
            why = "Apple silicon"
        elif graphics.get("available") in ("cuda", "mps"):
            why = f"a graphics processor ({graphics.get('name') or graphics.get('available')})"
        else:
            why = (f"the image model is installed and {gb:.0f} GB of memory runs it on the "
                   "processor — slowly on a large library")
    else:
        gb = (parts.get("memory_bytes") or 0) / 1024 ** 3
        if graphics.get("available") is not None:
            why = (f"{gb:.0f} GB of memory is not enough for the image model on "
                   f"{graphics.get('name') or 'the graphics processor'}")
        elif installed:
            why = f"no graphics processor, and {gb:.0f} GB of memory is too little to run the image model"
        else:
            why = "no graphics processor, and the image model is not installed (Settings → Extras)"
    return {"tier": tier, "measured": measured, "set": told, "why": why,
            "gives": GIVES[tier], "missing": GIVES["full"][1:] if tier == "basic" else []}


def defaults(tier: str) -> dict[str, Any]:
    """The settings the tier decides when the household has not: what
    ``--ai auto`` means, and how much of the expensive passes to attempt."""
    if tier == "full":
        return {"ai_engine": "clip", "video_keyframes": 5, "clip_batch_size": 16}
    return {"ai_engine": "light", "video_keyframes": 1, "clip_batch_size": 4}


def apply(cfg: Any, tier: str) -> list[str]:
    """Fill in the tier's defaults where the household left the choice open:
    ``ai_engine == "auto"``, and the numbers nobody has set (not in
    config.json). Returns what changed.

    Nothing here is saved as the household's choice: ``Config.save`` writes a
    tier-filled value only once somebody has changed it (see ``_tier_filled``),
    so a computer that later becomes Full gets Full's numbers, and the All
    settings page does not report the tier's answer as the household's.
    """
    from .config import Config                               # noqa: PLC0415

    shipped = Config()
    wanted = defaults(tier)
    chosen_here = getattr(cfg, "_chosen", set()) or set()
    filled = dict(getattr(cfg, "_tier_filled", {}) or {})
    changed = []
    if getattr(cfg, "ai_engine", "auto") == "auto":
        cfg.ai_engine_resolved = wanted["ai_engine"]
        changed.append("ai_engine")
    for name in ("video_keyframes", "clip_batch_size"):
        if name in chosen_here or getattr(cfg, name, None) != getattr(shipped, name, None):
            continue
        filled[name] = wanted[name]
        if getattr(cfg, name) != wanted[name]:
            setattr(cfg, name, wanted[name])
            changed.append(name)
    cfg._tier_filled = filled
    return changed
