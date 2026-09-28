"""Screenshots, documents and photographs of screens: administrators only.

A family library swept up from phones and laptops holds a great deal that was
never meant for the gallery: screenshots of chats and bank apps, a passport
photographed for a visa form, a receipt, a rental agreement, a monitor full of
somebody's code. With the setting on (the default) those are hidden, the same
level an administrator would give them by hand.

Two signals, either of which is enough:

* **The name.** ``Screenshot_20240607_234608_WhatsApp.jpg``, ``Screen Shot
  2019-03-01 at 10.12.jpg`` or a ``Screenshots`` folder. Known at indexing time
  and needs no model, so it works on every install.
* **The picture.** The image vector the search model already made for the
  photograph is compared with descriptions of screens and documents on one
  side and of ordinary photographs on the other. It costs a matrix product —
  no image is opened again — so a library tagged before this existed is judged
  from what is already in the database.

Only an automatic level is ever replaced. Anything an administrator set on the
item itself is left exactly as they set it, and making an item visible again
by hand is what keeps it visible. A folder rule does not lift the hiding: a
folder of holiday photographs made public still holds the boarding passes.
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np

#: Bump when the descriptions or the scoring change, to judge everything again.
SCREEN_VERSION = 1

#: The visibility source written on items this hides.
SOURCE = "screen"

#: Sources an automatic rule may override. ``item`` is an administrator's own
#: decision; ``hidden`` and ``kind`` are already at the admin-only level.
REPLACEABLE_SOURCES = ("default", "folder")

#: Screenshot names in the languages phones and desktops ship with, as SQL
#: LIKE patterns: ``%`` is any run of characters and ``_`` any one character,
#: so ``screen_shot`` is "Screen Shot", "screen-shot" and "screen_shot" alike.
#: The regular expression below is made from these, so indexing a file and
#: filtering the Folders screen can never disagree about a name. SQLite's LIKE
#: ignores case for ASCII letters only, hence both cases of the Cyrillic one.
NAME_LIKE = (
    "%screenshot%", "%screen_shot%", "%screencap%", "%screen_cap%",
    "%screengrab%", "%screen_grab%", "%bildschirmfoto%", "%capture d_écran%",
    "%captura de pantalla%", "%schermata%", "%schermafbeelding%",
    "%zrzut ekranu%", "%снимок экрана%", "%Снимок экрана%",
    "%スクリーンショット%", "%屏幕截图%", "%截屏%", "%스크린샷%",
)

_NAME = re.compile(
    "|".join("".join("." if ch == "_" else re.escape(ch) for ch in pattern.strip("%"))
             for pattern in NAME_LIKE),
    re.IGNORECASE,
)


def named_like_screenshot(rel_path: str) -> bool:
    """Does the file's own name, or a folder it is in, say it is a screenshot?"""
    return bool(_NAME.search(str(rel_path).replace("\\", "/")))


def name_sql(alias: str = "") -> tuple[str, list[str]]:
    """The same test as :func:`named_like_screenshot`, as SQL on ``rel_path``."""
    dot = f"{alias}." if alias else ""
    return ("(" + " OR ".join(f"{dot}rel_path LIKE ?" for _ in NAME_LIKE) + ")",
            list(NAME_LIKE))


#: What the model is asked to tell apart. Measured against 34,000 tagged
#: photographs in a real family library (ViT-B-32): the photograph side names
#: what the screen side was otherwise catching — near-black frames, a person
#: beside a park sign, museum plaques, a keyboard, a licence plate, a teddy
#: bear. Adding them cut camera photographs flagged at 0.6 from 844 to 613
#: without losing a single screenshot.
SCREEN_PROMPTS = (
    "a screenshot of a phone screen",
    "a screenshot of a computer program",
    "a screenshot of a website",
    "a screenshot of a chat conversation",
    "a photo of a computer monitor showing a program",
    "a photo of a laptop screen with text",
    "a scanned document",
    "a photo of a printed document with text",
    "a receipt or invoice",
    "a form or letter on paper",
    "a page of text",
    "a presentation slide",
    "a spreadsheet",
)
PHOTO_PROMPTS = (
    "a family photo",
    "a photo of people",
    "a photo of a child",
    "a selfie",
    "a landscape photograph",
    "a photo of food",
    "a photo of a pet",
    "a photo of a building or a street",
    "a photo of an everyday object",
    "a photo of a room",
    "people watching television",
    "a person using a laptop",
    "a photo of a celebration",
    "a photo of flowers or plants",
    "a very dark or black photo",
    "a blurry photo",
    "a sign or information board at a tourist attraction",
    "a museum exhibit",
    "a photo of a car",
    "a toy or stuffed animal",
    "a photo of a window or curtains",
    "a person standing next to a sign",
)

