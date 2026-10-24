"""What a worker process does with one task.

Every function here runs inside a worker process (pool.py), takes plain
values and returns plain values: nothing of the server's (its connections,
its locks, its models) crosses into a worker, and only what was found comes
back. What a worker loads once — a text reader, the face models — it keeps
in :func:`_kept` for every later task it is given.

A run of a few tasks calls them on threads of the server instead
(pool.InProcessPool); then what is kept is each thread's own, as a worker's
is its own process's, so no two threads share one text reader.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

_LOCAL = threading.local()


def _kept() -> dict[Any, Any]:
    """What this worker (a process, or a thread of the server) has loaded,
    by name."""
    kept = getattr(_LOCAL, "kept", None)
    if kept is None:
        kept = _LOCAL.kept = {}
    return kept

#: The answer for a file that is not there (a drive unplugged, a file
#: deleted since it was queued): not a failure of the file, and not done.
MISSING = "missing"

#: The answer for work that could not be done this time for a reason that is
#: not the file's (the thumbnail folder full, say): left owed for the next
#: run, and not counted as one of the file's attempts.
AGAIN = "again"


def read_text(path: str, min_score: float, max_chars: int, threads: int) -> str | None:
    """The words in one preview, or ``""``; :data:`MISSING` if it is gone."""
    from ..media import ocr                               # noqa: PLC0415
    if not Path(path).exists():
        return MISSING
    if _kept().get("ocr_threads") != threads:
        # One reader for this process, on the share of the cores it was given.
        ocr.own_engine_per_thread(threads)
        _kept()["ocr_threads"] = threads
    if (ocr._own_engine() or ocr.engine()) is None:           # noqa: SLF001
        # A reader that would not load answers "" for every photograph,
        # which stamped each one as having no text (audit C-33).
        return AGAIN
    return ocr.read(path, min_score=min_score, max_chars=max_chars)["text"]


def find_faces(state_dir: str, path: str, rotation: int, model: str = "sface") -> Any:
    """The faces in one original: a list of ``DetectedFace`` (each with its
    crop and embedding), ``None`` when the file cannot be read as an image,
    :data:`MISSING` when it is not there, :data:`AGAIN` when the models are
    not. *model* is the ``face_model`` setting: which pair of models looks."""
    from ..media import faces                             # noqa: PLC0415
    if not Path(path).exists():
        return MISSING
    kept = _kept()
    engine = kept.get(("faces", state_dir, model))
    if engine is None:
        engine = kept[("faces", state_dir, model)] = faces.FaceEngine(Path(state_dir), model=model)
    if not engine.available:
        # The models are not here (ArcFace chosen and not downloaded yet, say).
        # Not the photograph's fault: raising charged it a failure, and two
        # set it aside as having no faces, so the whole library was marked
        # "no faces" in minutes (audit C-02). Owed again, with the engine made
        # again next time in case the models have arrived since.
        kept.pop(("faces", state_dir, model), None)
        return AGAIN
    image = faces.load_image_for_faces(Path(path), rotation)
    if image is None:
        return None
    return engine.detect(image)


def draw_cover(path: str, rel_path: str, duration: float | None, thumbs_dir: str,
               base: str, sizes: list[int], fmt: str, quality: int) -> Any:
    """Draw a sound file's picture and write its thumbnails: what to store
    (``thumb``, ``blurhash``, ``color``), or :data:`MISSING` if it is gone."""
    from ..media import audio_art, media                  # noqa: PLC0415
    if not Path(path).is_file():
        return MISSING
    image = audio_art.picture_for(path, rel_path, duration=duration)
    try:
        media.write_thumbnails(image, Path(thumbs_dir), base, sizes, fmt, quality)
        return {"thumb": base, "blurhash": media.blurhash_encode(image),
                "color": media.dominant_color(image)}
    except Exception as exc:  # noqa: BLE001
        # As Scanner._draw_audio_art: not drawn this time, tried again at
        # the next scan, never set aside for a disk that was full for a while.
        import logging                                    # noqa: PLC0415
        logging.getLogger(__name__).warning("no picture for %s: %s", rel_path, exc)
        return AGAIN
    finally:
        image.close()


def read_keyframes(root: str, rel_path: str, duration: float | None,
                   points: tuple[float, ...], explicit: bool = False) -> dict[str, Any]:
    """A clip's moments (``Scanner.read_keyframes``), each as the JPEG the
    image model is given, so what crosses back is a few hundred kilobytes."""
    import io                                             # noqa: PLC0415
    from ..media import scanner                           # noqa: PLC0415
    read = scanner.read_keyframes(root, rel_path, duration, points, explicit=explicit)
    frames = read.get("frames")
    if frames:
        encoded = []
        for image in frames:
            out = io.BytesIO()
            # As Scanner._tag_one_video saves them for the model.
            image.save(out, "JPEG", quality=scanner.KEYFRAME_QUALITY)
            image.close()
            encoded.append(out.getvalue())
        read["frames"] = encoded
    return read


def fingerprint(path: str) -> Any:
    """``(digest, size, mtime)``, or :data:`MISSING`, ``"changed"`` (it changed
    while read) or ``"unreadable"``."""
    from ..storage import fingerprints                    # noqa: PLC0415
    try:
        return fingerprints.fingerprint_file(path)
    except FileNotFoundError:
        return MISSING
    except fingerprints.Unchanged:
        return "changed"
    except OSError:
        return "unreadable"


def plan_faces(db_path: str, roots: Any, model: str = "sface") -> dict[str, Any]:
    """Work out how the faces of *roots* group into people
    (``faceindex.plan_regroup``), reading the index and writing nothing.
    *model* is the ``face_model`` setting."""
    from ..media import faceindex, faces                  # noqa: PLC0415
    from ..storage import db                              # noqa: PLC0415
    conn = db.connect(db_path)
    try:
        return faceindex.plan_regroup(conn, roots, model_id=faces.model_id_for(model))
    finally:
        db.close_all()
