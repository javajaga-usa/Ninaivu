"""The ArcFace face models, run by ONNX Runtime.

InsightFace's ``buffalo_l`` pack holds the two models used here, downloaded
from the AI models page (catalogue entry ``faces-arcface``):

``SCRFD-10G``
    The detector (``det_10g.onnx``, 17 MB). Looks at the photograph as a
    640-pixel square, letterboxed, and answers with a box, five landmarks and
    a score for every face at three scales. Markedly better than YuNet at
    small, turned and shaded faces.

``ArcFace ResNet-50``
    The recogniser (``w600k_r50.onnx``, 174 MB, trained on WebFace600K).
    Turns one aligned 112x112 face into a 512-value embedding. Where SFace
    puts two frames of the same person at 0.77 and strangers at 0.04 to 0.18,
    this one places a family far more finely: siblings, parents and children,
    and one child across ten years, which is what a family library is full of.

Both are the same shape as the OpenCV pair in :mod:`ninaivu.media.faces`:
a detector that gives rows of ``x, y, w, h, five landmarks, score`` and a
recogniser that aligns and embeds one such row. :class:`FaceEngine` drives
either through that one shape, so everything downstream (the quality gate,
the crops, the matching policy, the pass) is shared.

The models' licence is InsightFace's: non-commercial research use only. They
are offered for a household's own library and say so on the AI models page.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("ninaivu.faces.arcface")

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is a hard requirement in practice
    np = None  # type: ignore

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional at runtime
    cv2 = None  # type: ignore


MODEL_ID = "scrfd-10g/arcface-w600k-r50"
EMBED_DIM = 512

DETECTOR_FILE = "det_10g.onnx"
RECOGNIZER_FILE = "w600k_r50.onnx"

#: SCRFD scores its faces more modestly than YuNet: InsightFace keeps
#: everything from 0.5, and a real face in a family photograph is rarely
#: below 0.6. Used where :mod:`faces` uses its ``MIN_DET_SCORE``.
MIN_DET_SCORE = 0.6

#: The square the detector looks at. Fixed by the exported model.
DETECT_SIZE = 640

#: Where ArcFace expects the five landmarks of a 112x112 face to be: the
#: template every ArcFace model is trained against, in that order (the
#: subject's right eye, left eye, nose, right and left mouth corners).
ARCFACE_TEMPLATE = (
    (38.2946, 51.6963), (73.5318, 51.5014), (56.0252, 71.7366),
    (41.5493, 92.3655), (70.7299, 92.2041),
)

_STRIDES = (8, 16, 32)
_ANCHORS_PER_CELL = 2
_NMS_IOU = 0.4


def _session(path: Path, threads: int):
    import onnxruntime as ort  # noqa: PLC0415

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


class Recogniser:
    """The detector and the recogniser, loaded once, driven one call at a time.

    *folder* holds the two files. Raises whatever loading raises; the engine
    turns that into an ``unavailable_reason`` as it does for OpenCV's models.
    """

    model_id = MODEL_ID
    embed_dim = EMBED_DIM
    min_det_score = MIN_DET_SCORE

    def __init__(self, folder: Path, threads: int = 1) -> None:
        self._detector = _session(Path(folder) / DETECTOR_FILE, threads)
        self._recognizer = _session(Path(folder) / RECOGNIZER_FILE, threads)
        self._det_input = self._detector.get_inputs()[0].name
        self._rec_input = self._recognizer.get_inputs()[0].name
        # The exported detector's nine outputs are scores, boxes and
        # landmarks for each of three strides. They are told apart by shape
        # here, not by name, so a re-export with other names still works:
        # the last axis is 1, 4 or 10, and the finest stride has most rows.
        self._outputs = [o.name for o in self._detector.get_outputs()]
        self._lock = threading.Lock()
        self._anchors: dict[int, "np.ndarray"] = {}
        for stride in _STRIDES:
            cells = DETECT_SIZE // stride
            ys, xs = np.mgrid[0:cells, 0:cells].astype("float32")
            centres = np.stack([xs, ys], axis=-1).reshape(-1, 2) * stride
            self._anchors[stride] = np.repeat(centres, _ANCHORS_PER_CELL, axis=0)

    # -- detection --------------------------------------------------------
    def detect(self, image: "np.ndarray", min_score: float | None = None,
               max_faces: int = 32) -> "np.ndarray":
        """Faces in one BGR image as rows of ``x, y, w, h, 5×(x, y), score``
        in the image's own pixels, best first; an empty array for none."""
        threshold = float(self.min_det_score if min_score is None else min_score)
        height, width = image.shape[:2]
        scale = DETECT_SIZE / float(max(height, width))
        new_w, new_h = max(1, int(round(width * scale))), max(1, int(round(height * scale)))
        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA
                             if scale < 1.0 else cv2.INTER_LINEAR)
        square = np.zeros((DETECT_SIZE, DETECT_SIZE, 3), dtype="uint8")
        square[:new_h, :new_w] = resized
        blob = cv2.dnn.blobFromImage(square, 1.0 / 128.0, (DETECT_SIZE, DETECT_SIZE),
                                     (127.5, 127.5, 127.5), swapRB=True)
        with self._lock:
            outputs = self._detector.run(self._outputs, {self._det_input: blob})

        by_stride = self._group_outputs(outputs)
        boxes, points, scores = [], [], []
        for stride, (score, box, kps) in by_stride.items():
            keep = np.flatnonzero(score >= threshold)
            if keep.size == 0:
                continue
            anchors = self._anchors[stride][keep]
            distances = box[keep] * stride
            x1 = anchors[:, 0] - distances[:, 0]
            y1 = anchors[:, 1] - distances[:, 1]
            x2 = anchors[:, 0] + distances[:, 2]
            y2 = anchors[:, 1] + distances[:, 3]
            boxes.append(np.stack([x1, y1, x2, y2], axis=1))
            landmarks = kps[keep] * stride
            landmarks[:, 0::2] += anchors[:, :1]
            landmarks[:, 1::2] += anchors[:, 1:]
            points.append(landmarks)
            scores.append(score[keep])
        if not boxes:
            return np.zeros((0, 15), dtype="float32")
        boxes_all = np.vstack(boxes) / scale
        points_all = np.vstack(points) / scale
        scores_all = np.concatenate(scores)
        order = np.argsort(-scores_all, kind="stable")
        kept = _nms(boxes_all[order], _NMS_IOU)[:max_faces]
        chosen = order[kept]
        rows = np.zeros((len(chosen), 15), dtype="float32")
        rows[:, 0] = boxes_all[chosen, 0]
        rows[:, 1] = boxes_all[chosen, 1]
        rows[:, 2] = boxes_all[chosen, 2] - boxes_all[chosen, 0]
        rows[:, 3] = boxes_all[chosen, 3] - boxes_all[chosen, 1]
        rows[:, 4:14] = points_all[chosen]
        rows[:, 14] = scores_all[chosen]
        return rows

    def _group_outputs(self, outputs: list) -> dict[int, tuple]:
        """The detector's outputs as ``{stride: (scores, boxes, landmarks)}``."""
        kinds: dict[int, list] = {1: [], 4: [], 10: []}
        for value in outputs:
            flat = np.asarray(value, dtype="float32").reshape(-1, value.shape[-1])
            if flat.shape[1] in kinds:
                kinds[flat.shape[1]].append(flat)
        grouped: dict[int, tuple] = {}
        for stride in _STRIDES:
            rows = self._anchors[stride].shape[0]
            parts = []
            for width in (1, 4, 10):
                match = next((v for v in kinds[width] if v.shape[0] == rows), None)
                if match is None:
                    raise RuntimeError(f"the detector gave no output with {rows} rows "
                                       f"and {width} values for stride {stride}")
                parts.append(match)
            grouped[stride] = (parts[0].reshape(-1), parts[1], parts[2])
        return grouped

    # -- embedding --------------------------------------------------------
    def embed(self, image: "np.ndarray", row: "np.ndarray") -> tuple[Any, "np.ndarray | None"]:
        """One face, aligned to 112x112 by its five landmarks, and its
        embedding (not yet normalised). ``(None, None)`` if it cannot be
        aligned."""
        source = np.asarray(row[4:14], dtype="float32").reshape(5, 2)
        target = np.asarray(ARCFACE_TEMPLATE, dtype="float32")
        matrix, _ = cv2.estimateAffinePartial2D(source, target, method=cv2.LMEDS)
        if matrix is None:
            return None, None
        aligned = cv2.warpAffine(image, matrix, (112, 112), borderValue=0.0)
        blob = cv2.dnn.blobFromImage(aligned, 1.0 / 127.5, (112, 112),
                                     (127.5, 127.5, 127.5), swapRB=True)
        with self._lock:
            feature = self._recognizer.run(None, {self._rec_input: blob})[0]
        return aligned, np.asarray(feature, dtype="float32").reshape(-1)


def _nms(boxes: "np.ndarray", threshold: float) -> list[int]:
    """Indices to keep, *boxes* already best first, as ``x1 y1 x2 y2``."""
    if boxes.shape[0] == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = np.arange(boxes.shape[0])
    keep: list[int] = []
    while order.size:
        first = int(order[0])
        keep.append(first)
        rest = order[1:]
        xx1 = np.maximum(x1[first], x1[rest])
        yy1 = np.maximum(y1[first], y1[rest])
        xx2 = np.minimum(x2[first], x2[rest])
        yy2 = np.minimum(y2[first], y2[rest])
        overlap = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[first] + areas[rest] - overlap
        iou = np.where(union > 0, overlap / np.maximum(union, 1e-6), 0.0)
        order = rest[iou <= threshold]
    return keep
