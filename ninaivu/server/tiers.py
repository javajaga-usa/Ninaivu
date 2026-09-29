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

#: Under this much memory, or without a graphics processor, a machine is Basic.
FULL_MEMORY_BYTES = 6 * 1024 ** 3

#: What each tier gives, in the words the Performance page uses.
GIVES: dict[str, list[str]] = {
    "basic": [
        "the gallery, albums, sharing and the family app",
        "faces, grouped and named on this machine",
        "sideways photographs found and put right",
        "places named from the photograph's location",
        "search by words in names, dates, places and people",
        "Mugil, the encrypted copy of the library",
    ],
    "full": [
        "everything Basic has",
        "tags, natural-language search and descriptions from the image model",
        "videos described from several moments, not one",
        "text read from photographs",
        "Creative Studio: generative edits, object removal, upscaling",
    ],
}


def detect(memory_bytes: int | None, graphics: str | None, apple_silicon: bool) -> str:
    """Which tier a computer with these parts is."""
    if apple_silicon:
        return "full"
    if graphics in ("cuda", "mps") and (memory_bytes is None or memory_bytes >= FULL_MEMORY_BYTES):
        return "full"
    return "basic"


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
    measured = detect(parts.get("memory_bytes"), graphics.get("available"), bool(parts.get("apple_silicon")))
    told = chosen(cfg)
    tier = told or measured
    if told:
        why = f"set to {told}" + (f"; this computer measures as {measured}" if told != measured else "")
    elif tier == "full":
        why = ("Apple silicon" if parts.get("apple_silicon")
               else f"a graphics processor ({graphics.get('name') or graphics.get('available')})")
    else:
        gb = (parts.get("memory_bytes") or 0) / 1024 ** 3
        why = ("no graphics processor" if graphics.get("available") is None
               else f"{gb:.0f} GB of memory is not enough for the image model on {graphics.get('name') or 'the graphics processor'}")
    return {"tier": tier, "measured": measured, "set": told, "why": why,
            "gives": GIVES[tier], "missing": GIVES["full"][1:] if tier == "basic" else []}


def defaults(tier: str) -> dict[str, Any]:
    """The settings the tier decides when the household has not: what
    ``--ai auto`` means, and how much of the expensive passes to attempt."""
    if tier == "full":
        return {"ai_engine": "clip", "video_keyframes": 5, "clip_batch_size": 16}
    return {"ai_engine": "light", "video_keyframes": 1, "clip_batch_size": 4}


def apply(cfg: Any, tier: str) -> list[str]:
    """Fill in the tier's defaults where the household left the choice open
    (``ai_engine == "auto"`` and the numbers at their shipped defaults).
    Returns what changed. Nothing is saved: these are this start's answers."""
    from .config import Config                               # noqa: PLC0415

    shipped = Config()
    wanted = defaults(tier)
    changed = []
    if getattr(cfg, "ai_engine", "auto") == "auto":
        cfg.ai_engine_resolved = wanted["ai_engine"]
        changed.append("ai_engine")
    for name in ("video_keyframes", "clip_batch_size"):
        if getattr(cfg, name, None) == getattr(shipped, name, None) and getattr(cfg, name) != wanted[name]:
            setattr(cfg, name, wanted[name])
            changed.append(name)
    return changed