#: The share of the softmax the screen side must hold, per search model.
#:
#: ViT-B-32, on the same library: at 0.8, 81% of files named as screenshots
#: were caught by the picture alone (the rest are screenshots *of photographs*
#: — a video call, an Instagram post — which the name catches), and 2.2% of
#: camera photographs were flagged. Looked at, those were passports, visas,
#: receipts, letters and monitors, with very few ordinary photographs among
#: them. Between 0.5 and 0.8 ordinary photographs were common.
#:
#: A model that is not listed has not been measured, and its scores are on a
#: different scale (SigLIP's cosines sit far closer together), so it hides
#: nothing by picture until somebody measures it. The name rule still applies.
#:
#: ViT-B-16-SigLIP2 was measured on the same library, 600 files named as
#: screenshots against 2,000 camera photographs, and is left out on purpose.
#: With 2.2% of camera photographs flagged it caught 14% of the screenshots
#: with this softmax, and 12% scoring each description on its own (sigmoid
#: with the model's bias, as SigLIP is trained). It is the better search model
#: and the worse judge of a screen: a library switched to it keeps the name rule
#: only. ``clip_model = "auto"`` picks it whenever it is downloaded, so a
#: library that relies on screenshots being hidden names ViT-B-32 instead.
THRESHOLDS: dict[str, float] = {
    "ViT-B-32/laion2b_s34b_b79k": 0.8,
}


class Scorer:
    """Scores image vectors from one search model."""

    def __init__(self, model_id: str, matrix: np.ndarray, scale: float,
                 threshold: float) -> None:
        self.model_id = model_id
        self.matrix = matrix.astype(np.float32)
        self.scale = float(scale)
        self.threshold = float(threshold)

    def score(self, vectors: np.ndarray) -> np.ndarray:
        """Probability each row is a screen or document, 0..1."""
        vectors = np.asarray(vectors, dtype=np.float32).reshape(-1, self.matrix.shape[1])
        logits = self.scale * (vectors @ self.matrix.T)
        logits -= logits.max(axis=1, keepdims=True)
        weights = np.exp(logits)
        weights /= weights.sum(axis=1, keepdims=True)
        return weights[:, :len(SCREEN_PROMPTS)].sum(axis=1)


def scorer_for(engine: Any) -> Scorer | None:
    """The scorer for this engine, made once and kept on it, or ``None``.

    ``None`` for an engine with no text encoder (the light tagger, AI off) and
    for a model nobody has measured a threshold for.
    """
    if engine is None:
        return None
    inner = getattr(engine, "_engine", engine)        # a thresholded wrapper
    cached = getattr(inner, "_screen_scorer", False)
    if cached is not False:
        return cached
    scorer = None
    model_id = getattr(inner, "model_id", "")
    threshold = THRESHOLDS.get(model_id)
    embed = getattr(inner, "embed_texts", None)
    if threshold is not None and embed is not None:
        matrix = np.asarray(embed(list(SCREEN_PROMPTS) + list(PHOTO_PROMPTS)))
        model = getattr(inner, "model", None)
        scale = 100.0
        try:
            scale = float(model.logit_scale.exp().detach())
        except Exception:                                  # noqa: BLE001
            pass
        scorer = Scorer(model_id, matrix, scale, threshold)
    try:
        inner._screen_scorer = scorer
    except Exception:                                      # noqa: BLE001
        pass
    return scorer
