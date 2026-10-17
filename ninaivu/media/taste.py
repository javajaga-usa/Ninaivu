"""Which photographs look well made, judged from the search model's vectors.

A book of "the best photographs" needs more than sharp and large: of two
sharp photographs of a birthday, one is the children laughing round the cake
and the other is the back of somebody's head. The search model already
learned, from millions of captioned pictures, what "a beautiful photograph"
and "a happy family smiling together" look like next to "a blurry, badly lit
snapshot" — and it already made a vector for every photograph here when the
library was tagged. Comparing those vectors with a few such descriptions is a
matrix product: no photograph is opened again, nothing is downloaded, and
nothing leaves this machine.

The answer is only ever compared within one set of candidates — the year, the
trip, the person a book is being made of — and given as how far each one is
above or below the others (a z-score). That keeps it independent of the model:
a different model's raw numbers sit on another scale, but "better than most
photographs of this trip" means the same with any of them. Without a model
that reads text (the light tagger, AI off) there is no answer, and the book is
picked the way it always was.
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

#: Bump when the descriptions or the scoring change.
TASTE_VERSION = 1

#: What a family keeps on the mantelpiece.
GOOD_PROMPTS = (
    "a beautiful, well composed photograph",
    "a happy family smiling together",
    "a candid photo of people laughing",
    "a joyful moment at a celebration",
    "a stunning travel photograph",
    "a professional portrait with soft light",
    "a cute child smiling at the camera",
)

#: What nobody prints.
BAD_PROMPTS = (
    "a blurry, out of focus photo",
    "a badly lit, dark snapshot",
    "an accidental photo of the floor",
    "a photo with eyes closed",
    "a boring photo of a wall",
    "a photo of the back of someone's head",
    "a cluttered, badly framed snapshot",
)

#: Furthest a photograph is allowed to stand out either way, in standard
#: deviations: one odd vector must not outweigh everything else that is known.
CLIP_Z = 2.5


class Taste:
    """Scores image vectors from one search model."""

    def __init__(self, model_id: str, matrix: np.ndarray, scale: float) -> None:
        self.model_id = model_id
        self.matrix = np.asarray(matrix, dtype=np.float32)
        self.scale = float(scale)

    @property
    def dim(self) -> int:
        return int(self.matrix.shape[1])

    def raw(self, vectors: np.ndarray) -> np.ndarray:
        """How likely each row is one of the good descriptions, 0..1."""
        vectors = np.asarray(vectors, dtype=np.float32).reshape(-1, self.dim)
        logits = self.scale * (vectors @ self.matrix.T)
        logits -= logits.max(axis=1, keepdims=True)
        weights = np.exp(logits)
        weights /= weights.sum(axis=1, keepdims=True)
        return weights[:, :len(GOOD_PROMPTS)].sum(axis=1)


def taste_for(engine: Any) -> Taste | None:
    """The scorer for this engine, made once and kept on it, or ``None``."""
    if engine is None:
        return None
    inner = getattr(engine, "_engine", engine)        # a thresholded wrapper
    cached = getattr(inner, "_taste_scorer", False)
    if cached is not False:
        return cached
    taste = None
    embed = getattr(inner, "embed_texts", None)
    if embed is not None and getattr(inner, "semantic", True):
        try:
            matrix = np.asarray(embed(list(GOOD_PROMPTS) + list(BAD_PROMPTS)), dtype=np.float32)
        except Exception:                                  # noqa: BLE001
            matrix = None
        if matrix is not None and matrix.ndim == 2 and matrix.shape[0] == \
                len(GOOD_PROMPTS) + len(BAD_PROMPTS):
            scale = 100.0
            try:
                scale = float(inner.model.logit_scale.exp().detach())
            except Exception:                              # noqa: BLE001
                pass
            taste = Taste(str(getattr(inner, "model_id", "") or ""), matrix, scale)
    try:
        inner._taste_scorer = taste
    except Exception:                                      # noqa: BLE001
        pass
    return taste


def relative(taste: Taste | None, ids: Sequence[int], store: tuple | None,
             ) -> dict[int, float]:
    """For each of *ids* that has a vector: how far above (+) or below (−) the
    others it is, in standard deviations, clipped to ±:data:`CLIP_Z`.

    *store* is what :func:`ninaivu.storage.embeddings.embedding_store` returns.
    Fewer than three vectors say nothing about a set, and give nothing.
    """
    if taste is None or not store or not ids:
        return {}
    all_ids, buffer, dim, index = store
    if not all_ids or int(dim) != taste.dim:
        return {}
    matrix = np.frombuffer(buffer, dtype="float32").reshape(len(all_ids), int(dim))
    have = [(int(i), index[int(i)]) for i in ids if int(i) in index]
    if len(have) < 3:
        return {}
    scores = taste.raw(matrix[[row for _, row in have]])
    spread = float(scores.std())
    if not np.isfinite(spread) or spread < 1e-6:
        return {}
    z = np.clip((scores - float(scores.mean())) / spread, -CLIP_Z, CLIP_Z)
    return {asset_id: round(float(value), 4) for (asset_id, _), value in zip(have, z)}
