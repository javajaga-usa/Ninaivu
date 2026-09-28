"""Which way up a photograph goes, when nothing in the file says.

A camera writes an orientation tag and Ninaivu has always honoured it. This
module is for everything else: scanned prints, photographs a messaging app
stripped on the way through, anything re-saved by an editor that dropped the
tag. There is nothing left to read, so the picture has to be looked at.

Three rules run through the whole of this.

**EXIF wins.** A tag written by the camera that took the photograph beats
anything inferred from pixels. Detection does not run at all when there is a
usable tag — not as an optimisation, but because guessing over a known answer
is how a library that was right becomes wrong in a new way.

**A guess is only made when it is a good one.** Rotating a photograph that was
already upright is the worse failure of the two: it breaks something that
worked, where leaving a sideways one alone leaves a problem its owner already
knows about and can fix in one click. Every detector here returns a confidence
and an unconfident one returns nothing.

**Every decision says where it came from.** ``exif``, ``faces``, ``ai``,
``manual`` or ``none``, carried alongside the rotation, so somebody looking at
a photograph that came out sideways can tell a bad guess from a bad tag and
correct the right thing.

Two detectors, cheapest and most certain first:

``faces``
    Haar cascades at each quarter turn. Faces are upright in the overwhelming
    majority of family photographs, so a face found at exactly one rotation is
    close to proof — and OpenCV is already installed for video thumbnails, so
    it costs nothing extra.

``ai``
    CLIP, when the AI tier is loaded, scoring each rotation against a handful
    of prompts. Weaker than a face and far slower, so it only runs when the
    faces found nothing, and it wants a wider margin before it will speak.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from PIL import Image

log = logging.getLogger(__name__)

__all__ = [
    "ROTATIONS", "Verdict", "rotation_for_exif", "decide", "apply",
    "MODEL_CONFIDENCE", "MODEL_TAG1_CONFIDENCE",
    "detect_rotation", "detect_by_faces", "detect_by_ai", "count_faces",
]

#: The only four answers there are. A photograph is never off by 3 degrees in
#: a way this can fix, and pretending otherwise invites a slow, lossy resample.
ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)

#: Below this the faces have not agreed enough to act on.
FACE_CONFIDENCE = 0.55

#: CLIP is a weaker signal than a found face, so it has to be clearer.
AI_CONFIDENCE = 0.72

#: The trained model, on a photograph whose file said nothing at all.
#:
#: It answers with a probability across the four turns, so this is a real
#: probability and not a scaled score: below it the model is genuinely unsure,
#: and an unsure answer changes nothing.
MODEL_CONFIDENCE = 0.80

#: …and on a photograph whose file says orientation 1.
#:
#: Tag 1 is the awkward one. It is what a camera writes to mean "this is the
#: right way up", and it is equally what a messaging app or an editor writes
#: after stripping the real answer — the two are indistinguishable in the file.
#: Tags 2-8 are never second-guessed, because only a camera writes those. This
#: one is, but only when the model is close to certain, because the cost of
#: being wrong is a photograph that was fine and is now on its side.
MODEL_TAG1_CONFIDENCE = 0.90

#: Detection runs on a small copy. A face that survives being shrunk to this
#: is a face; anything smaller was noise, and the whole pass costs milliseconds
#: instead of seconds on a 48-megapixel phone photograph.
WORK_SIZE = 640

#: Clockwise turn that puts each EXIF orientation the right way up. The
#: mirrored tags (2, 4, 5, 7) carry the same turn as their unmirrored twin —
#: the flip itself is handled by Pillow when the file is opened.
_EXIF_ROTATION = {1: 0, 2: 0, 3: 180, 4: 180, 5: 90, 6: 90, 7: 270, 8: 270}


@dataclass(frozen=True)
class Verdict:
    """A rotation, and the reason anybody should believe it."""

    rotation: int = 0
    source: str = "none"          # exif | model | faces | ai | manual | none
    confidence: float = 0.0

    @property
    def turns(self) -> bool:
        return self.rotation % 360 != 0


def rotation_for_exif(tag: Any) -> int:
    """The quarter turn an EXIF orientation asks for. 0 for anything unusable."""
    try:
        return _EXIF_ROTATION.get(int(tag), 0)
    except (TypeError, ValueError):
        return 0


def apply(img: Image.Image, rotation: int) -> Image.Image:
    """Turn an image clockwise by 0, 90, 180 or 270 degrees."""
    turn = rotation % 360
    if turn == 0:
        return img
    # Pillow rotates anticlockwise, so the angle is negated to keep every
    # number in this module a clockwise one.
    return img.rotate(-turn, expand=True)


def _working_copy(img: Image.Image) -> Image.Image:
    copy = img.convert("RGB")
    if max(copy.size) > WORK_SIZE:
        copy.thumbnail((WORK_SIZE, WORK_SIZE), Image.Resampling.BILINEAR)
    return copy


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------

_CASCADE_FILES = (
    "haarcascade_frontalface_default.xml",
    "haarcascade_profileface.xml",
)

#: One set of classifiers **per thread**.
#:
#: This is not an optimisation, it is a crash fix. ``detectMultiScale`` mutates
#: state inside the CascadeClassifier it is called on, and the scanner runs
#: ``build_record`` across a thread pool — so a single shared classifier is
#: several threads writing over each other inside OpenCV's C++, which segfaults
#: the interpreter outright. Loading a cascade is a few milliseconds and
#: happens once per worker thread, so the cost of getting this right is
#: nothing.
_local = threading.local()


_primed = False


def prime_on_main_thread() -> bool:
    """Start OpenCV's parallel scheduler from the main thread, once.

    OpenCV on Windows runs its parallel loops on Microsoft's Concurrency
    Runtime. If the first ``detectMultiScale`` of the process runs on any other
    thread — as it does in the scanner's pool — the process cannot terminate:
    every Python thread finishes, and ``python.exe`` never exits. That is why a
    stopped Ninaivu could hang, and why the test suite would not return after
    printing its summary. One tiny detection on the main thread first makes
    later ones on worker threads safe (OpenCV 4.14; ``setNumThreads`` does not
    help). Returns whether it primed; a no-op off the main thread or without
    OpenCV.
    """
    global _primed
    if _primed or threading.current_thread() is not threading.main_thread():
        return False
    try:
        import cv2
        import numpy as np
        classifier = cv2.CascadeClassifier(cv2.data.haarcascades + _CASCADE_FILES[0])
        if not classifier.empty():
            classifier.detectMultiScale(np.zeros((24, 24), np.uint8))
    except Exception:                                  # noqa: BLE001 - optional
        return False
    _primed = True
    return True


def _face_cascades() -> list[Any]:
    """This thread's classifiers. Empty when OpenCV is not installed."""
    cached = getattr(_local, "cascades", None)
    if cached is not None:
        return cached

    cascades: list[Any] = []
    try:
        import cv2
    except ImportError:
        _local.cascades = cascades
        return cascades

    # The scanner already runs one of these per pool thread; letting OpenCV
    # fan out again underneath would oversubscribe every core in the machine.
    try:
        cv2.setNumThreads(1)
    except Exception:                              # noqa: BLE001
        pass

    for name in _CASCADE_FILES:
        try:
            classifier = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            if not classifier.empty():
                cascades.append(classifier)
        except Exception:                          # noqa: BLE001
            continue
    _local.cascades = cascades
    return cascades


