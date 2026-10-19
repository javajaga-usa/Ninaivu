"""The on-device AI engine (Ninaivu 2.0, proposal C; a trial, off by default).

Switched on by the ``ai_runtime`` setting (Settings → Advanced, or
``NINAIVU_AI_RUNTIME=1`` for one launch). With it off nothing here runs, no
process is started and nothing is downloaded, and Ninaivu works exactly as
before. With it on:

* :mod:`.host` — **the model host**, one process of its own that owns the
  engine's models. The server asks it for a capability by name and gets plain
  values back; a crash inside a model costs one answer, not the gallery, and
  an idle host exits and gives its memory back.
* :mod:`.devices` — which part of the computer each model runs on: Core ML
  (the Neural Engine and graphics processor) on a Mac, DirectML on Windows,
  CUDA where it is installed, the processor always as the last resort; 8-bit
  models on the processor, where they are fastest.
* :mod:`.speech` — **voice stories as words**: Whisper large-v3 (or turbo),
  run through sherpa-onnx's ONNX Runtime, writes down what was said in
  Tamil or English and translates a Tamil story into English.
* :mod:`.transcriber` — keeps every story's words up to date in the
  background, giving way to the household like every analysis pass.

What comes next on the same host, as further capabilities: a caption for
every photograph, search vectors from ONNX instead of PyTorch (so torch can
leave the default build), Ask Ninaivu and the interview mode. The speech
models are downloaded only from the AI models page, and the speech reader is
installed only from Extras: nothing arrives that the household did not ask
for.
"""
from __future__ import annotations

from typing import Any

from .host import (CAPABILITIES, Capability, HostCrashed, HostError, HostStopped,
                   HostTimeout, ModelHost, Unavailable)

__all__ = ["CAPABILITIES", "Capability", "HostCrashed", "HostError", "HostStopped",
           "HostTimeout", "ModelHost", "Unavailable", "enabled", "host_setup"]


def enabled(cfg: Any) -> bool:
    """Whether the household has switched the on-device AI engine on."""
    return bool(getattr(cfg, "ai_runtime", False))


def host_setup(cfg: Any) -> dict[str, Any]:
    """What a model host for this server is started with: plain values only."""
    from ..media import model_catalog                          # noqa: PLC0415
    from ..utils import resources                              # noqa: PLC0415
    return {"models": str(model_catalog.models_root()),
            # Half of what the tuning lets the models use: the host works
            # while the family browses, and a story is not in a hurry.
            "threads": max(1, resources.compute_threads() // 2)}
