"""Face detection and recognition.

Two small ONNX models, both run through OpenCV's own DNN backend, both
downloaded once into the state directory:

``YuNet``
    The detector (~230 KB). Returns a box, five landmarks and a confidence
    for every face in a frame.

``SFace``
    The recogniser (~37 MB). Turns one aligned 112x112 face into a 128-value
    embedding. Two embeddings of the same person point in nearly the same
    direction, so a cosine similarity compares them.

Neither is a new dependency: OpenCV is already required for video poster
frames and orientation detection, and ``FaceDetectorYN`` / ``FaceRecognizerSF``
ship inside it. If OpenCV is missing, or the models have not been fetched, the
engine reports itself unavailable and every caller carries on without faces —
the same contract :mod:`ninaivu.ai` keeps.

A second pair can take their place: InsightFace's SCRFD detector and ArcFace
recogniser (:mod:`ninaivu.media.faces_arcface`, run by ONNX Runtime), chosen
with the ``face_model`` setting once downloaded from the AI models page. The
engine drives either through one shape — detector rows of a box, five
landmarks and a score; a recogniser that aligns and embeds one row — so the
quality gate, the crops and everything downstream are shared. ArcFace's
embeddings are 512 values to SFace's 128, and each row in the index carries
the model that made it.

Nothing here decides *who* a face is. Detection produces embeddings;
:mod:`ninaivu.facematch` holds the policy that turns them into people, and it
is deliberately kept in a separate module with no OpenCV import so the
matching rules can be tested without a model on disk.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("ninaivu.faces")

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is a hard requirement in practice
    np = None  # type: ignore

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional at runtime
    cv2 = None  # type: ignore


#: Bumped when detection changes in a way that invalidates stored faces.
#: ``assets.face_version`` is compared against it, exactly as ``ai_version``
#: is compared against ``AI_VERSION``, so a bump re-runs the pass.
FACE_VERSION = 1

MODEL_ID = "yunet-2023mar/sface-2021dec"

#: Where the models come from and what they must hash to. The checksum is the
#: point: a truncated download or a substituted file is refused rather than
#: loaded, because a corrupt recogniser does not fail loudly — it quietly
#: returns embeddings that match nobody, and the symptom is "face grouping
#: stopped working" weeks later with no error anywhere.
MODELS: dict[str, dict[str, str]] = {
    "detector": {
        "file": "face_detection_yunet_2023mar.onnx",
        "url": ("https://media.githubusercontent.com/media/opencv/opencv_zoo/"
                "main/models/face_detection_yunet/"
                "face_detection_yunet_2023mar.onnx"),
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    },
    "recognizer": {
        "file": "face_recognition_sface_2021dec.onnx",
        "url": ("https://media.githubusercontent.com/media/opencv/opencv_zoo/"
                "main/models/face_recognition_sface/"
                "face_recognition_sface_2021dec.onnx"),
        "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    },
}

EMBED_DIM = 128

#: The other pair the engine can run: InsightFace's SCRFD detector and
#: ArcFace recogniser (:mod:`ninaivu.media.faces_arcface`), chosen with the
#: ``face_model`` setting once downloaded from the AI models page. Its
#: embeddings are 512 values, so a row's model says how to read its BLOB.
ARCFACE_MODEL_ID = "scrfd-10g/arcface-w600k-r50"
EMBED_DIMS: dict[str, int] = {MODEL_ID: EMBED_DIM, ARCFACE_MODEL_ID: 512}
#: The ``face_model`` setting's values, and the model each stands for.
CHOICES: dict[str, str] = {"sface": MODEL_ID, "arcface": ARCFACE_MODEL_ID}
#: The AI models page's entry for each choice (``model_catalog.MODELS``).
CATALOG_ENTRY: dict[str, str] = {"sface": "faces", "arcface": "faces-arcface"}


def model_id_for(choice: str | None) -> str:
    """The model id the ``face_model`` setting names; SFace for anything odd."""
    return CHOICES.get(str(choice or "sface").lower(), MODEL_ID)


def embed_dim_of(model_id: str | None) -> int:
    return EMBED_DIMS.get(str(model_id or ""), EMBED_DIM)

# --- detection tuning ------------------------------------------------------

#: Longest edge the detector sees. Faces are found on a downscaled copy for
#: speed, then the box and landmarks are scaled back up and the *full
#: resolution* pixels are what gets aligned and embedded — detecting small and
#: embedding large is most of the difference between grouping that works on a
#: 2004 holiday snap and grouping that does not.
DETECT_MAX_DIM = 1600

#: Detector confidence below which a box is not a face worth keeping.
MIN_DET_SCORE = 0.75

#: A face narrower than this many pixels *in the original* carries too little
#: detail to identify. Below roughly 50px SFace embeddings start to cluster by
#: image quality rather than by person, which is how a library ends up with
#: one enormous cluster of blurry strangers.
MIN_FACE_PX = 50

#: Variance of the Laplacian over the aligned crop. Motion blur and heavy JPEG
#: smoothing both collapse it, and both produce embeddings that drift.
MIN_SHARPNESS = 12.0

#: Faces per image beyond which we stop. A crowd shot contributes nothing to
#: identifying anybody and costs an embedding each.
MAX_FACES_PER_IMAGE = 32


@dataclass
class DetectedFace:
    """One face found in one image, ready to be stored."""

    bbox: tuple[int, int, int, int]          # x, y, w, h in original pixels
    landmarks: list[tuple[float, float]]     # 5 points, original pixels
    det_score: float
    sharpness: float
    quality: float
    embedding: "np.ndarray"                  # float32, L2-normalised, 128-d
    crop: Any = field(default=None, repr=False)  # BGR ndarray, 112x112

    def as_row(self) -> dict[str, Any]:
        return {
            "bbox": list(self.bbox),
            "landmarks": [list(p) for p in self.landmarks],
            "det_score": float(self.det_score),
            "sharpness": float(self.sharpness),
            "quality": float(self.quality),
        }


# ---------------------------------------------------------------------------
# Models on disk
# ---------------------------------------------------------------------------

def models_dir(state_dir: Path | None = None) -> Path:
    """Where the detector and recogniser live: with every other AI model.

    They used to sit in the state folder, which meant the models were split
    across two places and moving Ninaivu to another machine left the face
    grouping behind. *state_dir* is accepted and ignored so that callers
    holding one do not all have to change at once.
    """
    from . import model_catalog                         # noqa: PLC0415

    return model_catalog.models_root()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_status(state_dir: Path | None = None, model: str = "sface") -> dict[str, Any]:
    """What is on disk for the chosen pair, without downloading anything."""
    directory = models_dir(state_dir)
    out: dict[str, Any] = {"dir": str(directory), "models": {},
                           "choice": "arcface" if model_id_for(model) == ARCFACE_MODEL_ID else "sface"}
    ready = True
    for key, spec in _model_files(model).items():
        path = directory / spec["file"]
        present = path.exists() and path.stat().st_size > 0
        out["models"][key] = {
            "file": spec["file"],
            "present": present,
            "bytes": path.stat().st_size if present else 0,
            "url": spec.get("url", ""),
        }
        ready = ready and present
    out["ready"] = ready
    out["opencv"] = _opencv_support()
    return out


def _model_files(model: str) -> dict[str, dict[str, str]]:
    """``{"detector": {...}, "recognizer": {...}}`` for a ``face_model`` choice."""
    if model_id_for(model) != ARCFACE_MODEL_ID:
        return MODELS
    from . import faces_arcface                              # noqa: PLC0415
    from . import model_catalog                              # noqa: PLC0415

    folder = model_catalog.MODELS["faces-arcface"]["files"][0]["folder"]
    return {
        "detector": {"file": f"{folder}/{faces_arcface.DETECTOR_FILE}"},
        "recognizer": {"file": f"{folder}/{faces_arcface.RECOGNIZER_FILE}"},
    }


def _opencv_support() -> dict[str, Any]:
    if cv2 is None:
        return {"available": False, "reason": "opencv is not installed"}
    missing = [n for n in ("FaceDetectorYN", "FaceRecognizerSF")
               if not hasattr(cv2, n)]
    if missing:
        return {"available": False,
                "version": getattr(cv2, "__version__", "?"),
                "reason": f"this OpenCV build has no {', '.join(missing)}"}
    return {"available": True, "version": getattr(cv2, "__version__", "?")}


def fetch_models(state_dir: Path | None = None, *, force: bool = False,
                 progress: Any = None, timeout: float = 60.0) -> dict[str, Any]:
    """Download whatever is missing. Safe to call repeatedly.

    Downloads land on a temporary name and are hashed before being renamed
    into place, so an interrupted fetch leaves no half-file that looks valid
    on the next start.
    """
    directory = models_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"downloaded": [], "kept": [], "errors": []}

    for spec in MODELS.values():
        path = directory / spec["file"]
        if path.exists() and path.stat().st_size > 0 and not force:
            result["kept"].append(spec["file"])
            continue
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            if progress:
                progress(f"Downloading {spec['file']}…")
            request = urllib.request.Request(
                spec["url"], headers={"User-Agent": "Ninaivu"})
            with urllib.request.urlopen(request, timeout=timeout) as response, \
                    open(tmp, "wb") as handle:
                shutil.copyfileobj(response, handle, 1 << 20)
            actual = _sha256(tmp)
            if actual != spec["sha256"]:
                tmp.unlink(missing_ok=True)
                result["errors"].append(
                    f"{spec['file']}: checksum mismatch (got {actual[:12]}…)")
                continue
            tmp.replace(path)
            result["downloaded"].append(spec["file"])
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            tmp.unlink(missing_ok=True)
            result["errors"].append(f"{spec['file']}: {exc}")

    result["ready"] = all(
        (directory / spec["file"]).exists() for spec in MODELS.values())
    return result


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class FaceEngine:
    """Detects faces and embeds them. Thread-confined by an internal lock.

    OpenCV's DNN objects are not safe to call from several threads at once,
    and the detector additionally carries mutable input-size state between
    ``setInputSize`` and ``detect``. One lock around both is far cheaper than
    the class of bug where two scanner threads interleave and every face in
    one image is embedded from another image's pixels.
    """

    model_id = MODEL_ID
    embed_dim = EMBED_DIM

    def __init__(self, state_dir: Path, model: str | None = None) -> None:
        self.state_dir = Path(state_dir)
        #: Which pair: ``"sface"`` (OpenCV's, the default) or ``"arcface"``
        #: (InsightFace's, the ``face_model`` setting once downloaded).
        self.model = "arcface" if model_id_for(model) == ARCFACE_MODEL_ID else "sface"
        self.model_id = model_id_for(self.model)
        self.embed_dim = embed_dim_of(self.model_id)
        self.min_det_score = MIN_DET_SCORE
        self._lock = threading.Lock()
        self._detector = None
        self._recognizer = None
        self.unavailable_reason: str | None = None
        #: True while the only thing missing is the model files. The engine
        #: is made once and kept — by the scan, the Faces page and Straighten
        #: alike — so without looking again a download made while Ninaivu
        #: ran was not used until a restart, while the console said "ready".
        self._waiting_for_files = False
        self._load_lock = threading.Lock()
        self._load()

    # -- lifecycle --------------------------------------------------------
    def _load(self) -> None:
        support = _opencv_support()
        if not support["available"]:
            self.unavailable_reason = support["reason"]
            return
        if np is None:
            self.unavailable_reason = "numpy is not installed"
            return
        if self.model == "arcface":
            self._load_arcface()
            return
        directory = models_dir(self.state_dir)
        detector_path = directory / MODELS["detector"]["file"]
        recognizer_path = directory / MODELS["recognizer"]["file"]
        missing = [p.name for p in (detector_path, recognizer_path)
                   if not p.exists()]
        if missing:
            self.unavailable_reason = (
                "model files not downloaded yet: " + ", ".join(missing))
            self._waiting_for_files = True
            return
        self._waiting_for_files = False
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(detector_path), "", (320, 320),
                MIN_DET_SCORE, 0.3, MAX_FACES_PER_IMAGE)
            self._recognizer = cv2.FaceRecognizerSF.create(
                str(recognizer_path), "")
        except Exception as exc:  # noqa: BLE001
            self._detector = self._recognizer = None
            self.unavailable_reason = f"could not load the models: {exc}"

    def _load_arcface(self) -> None:
        """InsightFace's pair, through ONNX Runtime. One object serves as
        both detector and recogniser; the rest of the engine asks it through
        :meth:`_detect_rows` and :meth:`_align_and_embed`."""
        import importlib.util                                 # noqa: PLC0415

        if importlib.util.find_spec("onnxruntime") is None:
            self.unavailable_reason = ("ONNX Runtime is not installed (the console's Extras "
                                       "page, or requirements/requirements-ai-local.txt)")
            return
        from . import faces_arcface                              # noqa: PLC0415

        directory = models_dir(self.state_dir)
        files = _model_files("arcface")
        missing = [Path(spec["file"]).name for spec in files.values()
                   if not (directory / spec["file"]).exists()]
        if missing:
            self.unavailable_reason = ("the ArcFace face models are not downloaded yet "
                                       "(AI models page): " + ", ".join(missing))
            self._waiting_for_files = True
            return
        self._waiting_for_files = False
        try:
            pair = faces_arcface.Recogniser(directory / Path(files["detector"]["file"]).parent)
        except Exception as exc:  # noqa: BLE001
            self._detector = self._recognizer = None
            self.unavailable_reason = f"could not load the ArcFace models: {exc}"
            return
        self._detector = self._recognizer = pair
        self.min_det_score = faces_arcface.MIN_DET_SCORE

    @property
    def available(self) -> bool:
        if self._detector is None and self._waiting_for_files:
            self._load_if_downloaded()
        return self._detector is not None and self._recognizer is not None

    def _load_if_downloaded(self) -> None:
        """Load the models if they have arrived since the last look.

        Two ``exists`` calls when they have not, so asking costs nothing. Its
        own lock, not ``_lock``: :meth:`detect` asks for ``available`` while
        holding that one.
        """
        directory = models_dir(self.state_dir)
        if not all((directory / spec["file"]).exists() for spec in _model_files(self.model).values()):
            return
        with self._load_lock:
            if self._detector is None and self._waiting_for_files:
                self._load()

    @property
    def info(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "model": self.model_id,
            "choice": self.model,
            "reason": self.unavailable_reason,
            "opencv": _opencv_support(),
        }

    def close(self) -> None:
        with self._lock:
            self._waiting_for_files = False
            self._detector = self._recognizer = None

    # -- detection --------------------------------------------------------
    def detect(self, image: "np.ndarray") -> list[DetectedFace]:
        """Find and embed every usable face in one BGR image."""
        if not self.available or image is None or image.size == 0:
            return []
        height, width = image.shape[:2]
        if height < MIN_FACE_PX or width < MIN_FACE_PX:
            return []

        scale = 1.0
        longest = max(height, width)
        if longest > DETECT_MAX_DIM:
            scale = DETECT_MAX_DIM / float(longest)
        if scale < 1.0:
            small = cv2.resize(
                image, (max(1, int(round(width * scale))),
                        max(1, int(round(height * scale)))),
                interpolation=cv2.INTER_AREA)
        else:
            small = image

        with self._lock:
            if not self.available:
                return []
            try:
                raw = self._detect_rows(small, self.min_det_score)
            except Exception as exc:  # noqa: BLE001
                log.debug("detector failed: %s", exc)
                return []
            if raw is None or len(raw) == 0:
                return []

            faces: list[DetectedFace] = []
            inverse = 1.0 / scale if scale else 1.0
            for row in raw[:MAX_FACES_PER_IMAGE]:
                face = self._embed_one(image, row, inverse, width, height)
                if face is not None:
                    faces.append(face)
        return faces

    def _detect_rows(self, small, min_score: float):
        """The detector's rows for one (downscaled) BGR image: fifteen values
        each, ``x, y, w, h``, five landmarks and the score, in its pixels."""
        if self.model == "arcface":
            return self._detector.detect(small, min_score, MAX_FACES_PER_IMAGE)
        self._detector.setInputSize((small.shape[1], small.shape[0]))
        self._detector.setScoreThreshold(float(min_score))
        try:
            _, raw = self._detector.detect(small)
        finally:
            self._detector.setScoreThreshold(MIN_DET_SCORE)
        return raw

    def _align_and_embed(self, image, full):
        """One detector row (in the image's own pixels) → the 112x112 aligned
        crop and its raw feature, or ``(None, None)``."""
        if self.model == "arcface":
            return self._recognizer.embed(image, full)
        aligned = self._recognizer.alignCrop(image, full.reshape(1, -1))
        if aligned is None or aligned.size == 0:
            return None, None
        return aligned, None

    def _embed_one(self, image, row, inverse: float,
                   width: int, height: int) -> DetectedFace | None:
        score = float(row[14])
        if score < self.min_det_score:
            return None

        # Map the detection back onto the full-resolution pixels. Only the
        # first fourteen values are coordinates; the fifteenth is the score
        # and scaling it would be nonsense.
        full = row.astype("float32").copy()
        full[:14] *= inverse

        x, y, w, h = (float(full[0]), float(full[1]),
                      float(full[2]), float(full[3]))
        if min(w, h) < MIN_FACE_PX:
            return None
        # A box the detector ran off the edge of the frame cannot be aligned.
        if x < -w * 0.25 or y < -h * 0.25 or x > width or y > height:
            return None

        try:
            aligned, feature = self._align_and_embed(image, full)
        except Exception as exc:  # noqa: BLE001
            log.debug("alignCrop failed: %s", exc)
            return None
        if aligned is None or aligned.size == 0:
            return None

        sharpness = _sharpness(aligned)
        if sharpness < MIN_SHARPNESS:
            return None

        if feature is None:
            # SFace embeds the aligned crop in a second step.
            try:
                feature = self._recognizer.feature(aligned)
            except Exception as exc:  # noqa: BLE001
                log.debug("feature failed: %s", exc)
                return None
        vector = normalise(np.asarray(feature, dtype="float32").reshape(-1))
        if vector is None or vector.shape[0] != self.embed_dim:
            return None

        landmarks = [(float(full[4 + i * 2]), float(full[5 + i * 2]))
                     for i in range(5)]
        bbox = (int(round(x)), int(round(y)), int(round(w)), int(round(h)))
        return DetectedFace(
            bbox=bbox,
            landmarks=landmarks,
            det_score=score,
            sharpness=sharpness,
            quality=face_quality(min(w, h), score, sharpness, det_min=self.min_det_score),
            embedding=vector,
            crop=aligned,
        )

    def detect_image(self, img) -> list[DetectedFace]:
        """Convenience wrapper taking a PIL image."""
        array = pil_to_bgr(img)
        return self.detect(array) if array is not None else []

    def locate(self, image: "np.ndarray", *, min_px: int = 24,
               max_dim: int = 2400, min_score: float = 0.6) -> list[dict[str, Any]]:
        """Where every face is, and nothing else: a box and five landmarks.

        :meth:`detect` is built for recognising people, so it embeds each face
        and throws away the soft, small and half-turned ones. Retouching wants
        the opposite: every face in a family photograph, including the blurry
        one at the back and the child looking away, because each of them is a
        person who may want to be brightened. So this keeps whatever the
        detector will stand behind at a lower bar, at a larger working size
        (a face 30 pixels across in a group photograph is still a face), and
        does no embedding at all.

        Returns ``{"box": (x, y, w, h), "landmarks": [(x, y) × 5], "score": s}``
        in the image's own pixels, left to right. The landmarks are the
        subject's right eye, left eye, nose tip, right and left mouth corners,
        so the first of them is on the *left* of the picture.
        """
        if not self.available or image is None or image.size == 0:
            return []
        height, width = image.shape[:2]
        if height < min_px or width < min_px:
            return []
        longest = max(height, width)
        scale = min(1.0, max_dim / float(longest))
        small = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA) if scale < 1.0 else image
        with self._lock:
            if self._detector is None:
                return []
            try:
                raw = self._detect_rows(small, float(min_score))
            except Exception as exc:  # noqa: BLE001
                log.debug("locate failed: %s", exc)
                return []
        if raw is None or len(raw) == 0:
            return []
        found: list[dict[str, Any]] = []
        for row in raw[:MAX_FACES_PER_IMAGE]:
            values = [float(v) for v in row[:14]]
            x, y, w, h = (v / scale for v in values[:4])
            if min(w, h) < min_px:
                continue
            points = [(values[4 + i * 2] / scale, values[5 + i * 2] / scale) for i in range(5)]
            found.append({"box": (x, y, w, h), "landmarks": points, "score": float(row[14])})
        found.sort(key=lambda face: face["box"][0])
        return found


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalise(vector) -> "np.ndarray | None":
    """L2-normalise so a dot product is the cosine similarity."""
    if np is None or vector is None:
        return None
    vec = np.asarray(vector, dtype="float32").reshape(-1)
    norm = float(np.linalg.norm(vec))
    if not norm or not np.isfinite(norm):
        return None
    return (vec / norm).astype("float32")


def _sharpness(crop) -> float:
    try:
        grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(grey, cv2.CV_64F).var())
    except Exception:  # noqa: BLE001
        return 0.0


def face_quality(size_px: float, det_score: float, sharpness: float,
                 det_min: float = MIN_DET_SCORE) -> float:
    """One 0–1 number ranking how much this face can be trusted.

    Used for two things: choosing which face represents a person, and deciding
    the order faces are clustered in, so that a cluster forms around its
    clearest members instead of around whichever blurry crop came first.
    *det_min* is the detector's own floor, so a modest scorer (SCRFD) is not
    marked down against a generous one (YuNet).
    """
    size = min(1.0, max(0.0, (size_px - MIN_FACE_PX) / 200.0))
    score = min(1.0, max(0.0, (det_score - det_min) / max(1e-6, 1.0 - det_min)))
    sharp = min(1.0, max(0.0, (sharpness - MIN_SHARPNESS) / 120.0))
    return round(0.45 * size + 0.30 * score + 0.25 * sharp, 4)


def pil_to_bgr(img):
    """PIL image → the BGR ndarray OpenCV expects."""
    if np is None or img is None:
        return None
    try:
        rgb = img.convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    array = np.asarray(rgb, dtype="uint8")
    if array.ndim != 3 or array.shape[2] != 3:
        return None
    return array[:, :, ::-1].copy()


def pack(vector) -> bytes:
    """Embedding → BLOB."""
    return np.asarray(vector, dtype="float32").tobytes()


def unpack(blob: bytes, dim: int | None = None) -> "np.ndarray | None":
    """BLOB → embedding, or ``None`` if it is the wrong length.

    *dim* is the length wanted (the engine's ``embed_dim``); without it any
    length a known model writes is accepted. The matcher always says which:
    SFace's 128 values and ArcFace's 512 are not comparable, and while a
    library is being looked at again after a switch both are in the table.
    """
    if np is None or not blob:
        return None
    vec = np.frombuffer(blob, dtype="float32")
    wanted = {int(dim)} if dim else set(EMBED_DIMS.values())
    return vec if vec.shape[0] in wanted else None


def unpack_many(blobs: Iterable[bytes], dim: int = EMBED_DIM) -> "np.ndarray":
    """Several BLOBs → one (n, *dim*) matrix, skipping rows of another length."""
    rows = [v for v in (unpack(b, dim) for b in blobs) if v is not None]
    if not rows:
        return np.zeros((0, int(dim)), dtype="float32")
    return np.vstack(rows).astype("float32")


def load_image_for_faces(path: Path, rotation: int = 0):
    """Open an original the right way up, as BGR pixels.

    EXIF orientation is applied first, then whatever rotation an admin or the
    orientation detector saved — the same two steps the thumbnails were baked
    with, so a stored face box lines up with what the viewer displays.
    """
    from . import media  # noqa: PLC0415 - avoids an import cycle at module load

    try:
        with media._open_oriented(path) as img:
            turn = int(rotation or 0) % 360
            if turn:
                img = img.rotate(-turn, expand=True)
            return pil_to_bgr(img)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not open %s: %s", path, exc)
        return None


def write_crop(crop, path: Path) -> bool:
    """Save a 112x112 face crop as the thumbnail the console shows."""
    if crop is None or cv2 is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return bool(cv2.imwrite(str(path), crop,
                                [int(cv2.IMWRITE_JPEG_QUALITY), 90]))
    except Exception as exc:  # noqa: BLE001
        log.debug("could not write crop %s: %s", path, exc)
        return False