def find_faces(img: Image.Image, *, min_size: int = 40) -> list[tuple[int, int, int, int]]:
    """The face boxes OpenCV finds in this image, as it is. ``(x, y, w, h)``."""
    cascades = _face_cascades()
    if not cascades:
        return []
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    grey = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    grey = cv2.equalizeHist(grey)
    boxes: list[tuple[int, int, int, int]] = []
    for classifier in cascades:
        try:
            hits = classifier.detectMultiScale(
                grey, scaleFactor=1.1, minNeighbors=5,
                minSize=(min_size, min_size))
        except Exception:                          # noqa: BLE001
            continue
        boxes.extend((int(x), int(y), int(w), int(h)) for x, y, w, h in hits)
    return boxes


def count_faces(img: Image.Image, *, min_size: int = 40) -> int:
    """How many faces OpenCV finds in this image, as it is."""
    return len(find_faces(img, min_size=min_size))


#: A detected box has to be at least this much skin to count as a person.
#:
#: This is the check that makes the whole thing safe. A Haar cascade fires on
#: the corner of a colour chart, a patch of brickwork, a car grille — and on
#: the fixtures used to build this it did exactly that, at a size and with a
#: certainty indistinguishable from a real face several metres away. Measured
#: for skin, the two are not close: the real faces came out at 97-99 per cent,
#: the false positives at zero.
#:
#: The range itself is crude, and deliberately generous, because the cost of
#: the two mistakes is not symmetric. Refusing a real face means a photograph
#: is left exactly as its owner already has it. Accepting a false one means
#: Ninaivu turns a photograph that was perfectly fine.
SKIN_FRACTION = 0.25


