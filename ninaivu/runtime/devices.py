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

import platform as platform_module
import sys
from pathlib import Path
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


#: How the image models (SigLIP 2's image half, Florence-2's image encoder,
#: DDColor) can be run, by name: the processor, or Core ML compiled as an ML
#: Program (full precision, which the older Neural Network format is not) on
#: every unit it likes or on the processor and graphics processor only. The
#: household picks one with the ``ai_image_device`` setting; ``auto`` is the
#: default (:func:`resolve_image_device`).
#:
#: On an M1 runner (.github/workflows/ai-engine.yml) all of them gave SigLIP 2
#: the same vectors to five decimals, and the processor was the quickest (330
#: ms a photograph against 450 to 465 for Core ML). But that runner is a
#: virtual machine with no real graphics processor or Neural Engine to give
#: it. On a real Mac mini with an M4 Pro (tools/check_onnx_search.py, 40
#: photographs) the processor took 141 to 160 ms a photograph and Core ML 15
#: (every unit) or 16 (processor and graphics processor), with the same
#: vectors. Core ML compiles the model first, 77 to 125 s there, which is why
#: the compiled model is kept (:func:`coreml_cache`): it is paid once, not each
#: time the model host starts.
IMAGE_DEVICES = ("cpu", "coreml", "coreml-gpu")
#: The setting's values: one of :data:`IMAGE_DEVICES`, or ``auto``.
IMAGE_DEVICE_CHOICES = ("auto", *IMAGE_DEVICES)


def _apple_silicon(arch: str | None = None) -> bool:
    return (arch or platform_module.machine() or "").lower() in ("arm64", "aarch64")


def resolve_image_device(choice: str | None = None, *, coreml_measured: bool = False,
                         platform: str | None = None, arch: str | None = None) -> str:
    """What *choice* means here: one of :data:`IMAGE_DEVICES`, or
    ``"accelerated"`` (the best provider there is) away from a Mac.

    ``auto`` on a Mac with Apple silicon is Core ML for a model measured on
    it (*coreml_measured*: SigLIP 2's image half, ten times quicker on an M4
    Pro, and Florence-2's image encoder at one size, twice as quick there),
    and the processor for the others until they are measured; on an
    Intel Mac it is the processor. A choice the household made is kept.
    """
    choice = (choice or "auto").lower()
    if choice != "auto":
        return choice
    if _platform(platform) == "darwin":
        return "coreml" if coreml_measured and _apple_silicon(arch) else "cpu"
    return "accelerated"


def coreml_cache(root: str | Path | None, model: Path) -> Path | None:
    """Where Core ML keeps *model* compiled, under *root* (the model host's
    ``coreml_cache``), or None for no cache.

    ONNX Runtime keys what it keeps by the model's path and never notices
    the file changing, so the folder is named by the file's size and time as
    well: a model downloaded again is compiled again.
    """
    if not root:
        return None
    try:
        stat = Path(model).stat()
    except OSError:
        return None
    name = f"{Path(model).parent.name}-{Path(model).stem}-{stat.st_size}-{stat.st_mtime_ns}"
    return Path(root) / name


def image_providers(choice: str | None = None, available: Sequence[str] | None = None, *,
                    platform: str | None = None, coreml_measured: bool = False,
                    cache: str | Path | None = None) -> list[Any]:
    """The providers for an image model: *choice* is one of
    :data:`IMAGE_DEVICE_CHOICES` (``None`` is ``auto``). *cache* is where
    Core ML keeps the compiled model (:func:`coreml_cache`)."""
    choice = resolve_image_device(choice, coreml_measured=coreml_measured, platform=platform)
    if choice == "cpu":
        return [CPU]
    if choice in ("coreml", "coreml-gpu"):
        offered = set(installed_onnx_providers() if available is None else available)
        if "CoreMLExecutionProvider" not in offered:
            return [CPU]
        options = {"ModelFormat": "MLProgram",
                   "MLComputeUnits": "ALL" if choice == "coreml" else "CPUAndGPU"}
        if cache:
            try:
                Path(cache).mkdir(parents=True, exist_ok=True)
                options["ModelCacheDirectory"] = str(cache)
            except OSError:
                pass                     # compiled each time, as without a cache
        return [("CoreMLExecutionProvider", options), CPU]
    return onnx_providers(available, platform=platform)


#: The short names the console knows each provider by (static/js/performance.js).
_CODES = {"CoreMLExecutionProvider": "coreml", "DmlExecutionProvider": "directml",
          "CUDAExecutionProvider": "cuda", CPU: "cpu"}


def device_code(provider: str, choice: str | None = None) -> str:
    """The console's name for where a session runs: its first *provider*,
    with Core ML told apart by the *choice* it was asked for."""
    code = _CODES.get(provider, provider.lower())
    if code == "coreml" and (choice or "").lower() == "coreml-gpu":
        return "coreml-gpu"
    return code


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
