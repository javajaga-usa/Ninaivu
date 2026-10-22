"""Find this print: which photograph in the library a framed print is a copy of.

Somebody points a phone at a photograph on Paati's wall, or one in an album,
and asks whether the family already has it. The phone's picture is not the
photograph: it is the print seen through glass, at an angle, inside a frame
and a mount, with a lamp's reflection across it. Two things the library
already keeps are compared with it, and either may find the original:

* **The picture fingerprint** (``assets.phash``, the 64-bit DCT hash the
  duplicate finder uses). It is kept for every photograph, hidden ones too,
  and needs no model. It is strict: the same picture gives nearly the same
  bits, a different one about half different. A frame or a mount left in the
  crop spoils it, so the print is also tried with its edges cut away, and in
  each quarter turn in case it was stood up the wrong way.
* **The picture search** (the stored SigLIP 2 or CLIP vectors). It forgives
  glare, fading and a crooked crop far better, and is only there for
  photographs it has read and only while its model is loaded.

The print is found in the phone's picture by the same code that scans prints
(:mod:`print_scan`), then flattened and stood up. A frame's mount often shows
as a second outline inside the first, so the print is looked into once more.

What counts as the same photograph is a guess in numbers below, made without
the family's real prints: a phone picture of a print and its original have
not been measured on the Mac yet. Nothing here leaves the machine, and what a
person may be shown is not decided here: the caller narrows the answer to the
photographs that person may open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image

from . import media, print_scan

log = logging.getLogger(__name__)

#: How much of each edge is cut away for the extra tries: none, a thin frame
#: or the white border of a print, and a mount.
INSETS = (0.0, 0.06, 0.14)
#: Prints in one phone picture that are looked up. A wall of frames is better
#: photographed one at a time; three covers a page of an album.
MAX_PRINTS = 3

#: Fingerprint bits that may differ for the very same photograph. Two
#: unrelated photographs differ in about 32 of the 64; a phone picture of a
#: print, through glass, in perhaps 6 to 12 (a guess, untuned).
SURE_BITS = 10
#: …and up to this, a likely one, shown as "might be".
MAYBE_BITS = 16
#: Picture search likeness (cosine of unit vectors) for the very same
#: photograph, and for one worth showing. Guesses, untuned: a re-saved copy
#: is about 0.98, the same scene shot a second later about 0.9.
SURE_LOOK = 0.93
MAYBE_LOOK = 0.84
#: Both together: a fingerprint this close with a look this alike is sure.
BOTH_BITS, BOTH_LOOK = MAYBE_BITS, MAYBE_LOOK
#: Nearest photographs asked of the picture search, per view of the print.
LOOK_TOP = 40
#: Photographs shown at most.
MAX_SHOWN = 8


@dataclass
class Match:
    """One library photograph and how it matched."""

    id: int
    bits: int | None = None
    look: float | None = None
    prints: set[int] = field(default_factory=set)

    @property
    def sure(self) -> bool:
        return ((self.bits is not None and self.bits <= SURE_BITS)
                or (self.look is not None and self.look >= SURE_LOOK)
                or (self.bits is not None and self.bits <= BOTH_BITS
                    and self.look is not None and self.look >= BOTH_LOOK))

    @property
    def score(self) -> float:
        """One number to order by: the better of the two, on one scale."""
        by_bits = 1.0 - self.bits / 32.0 if self.bits is not None else 0.0
        return max(by_bits, self.look or 0.0)

    def as_dict(self) -> dict[str, Any]:
        how = []
        if self.bits is not None:
            how.append("fingerprint")
        if self.look is not None:
            how.append("look")
        return {"id": self.id, "sure": self.sure, "how": how,
                "bits": self.bits,
                "look": None if self.look is None else round(float(self.look), 3),
                "prints": sorted(self.prints)}


# ---------------------------------------------------------------------------
# The print, in the ways it may be compared
# ---------------------------------------------------------------------------

def _cut(img: Image.Image, share: float) -> Image.Image:
    if share <= 0:
        return img
    dx, dy = round(img.width * share), round(img.height * share)
    if img.width - 2 * dx < 16 or img.height - 2 * dy < 16:
        return img
    return img.crop((dx, dy, img.width - dx, img.height - dy))


def _inner(img: Image.Image) -> Image.Image | None:
    """The picture inside a mount: the one print found inside this print."""
    try:
        found = print_scan.find_prints(img)
    except Exception:                                   # noqa: BLE001 - a try, not a must
        log.debug("find print: no inner outline", exc_info=True)
        return None
    if len(found) != 1 or found[0].whole:
        return None
    return print_scan.warp(img, found[0])


def views(img: Image.Image) -> list[Image.Image]:
    """The print as found, the picture inside its mount where there is one,
    and each of those with its edges cut away."""
    bases = [img.convert("RGB")]
    inner = _inner(bases[0])
    if inner is not None:
        bases.append(inner)
    return [_cut(base, share) for base in bases for share in INSETS]


def fingerprints(images: Iterable[Image.Image]) -> list[int]:
    """The picture fingerprint of each view in each quarter turn."""
    out: list[int] = []
    for img in images:
        grey = img.convert("L")
        for turn in (0, 90, 180, 270):
            turned = grey if not turn else grey.rotate(turn, expand=True)
            value = media.perceptual_hash(turned)
            if value:
                number = int(value, 16)
                if number not in out:
                    out.append(number)
    return out


# ---------------------------------------------------------------------------
# Looking them up
# ---------------------------------------------------------------------------

_BYTE_BITS = np.array([bin(n).count("1") for n in range(256)], dtype=np.uint8)


def _popcount(values: np.ndarray) -> np.ndarray:
    counter = getattr(np, "bitwise_count", None)
    if counter is not None:
        return counter(values).astype(np.int64)
    return _BYTE_BITS[values.view(np.uint8)].reshape(-1, 8).sum(axis=1, dtype=np.int64)


def by_fingerprint(rows: Iterable[tuple[int, str]], prints: Sequence[Sequence[int]],
                   within: int = MAYBE_BITS) -> dict[int, tuple[int, int]]:
    """``{asset id: (fewest bits apart, which print)}`` for every photograph
    whose fingerprint is within *within* bits of one of the prints'.

    *rows* are ``(id, phash)`` pairs as the index keeps them (16 hex digits);
    *prints* holds each print's fingerprints (:func:`fingerprints`).
    """
    ids: list[int] = []
    values: list[int] = []
    for asset_id, phash in rows:
        try:
            values.append(int(phash, 16))
        except (TypeError, ValueError):
            continue
        ids.append(int(asset_id))
    if not ids:
        return {}
    stored = np.array(values, dtype=np.uint64)
    best = np.full(len(ids), 65, dtype=np.int64)
    which = np.zeros(len(ids), dtype=np.int64)
    for index, queries in enumerate(prints):
        for query in queries:
            distance = _popcount(np.bitwise_xor(stored, np.uint64(query)))
            closer = distance < best
            best[closer] = distance[closer]
            which[closer] = index
    near = np.nonzero(best <= within)[0]
    return {ids[i]: (int(best[i]), int(which[i])) for i in near}


def look_ready(engine: Any) -> bool:
    return bool(engine is not None and getattr(engine, "semantic", False)
                and callable(getattr(engine, "embed_images", None)))


def by_look(engine: Any, prints: Sequence[Sequence[Image.Image]], store: tuple,
            floor: float = MAYBE_LOOK, top: int = LOOK_TOP) -> dict[int, tuple[float, int]]:
    """``{asset id: (best likeness, which print)}`` from the picture search,
    for every photograph at least *floor* alike. Empty without the model, or
    when the stored vectors are another model's (another width)."""
    ids, buffer, dim, _ = store
    if not ids or not dim or not look_ready(engine):
        return {}
    from .. import ai as ai_mod                              # noqa: PLC0415

    found: dict[int, tuple[float, int]] = {}
    for index, images in enumerate(prints):
        if not images:
            continue
        try:
            vectors = np.asarray(engine.embed_images([im.convert("RGB") for im in images]),
                                 dtype=np.float32)
        except Exception:                                   # noqa: BLE001 - the fingerprint still runs
            log.warning("find print: the picture search could not read the print",
                        exc_info=True)
            return found
        if vectors.ndim != 2 or vectors.shape[1] != dim:
            log.info("find print: the search vectors are another model's (%s, not %s wide)",
                     vectors.shape[-1] if vectors.ndim else "?", dim)
            return found
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
        for vector in vectors:
            for asset_id, score in ai_mod.semantic_search(vector, ids, buffer, dim,
                                                          top_k=top, min_score=floor):
                if asset_id not in found or score > found[asset_id][0]:
                    found[asset_id] = (float(score), index)
    return found


