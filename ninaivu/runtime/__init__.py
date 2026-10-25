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

* :mod:`.siglip` — **search without PyTorch**: SigLIP 2 through ONNX
  Runtime, giving the server's ``ai.OnnxEngine`` the vectors, tags and
  content check PyTorch's engine gives, in a model host of its own.
* :mod:`.florence` — **a description of every photograph**, by Florence-2.
* :mod:`.describer` — keeps every photograph's description up to date in
  the background, from the same host as the stories.

* :mod:`.llm` — **writing**: one language model (Gemma 3 12B through
  llama.cpp) for everything that writes in Tamil or English: the chapters
  of a memory film now, Ask Ninaivu and the interview mode next.

* :mod:`.colour` — **colour for black-and-white photographs**, by DDColor,
  asked by the viewer's Add colour (``colour_photos``). It and a search
  spoken aloud (``voice_search``, through :mod:`.speech`) are answered by a
  second host, the quick host, so a person waiting never queues behind the
  background's long calls.

* :mod:`.voice` — **letters read aloud** (``letters_aloud``): Meta's MMS
  voices read the words of an old letter in Tamil or English, on the quick
  host.

What comes next on the same hosts, as further capabilities: Ask Ninaivu and
the interview mode, both asking :mod:`.llm`. The models are downloaded only from the AI models page,
and the packages they run on are installed only from Extras: nothing arrives
that the household did not ask for.
"""
from __future__ import annotations

from typing import Any

from .host import (CAPABILITIES, Capability, HostBusy, HostCrashed, HostError, HostStopped,
                   HostTimeout, ModelHost, Unavailable)

__all__ = ["CAPABILITIES", "Capability", "HostBusy", "HostCrashed", "HostError", "HostStopped",
           "HostTimeout", "ModelHost", "Unavailable", "enabled", "host_setup", "image_device"]


def enabled(cfg: Any) -> bool:
    """Whether the on-device AI engine is on: on by default since 3.1 once one
    of its models is downloaded, unless the household turned it off
    (server/features.py)."""
    from ..server import features                      # noqa: PLC0415
    return features.on(cfg, "ai_runtime")


def image_device(cfg: Any) -> str:
    """Where the household lets the image models run (``ai_image_device``,
    runtime/devices.py): the processor alone when the graphics processor is
    switched off (``ai_gpu``)."""
    if not bool(getattr(cfg, "ai_gpu", True)):
        return "cpu"
    return str(getattr(cfg, "ai_image_device", "auto") or "auto").lower()


def host_setup(cfg: Any) -> dict[str, Any]:
    """What a model host for this server is started with: plain values only."""
    from pathlib import Path                                   # noqa: PLC0415

    from ..media import model_catalog                          # noqa: PLC0415
    from ..utils import resources                              # noqa: PLC0415
    setup: dict[str, Any] = {
        "models": str(model_catalog.models_root()),
        # Half of what the tuning lets the models use: the host works
        # while the family browses, and a story is not in a hurry.
        "threads": max(1, resources.compute_threads() // 2),
        "image_device": image_device(cfg),
    }
    state = getattr(cfg, "state_dir", None)
    if state:
        # Core ML's compiled models, kept so a host that starts again does
        # not spend minutes compiling the same model (devices.coreml_cache).
        setup["coreml_cache"] = str(Path(state) / "coreml-cache")
    return setup
