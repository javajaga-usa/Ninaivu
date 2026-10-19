"""What a worker process does with one task.

Every function here runs inside a worker process (pool.py), takes plain
values and returns plain values: nothing of the server's (its connections,
its locks, its models) crosses into a worker, and only what was found comes
back. What a worker loads once — a text reader, the face models — it keeps
in :data:`_KEPT` for every later task it is given.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: What this worker process has loaded, by name.
_KEPT: dict[str, Any] = {}

#: The answer for a file that is not there (a drive unplugged, a file
#: deleted since it was queued): not a failure of the file, and not done.
MISSING = "missing"


def read_text(path: str, min_score: float, max_chars: int, threads: int) -> str | None:
    """The words in one preview, or ``""``; :data:`MISSING` if it is gone."""
    from ..media import ocr                               # noqa: PLC0415
    if not Path(path).exists():
        return MISSING
    if _KEPT.get("ocr_threads") != threads:
        # One reader for this process, on the share of the cores it was given.
        ocr.own_engine_per_thread(threads)
        _KEPT["ocr_threads"] = threads
    return ocr.read(path, min_score=min_score, max_chars=max_chars)["text"]


def find_faces(state_dir: str, path: str, rotation: int) -> Any:
    """The faces in one original: a list of ``DetectedFace`` (each with its
    crop and embedding), ``None`` when the file cannot be read as an image,
    :data:`MISSING` when it is not there."""
    from ..media import faces                             # noqa: PLC0415
    if not Path(path).exists():
        return MISSING
    engine = _KEPT.get(("faces", state_dir))
    if engine is None:
        engine = _KEPT[("faces", state_dir)] = faces.FaceEngine(Path(state_dir))
    if not engine.available:
        raise RuntimeError(engine.unavailable_reason or "the face models are not here")
    image = faces.load_image_for_faces(Path(path), rotation)
    if image is None:
        return None
    return engine.detect(image)