def combine(fingerprinted: dict[int, tuple[int, int]],
            looked: dict[int, tuple[float, int]]) -> list[Match]:
    """Every photograph either found, best first."""
    matches: dict[int, Match] = {}
    for asset_id, (bits, index) in fingerprinted.items():
        matches.setdefault(asset_id, Match(asset_id)).bits = bits
        matches[asset_id].prints.add(index)
    for asset_id, (look, index) in looked.items():
        matches.setdefault(asset_id, Match(asset_id)).look = look
        matches[asset_id].prints.add(index)
    return sorted(matches.values(), key=lambda m: (not m.sure, -m.score, m.id))


def find(seen: Sequence[Image.Image], rows: Iterable[tuple[int, str]], store: tuple,
         engine: Any) -> list[Match]:
    """The library photographs these prints may be, best first.

    *seen* are the prints as :func:`print_scan.examine` cut them out of the
    phone's picture, at most :data:`MAX_PRINTS` of them; *rows* the
    ``(id, phash)`` of the photographs to look among; *store*
    ``db.embedding_store``'s answer. The caller still narrows the answer to
    what the person may see.
    """
    looks = [views(img) for img in seen[:MAX_PRINTS]]
    fingerprinted = by_fingerprint(rows, [fingerprints(v) for v in looks])
    looked = by_look(engine, looks, store)
    return combine(fingerprinted, looked)
