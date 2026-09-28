"""Reading the words that are already in the picture.

A family library is full of text nobody typed: a shop sign, a menu, the
whiteboard from a meeting, a screenshot of a train ticket, the back of a
postcard. None of it is searchable, because none of it is metadata — and the
photograph of the ticket is exactly the one you go looking for.

The engine is PP-OCR through ONNX Runtime, which ships as an ordinary wheel
with its own models inside it. Nothing is downloaded at run time and nothing
leaves the machine — the same rule the rest of Ninaivu keeps. It is optional:
without it installed, every function here says so and the scan skips the pass
rather than failing.

Two package names answer to this. `rapidocr` is the current one and the only
one that installs on Python 3.13 and later; `rapidocr_onnxruntime` is what it
used to be called, still fine on older interpreters, and returns its results
in a different shape. Both are accepted, and the difference is flattened
here rather than left for the caller.

Two judgements are made here rather than left to the caller. Low-confidence
lines are dropped, because OCR run over photographs of trees and carpets
produces confident-looking nonsense that would then be searchable. And the
text kept per photograph is capped, because a scan of a page of newsprint
would otherwise put an entire article into a column meant for a sign.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from PIL import Image

try:
    import numpy as np
except Exception:  # pragma: no cover - optional
    np = None  # type: ignore

log = logging.getLogger("ninaivu.ocr")

#: Bumped when the reading changes in a way worth re-running a library for.
OCR_VERSION = 1

#: Long edge the image is reduced to before reading. Text large enough for a
#: person to have photographed on purpose survives this; going bigger costs
#: seconds per photograph for words nobody was trying to keep.
READ_EDGE = 1600

#: Below this the detector is guessing. Photographs of foliage, brickwork and
#: carpet all produce "words" with middling scores, and one wrong line in the
#: index is worse than a missing one — it makes a search return a photograph
#: with no connection to what was typed.
MIN_SCORE = 0.5

#: Characters kept per photograph.
MAX_CHARS = 4000

_engine = None
_engine_lock = threading.Lock()
_unavailable = False


def _reader_class():
    """The engine class from whichever package is installed, or None."""
    try:
        from rapidocr import RapidOCR  # noqa: PLC0415
        return RapidOCR
    except Exception:  # noqa: BLE001 - not installed, or a broken build
        pass
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
        return RapidOCR
    except Exception:  # noqa: BLE001
        return None


def available() -> bool:
    """Whether text reading can run at all here."""
    return np is not None and _reader_class() is not None


def engine() -> Any | None:
    """The reader, loaded once and shared.

    Loading costs a fraction of a second and a few tens of megabytes, so it
    happens on first use rather than at import: a household that never turns
    OCR on never pays for it.
    """
    global _engine, _unavailable
    if _engine is not None or _unavailable:
        return _engine
    with _engine_lock:
        if _engine is not None or _unavailable:
            return _engine
        reader = _reader_class()
        if reader is None:
            _unavailable = True
            return None
        # Its loggers do not exist until it builds them, so there is nothing to
        # quieten in advance — hence muting INFO for the fraction of a second
        # the engine takes to load, then quietening the loggers it left behind.
        was_disabled = logging.root.manager.disable
        try:
            logging.disable(logging.INFO)
            _engine = reader()
        except Exception as exc:  # noqa: BLE001
            log.info("text reading is unavailable: %s", exc)
            _unavailable = True
        finally:
            logging.disable(was_disabled)
            _quieten()
    return _engine


def _quieten() -> None:
    """Stop the reader narrating itself into Ninaivu's window.

    It announces every model it loads at INFO, in colour, and warns on every
    photograph that turns out to have no text in it — which is most of a
    family library. Ninaivu reports its own progress; this is somebody else's
    debug output, and there is a lot of it.

    Its loggers are created as the engine loads, so this runs on both sides
    of that.
    """
    for name in list(logging.root.manager.loggerDict):
        if "rapidocr" in name.lower():
            logging.getLogger(name).setLevel(logging.ERROR)


def _prepare(source: Image.Image | str | Path) -> Any | None:
    """A bounded RGB array, whatever the caller had."""
    try:
        img = source if isinstance(source, Image.Image) else Image.open(source)
        with img:
            work = img.convert("RGB")
            work.thumbnail((READ_EDGE, READ_EDGE), Image.Resampling.LANCZOS)
            return np.asarray(work)
    except Exception as exc:  # noqa: BLE001 - one unreadable file, not a scan
        log.debug("could not open %s for reading: %s", source, exc)
        return None


def _pairs(result: Any) -> list[tuple[str, float]]:
    """(text, score) out of either package's answer.

    The current one returns an object with parallel ``txts`` and ``scores``;
    the older one returns a list of ``(box, text, score)`` rows, or a bare
    ``None`` when it found nothing. The boxes are dropped either way —
    nothing in Ninaivu draws them, and coordinates no screen uses are weight.
    """
    if result is None:
        return []
    texts = getattr(result, "txts", None)
    if texts is not None:
        raw = getattr(result, "scores", None) or ()
        out = []
        for i, text in enumerate(texts):
            try:
                score = float(raw[i])
            except (IndexError, TypeError, ValueError):
                score = 0.0
            out.append((str(text), score))
        return out
    if isinstance(result, tuple):          # (rows, elapsed)
        result = result[0]
    out = []
    for entry in result or []:
        try:
            out.append((str(entry[1]), float(entry[2])))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def read(source: Image.Image | str | Path, *, min_score: float = MIN_SCORE,
         max_chars: int = MAX_CHARS) -> dict[str, Any]:
    """Read the text in one image.

    Returns ``{"text": str, "lines": int, "score": float}``. An image with no
    text in it, and an engine that is not installed, both return empty text —
    the caller tells them apart with :func:`available` if it needs to.
    """
    empty = {"text": "", "lines": 0, "score": 0.0}
    reader = engine()
    if reader is None:
        return empty
    array = _prepare(source)
    if array is None:
        return empty

    try:
        result = reader(array)
    except Exception as exc:  # noqa: BLE001
        log.debug("reading failed: %s", exc)
        return empty

    lines: list[str] = []
    scores: list[float] = []
    for text, score in _pairs(result):
        text = (text or "").strip()
        if not text or score < min_score:
            continue
        lines.append(text)
        scores.append(score)

    if not lines:
        return empty

    joined = "\n".join(lines)
    if len(joined) > max_chars:
        # Cut on a line boundary: half a word helps nobody searching.
        kept: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) + 1 > max_chars:
                break
            kept.append(line)
            used += len(line) + 1
        joined = "\n".join(kept)
        lines = kept

    return {
        "text": joined,
        "lines": len(lines),
        "score": round(sum(scores) / len(scores), 3) if scores else 0.0,
    }