def _skin_fraction(img: Image.Image) -> float:
    """Roughly how much of this crop could be skin, judged in YCrCb.

    Chrominance rather than RGB because it separates skin from lighting far
    better: the same face in shade and in sun sits in much the same place in
    Cr/Cb while its RGB moves all over.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        # Without OpenCV there are no cascades either, so nothing reaches here.
        return 1.0
    try:
        arr = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2YCrCb)
    except Exception:                              # noqa: BLE001
        return 1.0
    cr, cb = arr[:, :, 1], arr[:, :, 2]
    mask = (cr >= 133) & (cr <= 180) & (cb >= 77) & (cb <= 130)
    return float(mask.mean())


def face_boxes(img: Image.Image) -> list[tuple[int, int, int, int]]:
    """The detections in this image that actually look like people."""
    return [box for box in find_faces(img)
            if _skin_fraction(img.crop((box[0], box[1],
                                        box[0] + box[2], box[1] + box[3])))
            >= SKIN_FRACTION]


#: How much of the frame the faces must take up before they are evidence.
#:
#: A floor rather than a filter: the colour check above does the real work of
#: throwing out false positives, so this only has to exclude the genuinely
#: negligible — a face so small in the frame that turning the whole photograph
#: on its evidence would be absurd.
MIN_FACE_EVIDENCE = 0.0035

#: …and the winner has to beat the runner-up by this much. Faces turn up at
#: more than one rotation constantly — the profile cascade in particular fires
#: on an ear, an elbow, a patch of hair — so "found at exactly one rotation" is
#: not a test that survives real photographs. Which rotation has the *most*
#: face, highest up, is.
MIN_FACE_MARGIN = 1.6


def _height_weight(centre: float) -> float:
    """How much to believe a face at this height in the frame, 0 top, 1 bottom.

    People are near the top of a photograph far more often than the bottom —
    it is where heads are when a person is standing in a frame. This is what
    separates a photograph from the same photograph upside down, which the
    classifier itself is largely blind to.
    """
    if centre <= 0.5:
        return 1.0
    # Tapers to 0.4 at the very bottom rather than to zero: a face low in the
    # frame is unusual, not impossible.
    return max(0.4, 1.0 - (centre - 0.5) * 1.2)


def _face_evidence(boxes: list[tuple[int, int, int, int]],
                   size: tuple[int, int]) -> float:
    """How much face there is in this picture, weighted by where it sits.

    The sum of each face's share of the frame, discounted for sitting low.
    Summing rather than taking the best means a group photograph — several
    faces agreeing — outweighs one lucky rectangle, which is exactly the
    ordering that keeps a false positive from winning.
    """
    width, height = size
    if not width or not height:
        return 0.0
    area = float(width * height)
    total = 0.0
    for _, y, w, h in boxes:
        centre = (y + h / 2) / height
        total += (w * h) / area * _height_weight(centre)
    return total


def detect_by_faces(img: Image.Image) -> tuple[int, float]:
    """Find the quarter turn at which the faces make sense.

    Not "the rotation where a face was found" — that test does not survive real
    photographs. A frontal cascade finds the same head upside down about as
    often as the right way up, and the profile cascade fires on ears, elbows
    and hair at every angle. So all four rotations are scored on *how much*
    face there is and *how high up* it sits, and a verdict is only returned
    when one rotation is clearly ahead and there is enough face there to be
    talking about a person at all.
    """
    work = _working_copy(img)
    scores: dict[int, float] = {}
    for turn in ROTATIONS:
        turned = apply(work, turn)
        scores[turn] = _face_evidence(face_boxes(turned), turned.size)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]

    if top < MIN_FACE_EVIDENCE:
        # Either nothing was found, or what was found is too small to be a
        # person. A speck of a false positive must never turn a photograph.
        return 0, 0.0
    if second > 0 and top / second < MIN_FACE_MARGIN:
        # The picture looks about as face-like two ways up. Symmetrical
        # subjects do this, and acting on it turns photographs that were fine.
        return 0, 0.0

    margin = top / second if second > 0 else 4.0
    confidence = min(0.95, 0.65 + 0.08 * min(margin - 1.0, 3.0))
    return best, round(confidence, 3)


# ---------------------------------------------------------------------------
# CLIP
# ---------------------------------------------------------------------------

_UPRIGHT_PROMPTS = (
    "a photograph the right way up",
    "an upright photograph of people",
    "a correctly oriented photograph",
)
_SIDEWAYS_PROMPTS = (
    "a sideways photograph",
    "a photograph rotated ninety degrees",
    "an upside-down photograph",
)


def detect_by_ai(img: Image.Image, engine: Any) -> tuple[int, float]:
    """Score each quarter turn with CLIP. ``(rotation, confidence)``.

    Only speaks when one rotation is clearly ahead of the rest: the margin
    between the best and the runner-up has to be worth acting on, because CLIP
    will happily rank four rotations of the same photograph within a whisker of
    each other and the winner then means nothing.
    """
    if engine is None or not hasattr(engine, "encode_text"):
        return 0, 0.0
    try:
        import numpy as np
    except ImportError:
        return 0, 0.0

    try:
        upright_vecs = [engine.encode_text(p) for p in _UPRIGHT_PROMPTS]
        sideways_vecs = [engine.encode_text(p) for p in _SIDEWAYS_PROMPTS]
        if any(v is None for v in upright_vecs + sideways_vecs):
            return 0, 0.0
        good = np.mean(np.stack(upright_vecs), axis=0)
        bad = np.mean(np.stack(sideways_vecs), axis=0)

        work = _working_copy(img)
        scores: dict[int, float] = {}
        for turn in ROTATIONS:
            vector = _embed_image(engine, apply(work, turn))
            if vector is None:
                return 0, 0.0
            scores[turn] = float(vector @ good - vector @ bad)
    except Exception as exc:                       # noqa: BLE001
        log.debug("orientation: CLIP scoring failed — %s", exc)
        return 0, 0.0

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, runner_up = ranked[0], ranked[1]
    spread = (max(scores.values()) - min(scores.values())) or 1e-6
    margin = (best[1] - runner_up[1]) / spread
    if margin < 0.35:
        return 0, 0.0
    return best[0], min(0.9, 0.6 + margin * 0.4)


def _embed_image(engine: Any, img: Image.Image):
    """A normalised CLIP vector for one image, or None."""
    for name in ("encode_image", "embed_image"):
        fn = getattr(engine, name, None)
        if fn is None:
            continue
        vector = fn(img)
        if vector is None:
            return None
        try:
            import numpy as np
            vector = np.asarray(vector, dtype="float32").ravel()
            norm = float(np.linalg.norm(vector))
            return vector / norm if norm else None
        except Exception:                          # noqa: BLE001
            return None
    return None


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def detect_rotation(img: Image.Image, engine: Any = None) -> tuple[int, float]:
    """Faces first, then CLIP. ``(rotation, confidence)``, 0 when unsure."""
    rotation, confidence = detect_by_faces(img)
    if confidence >= FACE_CONFIDENCE:
        return rotation, confidence
    if engine is not None:
        rotation, confidence = detect_by_ai(img, engine)
        if confidence >= AI_CONFIDENCE:
            return rotation, confidence
    return 0, 0.0


def decide(img: Image.Image, *, exif_orientation: Any = None,
           engine: Any = None, enabled: bool = True,
           use_model: bool = True) -> Verdict:
    """Which way up this photograph goes, and on whose authority.

    The order is the whole design, and there are now three tiers rather than
    two:

    *A camera's own answer wins.* Tags 2-8 can only have been written by
    something that knew which way round the sensor was, and are taken as
    final without a pixel being looked at.

    *Tag 1 is not quite an answer.* It means "upright" — but it is also what
    is left behind when an application strips the real tag, which is why half
    a library can carry it and still be full of sideways photographs. The
    trained model may overrule it, and only when it is close to certain.

    *Silence is an invitation.* A file with no tag at all is what the
    detectors exist for, at the ordinary confidence bar.
    """
    tag_rotation = rotation_for_exif(exif_orientation)
    try:
        tag = int(exif_orientation)
    except (TypeError, ValueError):
        tag = None
    tagged = tag in _EXIF_ROTATION

    # A real camera answer. Never second-guessed — see the module docstring.
    if tagged and tag != 1:
        return Verdict(tag_rotation, "exif", 1.0)
    if not enabled:
        return Verdict()

    floor = MODEL_TAG1_CONFIDENCE if tag == 1 else MODEL_CONFIDENCE

    if use_model:
        try:
            from . import orientnet                   # noqa: PLC0415
            # No folder: the model lives wherever `orientnet.configure` was
            # pointed at start-up, with every other AI model.
            rotation, confidence = orientnet.predict(img)
        except Exception as exc:                      # noqa: BLE001
            log.debug("orientation: model unavailable — %s", exc)
            rotation, confidence = 0, 0.0
        if confidence:
            if rotation and confidence >= floor:
                return Verdict(rotation % 360, "model", round(confidence, 3))
            # The model ran and was either sure it is upright, or unsure. Either
            # way it has seen the whole picture, which is more than the face
            # detector can claim — so its silence ends the question rather than
            # handing a photograph on to a weaker opinion.
            return Verdict()

    # No model on disk: the original face and CLIP path, for untagged files
    # only. A tag of 1 is left alone here, because these detectors are not
    # steady enough to overrule even a weak stated answer.
    if tag == 1:
        return Verdict()

    try:
        rotation, confidence = detect_rotation(img, engine)
    except Exception as exc:                       # noqa: BLE001
        # One unreadable file must never stop a scan, and must certainly never
        # rotate anything on the strength of an exception.
        log.debug("orientation: detection failed — %s", exc)
        return Verdict()

    if not rotation or confidence < FACE_CONFIDENCE:
        return Verdict()
    source = "faces" if confidence >= FACE_CONFIDENCE and confidence <= 0.95 else "ai"
    return Verdict(rotation % 360, source, round(confidence, 3))
