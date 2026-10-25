"""Whether a feature is on: the household's setting, and for the features
that need a model, whether that model is on this computer.

Since 3.1 those features are on by default, and the AI models page is the
one place that decides them (settings_groups.FOLLOW_THEIR_MODEL): with the
model downloaded the feature is offered, without it the feature is not, as
if it were switched off. A ``false`` saved in config.json, or the setting's
environment variable at 0, still turns it off.

Asking whether a model is there reads the disk and looks for a package, and
the family app asks on every page, so each answer is kept for
:data:`KEEP_SECONDS`.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

#: What each feature needs before it is offered.
NEEDS: dict[str, str] = {
    "ai_runtime": "any",
    "ask_ninaivu": "writing",
    "photo_talk": "looking",
    "voice_search": "speech",
    "colour_photos": "colour",
    # The picture search the gallery already has: nothing more to download.
    "video_moments": "",
}

#: How long an answer about a model is kept.
KEEP_SECONDS = 30.0

_lock = threading.Lock()
_seen: dict[str, tuple[float, bool]] = {}


def _why_not(need: str) -> str | None:
    """Why the model *need* names is not here, or None when it is."""
    if need == "writing":
        from ..runtime import llm                                # noqa: PLC0415
        return llm.ready()
    if need == "looking":
        from ..runtime import llm                                # noqa: PLC0415
        return llm.vision_ready()
    if need == "speech":
        from ..runtime import transcriber                        # noqa: PLC0415
        return transcriber.ready()
    if need == "colour":
        from ..runtime import colour                             # noqa: PLC0415
        return colour.ready()
    if need == "any":
        from ..runtime import florence, siglip                   # noqa: PLC0415
        checks: tuple[Callable[[], str | None], ...] = (
            siglip.ready, florence.ready, lambda: _why_not("speech"),
            lambda: _why_not("writing"), lambda: _why_not("colour"))
        reasons = [check() for check in checks]
        return None if any(r is None for r in reasons) else reasons[0]
    return None


def model_ready(need: str) -> bool:
    """Whether the model *need* names is on this computer, kept a while."""
    if not need:
        return True
    now = time.monotonic()
    with _lock:
        kept = _seen.get(need)
        if kept is not None and now - kept[0] < KEEP_SECONDS:
            return kept[1]
    try:
        answer = _why_not(need) is None
    except Exception:                                            # noqa: BLE001 - not there
        answer = False
    with _lock:
        _seen[need] = (now, answer)
    return answer


def forget() -> None:
    """A model was downloaded or removed: ask again next time."""
    with _lock:
        _seen.clear()


def on(cfg: Any, name: str) -> bool:
    """Whether the feature *name* is on for *cfg*."""
    if not bool(getattr(cfg, name, False)):
        return False
    need = NEEDS.get(name)
    return need is None or model_ready(need)


__all__ = ["NEEDS", "forget", "model_ready", "on"]
