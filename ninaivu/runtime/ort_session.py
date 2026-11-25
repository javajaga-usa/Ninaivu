"""An ONNX Runtime session, as the model host's image and speech models open one.

Shared by more than one model plugin (ninaivu/models/), which is why it is
here and not in any of them.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)


def _session(path: Path, threads: int, providers: Sequence[Any],
             fixed: dict[str, int] | None = None) -> Any:
    """An ONNX Runtime session. *fixed* gives sizes the model leaves open
    (its input's named dimensions) one value, so an accelerator is handed a
    model of one shape (runtime/florence.py)."""
    import onnxruntime                                         # noqa: PLC0415
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    for name, size in (fixed or {}).items():
        options.add_free_dimension_override_by_name(name, int(size))
    try:
        return onnxruntime.InferenceSession(str(path), options, providers=list(providers))
    except Exception:                                          # noqa: BLE001
        if list(providers) == ["CPUExecutionProvider"]:
            raise
        # An accelerator that cannot take this model fails at load, not
        # quietly: the processor takes anything.
        log.warning("%s would not load on %s; using the processor", path.name, providers[0],
                    exc_info=True)
        return onnxruntime.InferenceSession(str(path), options,
                                            providers=["CPUExecutionProvider"])
