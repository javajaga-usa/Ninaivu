"""Which part of this computer each model runs on.

ONNX Runtime runs a model through an *execution provider*: Core ML on a Mac
(the Neural Engine and the graphics processor), DirectML on Windows (any
graphics card), CUDA on an NVIDIA card where its build is installed, and the
processor everywhere. Which ones exist depends on the computer and on which
ONNX Runtime package is installed, so this asks rather than assumes, and
always ends the list with the processor: a provider that cannot take a model
hands it back to the next one, and the processor takes anything.

Quantised models are the exception. Whisper as Ninaivu downloads it is 8-bit
(a quarter of the size, and what makes large-v3 fit beside everything else on
a 16 GB Mac); Core ML and DirectML have no kernels for most 8-bit operations
and would move the model back and forth between processor and accelerator a
layer at a time, which is slower than the processor alone. So an 8-bit model
is put on the processor on purpose, and the accelerators are kept for the
full-precision image models that come next.
"""
from __future__ import annotations

import sys
from typing import Any, Sequence

from ..words import said

#: ONNX Runtime's providers in the order they are preferred, each with the
#: platforms it is worth asking for there and how the console names it.
ONNX_PROVIDERS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("CoreMLExecutionProvider", ("darwin",),
     said("the Neural Engine and graphics processor (Core ML)")),
    ("DmlExecutionProvider", ("win32",), said("the graphics card (DirectML)")),
    ("CUDAExecutionProvider", ("win32", "linux"), said("the graphics card (CUDA)")),
    ("CPUExecutionProvider", ("darwin", "win32", "linux"), said("the processor")),
)

CPU = "CPUExecutionProvider"


def _platform(platform: str | None) -> str:
    platform = platform or sys.platform
    return "linux" if platform.startswith("linux") else platform


def installed_onnx_providers() -> list[str]:
    """What the installed ONNX Runtime offers, or nothing when it is not there."""
    try:
        import onnxruntime                                    # noqa: PLC0415
        return list(onnxruntime.get_available_providers())
    except Exception:                                         # noqa: BLE001 - absent or broken
        return []


def onnx_providers(available: Sequence[str] | None = None, *, quantised: bool = False,
                   platform: str | None = None) -> list[str]:
    """The providers to hand ``InferenceSession``, best first, processor last.

    *available* is what ONNX Runtime offers (asked when not given). A
    *quantised* model gets the processor alone (see the module's note).
    """
    if quantised:
        return [CPU]
    offered = set(installed_onnx_providers() if available is None else available)
    here = _platform(platform)
    chosen = [name for name, platforms, _label in ONNX_PROVIDERS
              if name != CPU and here in platforms and name in offered]
    return [*chosen, CPU]


#: How the image models (SigLIP 2's image half, Florence-2's image encoder)
#: can be run, by name: the processor, or Core ML compiled as an ML Program
#: (full precision, which the older Neural Network format is not) on every
#: unit it likes or on the processor and graphics processor only.
#:
#: On an M1 (.github/workflows/ai-engine.yml) all of them gave SigLIP 2 the
#: same vectors to five decimals. The processor was the quickest there (330
#: ms a photograph against 450 to 465 for Core ML) and Core ML spent nearly
#: four minutes compiling the model each time the host started, so on a Mac
#: the processor is the default. A Mac with a larger graphics processor may
#: do better with Core ML: tools/check_onnx_search.py measures every one.
IMAGE_DEVICES = ("cpu", "coreml", "coreml-gpu")


def image_providers(choice: str | None = None, available: Sequence[str] | None = None, *,
                    platform: str | None = None) -> list[Any]:
    """The providers for an image model: *choice* is one of
    :data:`IMAGE_DEVICES`, or ``None``/``"auto"`` for the default here."""
    here = _platform(platform)
    choice = (choice or "auto").lower()
    if choice == "auto":
        choice = "cpu" if here == "darwin" else "accelerated"
    if choice == "cpu":
        return [CPU]
    if choice in ("coreml", "coreml-gpu"):
        offered = set(installed_onnx_providers() if available is None else available)
        if "CoreMLExecutionProvider" not in offered:
            return [CPU]
        units = "ALL" if choice == "coreml" else "CPUAndGPU"
        return [("CoreMLExecutionProvider", {"ModelFormat": "MLProgram", "MLComputeUnits": units}),
                CPU]
    return onnx_providers(available, platform=platform)


def sherpa_provider(*, quantised: bool, platform: str | None = None) -> str:
    """The provider name sherpa-onnx takes (``cpu``, ``coreml``, ``cuda``).

    sherpa-onnx carries its own ONNX Runtime, built with Core ML on the Mac
    and the processor elsewhere in its PyPI packages, so this is decided by
    the platform alone.
    """
    if quantised:
        return "cpu"
    return "coreml" if _platform(platform) == "darwin" else "cpu"


def label(provider: str) -> str:
    """How the console names a provider."""
    lowered = provider.lower()
    for name, _platforms, text in ONNX_PROVIDERS:
        if lowered in (name.lower(), name.lower().replace("executionprovider", "")):
            return text
    return said("the processor") if lowered == "cpu" else provider


def describe(platform: str | None = None) -> dict[str, Any]:
    """What the engine would use on this computer, for the console."""
    providers = onnx_providers(platform=platform)
    return {
        "image_models": [label(p) for p in providers],
        "accelerator": label(providers[0]) if providers[0] != CPU else "",
        "speech": label(sherpa_provider(quantised=True, platform=platform)),
    }
