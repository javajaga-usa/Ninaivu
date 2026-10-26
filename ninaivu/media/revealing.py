"""People in nothing but their underwear: explicit.

A photograph of somebody in only a bra, only boxers or lingerie is kept out of
the family app the way adult content is: one scored at :func:`line` or over is
flagged explicit (storage/db.py, ``flag_revealing``), so family
members see it neither in the gallery nor in search, and administrators still
do. The household asked for it (2026-10-26); before that it was only left out
of a family member's search (``search_shield_clause``), which still reads the
score.

The judgement reads the vectors tagging already stored, so no photograph is
opened again. Each picture is scored against the descriptions below on their
own, with the search model's learned scale and bias, as SigLIP is trained to
be read (the explicit check does the same). Swimwear at a beach or a pool
looks much like underwear to the model and is an ordinary family photograph,
so a picture the swimwear descriptions fit better scores 0.

Only a model whose scale and bias are known here is read: on any other the
scores would be on a scale nobody chose a line for, and nothing is judged.
"""
from __future__ import annotations

from typing import Any

import numpy as np

#: Bumped when the descriptions, the line or how they are read change: every
#: picture is judged again from its stored vector.
VERSION = 2

#: People in nothing but their underwear.
PROMPTS = (
    "a person wearing only underwear",
    "a woman wearing only a bra and panties",
    "a woman in a bra",
    "a man wearing only boxer shorts",
    "a man in his underwear, briefs or boxers",
    "a person in lingerie",
)
#: What looks like it and is not: these winning scores the picture 0.
COVERED = (
    "people in swimsuits at a beach",
    "people in swimwear at a swimming pool",
    "a child in a swimsuit playing in water",
    "a person wearing a sleeveless vest or sportswear",
)

#: The learned scale (as a logarithm) and bias of each search model whose
#: scores this reads; runtime/siglip.py has where SigLIP 2's come from.
MODELS: dict[str, tuple[float, float]] = {}


def _known() -> dict[str, tuple[float, float]]:
    if not MODELS:
        from ..runtime import siglip                     # noqa: PLC0415
        MODELS[siglip.MODEL_ID] = (siglip.LOG_SCALE, siglip.BIAS)
    return MODELS


#: A picture scored at this or over is flagged explicit, unless the
#: household set ``underwear_threshold`` (server/config.py) to another line.
LINE = 0.005


def line(cfg: Any) -> float:
    """The line this household flags at; 0 when the check is off."""
    try:
        return max(0.0, float(getattr(cfg, "underwear_threshold", LINE)))
    except (TypeError, ValueError):
        return LINE


class Scorer:
    """Scores image vectors from one search model."""

    def __init__(self, model_id: str, matrix: np.ndarray, log_scale: float,
                 bias: float) -> None:
        self.model_id = model_id
        self.matrix = np.asarray(matrix, dtype=np.float32)
        self.scale = float(np.exp(log_scale))
        self.bias = float(bias)

    def score(self, vectors: Any) -> np.ndarray:
        """How much each row looks like people in only their underwear, 0..1."""
        vectors = np.asarray(vectors, dtype=np.float32).reshape(-1, self.matrix.shape[1])
        logits = self.scale * (vectors @ self.matrix.T) + self.bias
        chance = 1.0 / (1.0 + np.exp(-logits))
        bare = chance[:, :len(PROMPTS)].max(axis=1)
        covered = chance[:, len(PROMPTS):].max(axis=1)
        return np.where(bare > covered, bare, 0.0)


def applies(model_id: str) -> bool:
    """Whether pictures are judged under this search model."""
    return model_id in _known()


def scorer_for(engine: Any) -> Scorer | None:
    """The scorer for this engine, made once and kept on it, or ``None``.

    ``None`` for an engine with no text encoder (the light tagger, AI off) and
    for a model whose scale and bias are not known here.
    """
    if engine is None:
        return None
    inner = getattr(engine, "_engine", engine)        # a thresholded wrapper
    cached = getattr(inner, "_revealing_scorer", False)
    if cached is not False:
        return cached
    scorer = None
    model_id = str(getattr(inner, "model_id", "") or "")
    known = _known().get(model_id)
    embed = getattr(inner, "embed_texts", None)
    encode = getattr(inner, "encode_text", None)
    if known is not None and (embed is not None or encode is not None):
        texts = [*PROMPTS, *COVERED]
        if embed is not None:
            matrix = np.asarray(embed(texts), dtype=np.float32)
        else:
            matrix = np.stack([np.asarray(encode(t), dtype=np.float32) for t in texts])
        scorer = Scorer(model_id, matrix, *known)
    try:
        inner._revealing_scorer = scorer
    except Exception:                                      # noqa: BLE001
        pass
    return scorer
